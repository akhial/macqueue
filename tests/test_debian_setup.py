import importlib.util
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE = Path(__file__).resolve().parents[1] / "deploy/debian/agent.py"
spec = importlib.util.spec_from_file_location("debian_agent", MODULE)
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)
serve_spec = importlib.util.spec_from_file_location("debian_serve", MODULE.with_name("serve.py"))
serve = importlib.util.module_from_spec(serve_spec)
serve_spec.loader.exec_module(serve)


class SourceExportTests(unittest.TestCase):
    def test_installed_wrapper_help_includes_source_commands(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'client.json').write_text(json.dumps({}))
            output = io.StringIO()
            with patch.object(agent, '__file__', str(root / 'agent.py')), patch('sys.argv', ['macqueue', '--help']), contextlib.redirect_stdout(output):
                with self.assertRaises(SystemExit) as result:
                    agent.main()
            self.assertEqual(0, result.exception.code)
            for action in ('source-status', 'export-source', 'plan', 'submit'):
                self.assertIn(action, output.getvalue())

    def test_systemd_credential_adapter_keeps_owner_only_mode_across_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "systemd.token"
            source.write_text("first-secret")
            source.chmod(0o440)
            runtime = root / "runtime"
            runtime.mkdir(mode=0o700)
            serve.copy_credential(source, runtime, "submit")
            self.assertEqual("first-secret", (runtime / "submit.token").read_text())
            self.assertEqual(0o600, (runtime / "submit.token").stat().st_mode & 0o777)
            source.chmod(0o600)
            source.write_text("replacement-secret")
            source.chmod(0o440)
            serve.copy_credential(source, runtime, "submit")
            self.assertEqual("replacement-secret", (runtime / "submit.token").read_text())
            self.assertEqual(["submit.token"], [p.name for p in runtime.iterdir()])

    def test_bundle_contains_exact_commits_without_mutating_active_worktree(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo, worktree = root / "repo", root / "worktree"
            agent.git(root, "init", str(repo))
            (repo / "source.txt").write_text("baseline\n")
            agent.git(repo, "add", "source.txt")
            def commit(message):
                agent.git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", message)
                return agent.git(repo, "rev-parse", "HEAD")
            baseline = commit("baseline")
            agent.git(repo, "worktree", "add", "-b", "candidate", str(worktree))
            (worktree / "source.txt").write_text("candidate\n")
            agent.git(worktree, "add", "source.txt")
            agent.git(worktree, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "candidate")
            candidate = agent.git(worktree, "rev-parse", "HEAD")
            (worktree / "source.txt").write_text("uncommitted active agent changes\n")
            (worktree / "untracked.txt").write_text("private working notes\n")
            index = Path(agent.git(worktree, "rev-parse", "--path-format=absolute", "--git-path", "index"))
            before = (index.read_bytes(), agent.git(repo, "show-ref"))
            config = {"repository": str(repo), "worktree": str(worktree)}
            bundle = root / "source.bundle"
            result = agent.export_source(config, baseline, candidate, bundle)
            self.assertFalse(result["uncommitted_changes_included"])
            self.assertTrue(result["sources"][1]["changes"])
            self.assertEqual(before, (index.read_bytes(), agent.git(repo, "show-ref")))
            self.assertEqual("uncommitted active agent changes\n", (worktree / "source.txt").read_text())
            restored = root / "restored"
            agent.git(root, "clone", "--branch", "candidate", str(bundle), str(restored))
            self.assertEqual(candidate, agent.git(restored, "rev-parse", "HEAD"))
            self.assertEqual("candidate\n", (restored / "source.txt").read_text())
            self.assertFalse((restored / "untracked.txt").exists())
            self.assertEqual(0o600, bundle.stat().st_mode & 0o777)
            original = bundle.read_bytes()
            with self.assertRaises(FileExistsError):
                agent.export_source(config, baseline, candidate, bundle)
            self.assertEqual(original, bundle.read_bytes())
            with self.assertRaises(ValueError):
                agent.export_source(config, "HEAD", candidate, root / "bad.bundle")
