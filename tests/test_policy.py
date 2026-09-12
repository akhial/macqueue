import copy
import json
import tempfile
import unittest
from pathlib import Path

from macqueue.common import Invalid, safe_path
from macqueue.plans import benchmark, command, correctness, pgo
from macqueue.policy import Policy
from macqueue.runner import pack_artifacts
from macqueue.schema import validate_job


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = Policy({"rust_toolchain": "/toolchain", "cargo_home": "/cargo", "projects": {"seedfinder": "/mirror"},
                              "capabilities": ["cargo", "benchmark", "inspect", "pgo", "profiling"]})
        self.job = benchmark("seedfinder", "a" * 40, "b" * 40, query={}, seeds=[123, 456, 789])

    def test_generated_plans_are_locally_valid(self):
        self.policy.validate(self.job)
        self.policy.validate(correctness("seedfinder", "a" * 40))
        self.policy.validate(correctness("seedfinder", "a" * 40, "test::needle"))
        self.policy.validate(pgo("seedfinder", "a" * 40, query={}, seeds=[1, 2]))
        self.policy.validate(benchmark("seedfinder", "a" * 40, "b" * 40, query={}, seeds=[1], profiling=True))

    def test_command_and_environment_escapes_are_rejected(self):
        attacks = [
            lambda c: c.update(argv=["sh", "-c", "id"]),
            lambda c: c.update(argv=["python3", "-c", "print(1)"]),
            lambda c: c["argv"].append("--config=build.rustc=/tmp/evil"),
            lambda c: c["argv"].remove("--offline"),
            lambda c: c["env"].update(PATH="/tmp/evil"),
            lambda c: c["env"].update(RUSTFLAGS="-Clink-arg=evil"),
            lambda c: c["env"].update(RUSTFLAGS="-Cprofile-generate=/tmp/escape"),
            lambda c: c["env"].update(CARGO_TARGET_DIR="${JOB}/work/targets/../../escape"),
            lambda c: c.update(cwd="work/checkouts/candidate/../../other"),
            lambda c: c.update(stdout="artifacts/frozen/candidate/seed-seeker"),
            lambda c: c.update(argv=["kill", "-9", "1"]),
            lambda c: c.update(argv=["sample", "1", "10"]),
        ]
        for attack in attacks:
            with self.subTest(attack=attack):
                job = copy.deepcopy(self.job)
                attack(job["steps"][0]["command"])
                with self.assertRaises((Invalid, ValueError)):
                    self.policy.validate(job)

    def test_disabled_optional_capabilities_and_unknown_project(self):
        basic = Policy({"rust_toolchain": "/toolchain", "cargo_home": "/cargo", "projects": {"seedfinder": "/mirror"}})
        with self.assertRaises(Invalid):
            basic.validate(pgo("seedfinder", "a" * 40, query={}, seeds=[1]))
        self.job["project"] = "unapproved"
        with self.assertRaises(Invalid):
            basic.validate(self.job)

    def test_shell_metacharacters_in_query_remain_data(self):
        query = {"text": "$(touch /tmp/escaped); `id`"}
        spec = benchmark("seedfinder", "a" * 40, "b" * 40, query=query, seeds=[1])
        self.policy.validate(spec)
        self.assertEqual(query, json.loads(spec["steps"][-1]["commands"]["baseline"]["argv"][1]))

    def test_symlinks_dot_paths_and_special_artifacts_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "link").symlink_to("/tmp")
            (root / "dangling").symlink_to(root / "absent")
            for path in ("../escape", "/absolute", "a//b", "link/secret", "dangling/file"):
                with self.subTest(path=path), self.assertRaises(Invalid):
                    safe_path(root, path)
            artifacts = root / "artifacts"
            artifacts.mkdir()
            (artifacts / "leak").symlink_to("/etc/passwd")
            with self.assertRaises(Invalid):
                pack_artifacts(root, root / "archive.tar.gz")

    def test_source_revision_and_focused_filter_validation(self):
        for revision in ("main", "HEAD~1", "a" * 39, "--upload-pack=evil"):
            with self.subTest(revision=revision), self.assertRaises(Invalid):
                correctness("seedfinder", revision)
        with self.assertRaises(Invalid):
            self.policy.validate(correctness("seedfinder", "a" * 40, "--config=evil"))

