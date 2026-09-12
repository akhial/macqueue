import copy
import json
import tempfile
import unittest
from pathlib import Path

from macqueue.common import Invalid, safe_path
from macqueue.plans import benchmark, command, correctness, pgo
from macqueue.policy import Policy
from macqueue.runner import pack_artifacts
from macqueue.schema import MAX_EXPLICIT_SEEDS, MAX_RANGE_SEEDS, expand_seed_request, seed_request, validate_job


class PolicyTests(unittest.TestCase):
    def test_matching_seed_bounds_and_compact_ranges(self):
        for request in ({'seeds': [2**64-1] * MAX_EXPLICIT_SEEDS},
                        {'seed_range': {'start': 2**64-MAX_RANGE_SEEDS, 'count': MAX_RANGE_SEEDS}}):
            job = benchmark('seedfinder', 'a'*40, 'b'*40, query={}, **request)
            self.policy.validate(job)
            self.assertLess(len(json.dumps(job).encode()), 32 * 1024**2)
        self.assertEqual({'seeds': [123, 124, 125]}, expand_seed_request({'seed_range': {'start': 123, 'count': 3}}))
        invalid = [{'seeds': []}, {'seeds': [0] * (MAX_EXPLICIT_SEEDS+1)}, {'seeds': [True]},
                   {'seed_range': {'start': 0, 'count': 0}}, {'seed_range': {'start': 0, 'count': True}},
                   {'seed_range': {'start': 2**64-1, 'count': 2}},
                   {'seed_range': {'start': 0, 'count': MAX_RANGE_SEEDS+1}},
                   {'seeds': [1], 'seed_range': {'start': 0, 'count': 1}},
                   {'seed_range': {'start': 0, 'count': 1, 'step': 2}}]
        for request in invalid:
            with self.subTest(request=str(request)[:100]), self.assertRaises(Invalid):
                seed_request(request)

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
        checked = pgo('seedfinder', 'a'*40, query={}, seed_range={'start': 1000, 'count': 4096}, baseline_profile_sha256='b'*64)
        self.policy.validate(checked)
        builds = [s['command'] for s in checked['steps'] if s['op'] == 'exec' and s['command']['argv'][0] == 'cargo']
        self.assertEqual({'work/checkouts/candidate'}, {c['cwd'] for c in builds})
        self.assertEqual(3, len({c['env']['CARGO_TARGET_DIR'] for c in builds}))

    def test_profiling_build_does_not_enable_profilers(self):
        policy = Policy({**self.policy.config, 'capabilities': ['cargo', 'benchmark', 'inspect', 'profiling-build']})
        spec = benchmark('seedfinder', 'a'*40, 'b'*40, query={}, seeds=[1], profiling=True)
        policy.validate(spec)
        for tool in ('sample', 'xctrace'):
            cmd = command(['${JOB}/artifacts/frozen/candidate/seed-seeker', '--benchmark', '1000', '--workers', '1'], env={})
            spec['steps'] = [{'id': 'profile', 'op': 'profile', 'tool': tool, 'command': cmd,
                              'duration_seconds': 1, 'output': 'artifacts/profile.out'}]
            with self.assertRaisesRegex(Invalid, 'capability disabled locally: ' + tool):
                policy.validate(spec)

    def test_pgo_training_and_import_are_narrowly_scoped(self):
        spec = pgo('seedfinder', 'a'*40, query={}, seeds=[1], baseline_profile_sha256='b'*64)
        for key, value in (('source', 'work/checkouts/candidate/other.profdata'), ('sha256', 'x'*64)):
            changed = copy.deepcopy(spec)
            changed['steps'][0][key] = value
            with self.assertRaises(Invalid):
                self.policy.validate(changed)
        train = next(s for s in spec['steps'] if s['op'] == 'train')
        train['command']['argv'][2] = '8'
        with self.assertRaisesRegex(Invalid, 'one worker'):
            self.policy.validate(spec)

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
        provision = {'version': 1, 'project': 'seedfinder', 'sources': {'candidate': 'a'*40},
                     'steps': [{'id': 'provision', 'op': 'provision', 'sha256': 'b'*64}]}
        with self.assertRaisesRegex(Invalid, 'source-provision'):
            basic.validate(provision)
        Policy({**basic.config, 'capabilities': ['cargo', 'source-provision']}).validate(provision)
        provision['steps'].append({'id': 'later', 'op': 'metadata'})
        with self.assertRaisesRegex(Invalid, 'separate job'):
            validate_job(provision)
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
