import json
import platform
import shutil
import subprocess
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path

from macqueue.doctor import doctor
from macqueue.plans import benchmark, correctness, pgo
from macqueue.policy import Policy
from macqueue.runner import Runner, pack_artifacts
from macqueue.server import Server
from macqueue.store import Store
from macqueue.worker import Worker


@unittest.skipUnless(platform.system() == "Darwin" and platform.machine() == "arm64" and shutil.which("rustc"),
                     "requires Apple Silicon and an installed Rust toolchain")
class MacOSIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name).resolve()
        cls.toolchain = subprocess.check_output(["rustc", "--print", "sysroot"], text=True).strip()
        cls.source = cls.base / "source"
        cls.source.mkdir()
        (cls.base / "cargo").mkdir()
        cls.config = {"state_dir": str(cls.base / "state"), "cargo_home": str(cls.base / "cargo"),
                      "rust_toolchain": cls.toolchain, "projects": {"seedfinder": str(cls.source)}}
        files = {
            "Cargo.toml": '[workspace]\nmembers = ["cli", "ffi", "core"]\nresolver = "2"\n[profile.release]\nopt-level = 3\nlto = "fat"\ncodegen-units = 1\n[profile.profiling]\ninherits = "release"\ndebug = true\n',
            "cli/Cargo.toml": '[package]\nname = "shpd-seedfinder-cli"\nversion = "0.1.0"\nedition = "2021"\n[[bin]]\nname = "seed-seeker"\npath = "src/main.rs"\n',
            "cli/src/main.rs": 'fn main() { println!("benchmark completed"); }\n',
            "ffi/Cargo.toml": '[package]\nname = "shpd-seedfinder-ffi"\nversion = "0.1.0"\nedition = "2021"\n',
            "ffi/src/lib.rs": 'pub fn value() -> u64 { 42 }\n',
            "ffi/examples/match_benchmark.rs": r'''use std::io::{self, BufRead};
fn main() {
 println!("{{\"event\":\"ready\"}}");
 for line in io::stdin().lock().lines() {
  let line = line.unwrap();
  assert!(line.starts_with("{\"seeds\":["));
  let tested = line.matches(',').count() + 1;
  println!("{{\"elapsed_ns\":100,\"tested\":{},\"matches\":[],\"recipes\":[],\"witnesses\":[]}}", tested);
 }
}
''',
            "core/Cargo.toml": '[package]\nname = "shpd-seedfinder-core"\nversion = "0.1.0"\nedition = "2021"\n',
            "core/src/lib.rs": '#[test]\nfn smoke() { assert_eq!(2 + 2, 4); }\n',
            "core/examples/equivalence.rs": 'fn main() { println!("equivalent"); }\n',
        }
        for name, text in files.items():
            path = cls.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        def run(argv):
            return subprocess.check_output(argv, cwd=cls.source, stderr=subprocess.STDOUT, text=True)
        run([str(Path(cls.toolchain) / "bin/cargo"), "generate-lockfile", "--offline"])
        run([str(Path(cls.toolchain) / "bin/cargo"), "fmt", "--all"])
        run(["/usr/bin/git", "init", "-q"])
        run(["/usr/bin/git", "add", "."])
        run(["/usr/bin/git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "fixture"])
        cls.sha = run(["/usr/bin/git", "rev-parse", "HEAD"]).strip()

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_actual_sandbox_boundaries_and_toolchain(self):
        self.assertTrue(doctor(self.config)["ok"])

    def test_offline_build_freeze_persistent_comparison_and_artifact(self):
        spec = benchmark("seedfinder", self.sha, self.sha, query={}, seed_range={"start": 123, "count": 4096},
                         seed_count=3, warmups=1, samples=2)
        root = self.base / "benchmark-job"
        runner = Runner(Policy(self.config), root, spec, threading.Event())
        try:
            result = runner.run()
        except Exception:
            for path in root.rglob("*.stderr*"):
                text = path.read_text(errors="replace")
                if text:
                    print(f"\n{path.relative_to(root)}:\n{text}")
            raise
        self.assertEqual({"baseline", "candidate"}, set(result["frozen"]))
        binary = root / "artifacts/frozen/baseline/seed-seeker"
        self.assertEqual(0o555, binary.stat().st_mode & 0o777)
        records = [json.loads(line) for line in (root / "artifacts/results/match_benchmark.jsonl").read_text().splitlines()]
        samples = [r for r in records if r["kind"] == "sample"]
        self.assertEqual(4 * len(set([1, *runner.cores.values()])), len(samples))
        self.assertEqual(["baseline", "candidate", "candidate", "baseline"], [r["variant"] for r in samples[:4]])
        self.assertTrue(all(r["response"]["tested"] == 4096 for r in samples))
        archive = self.base / "benchmark.tar.gz"
        manifest = pack_artifacts(root, archive)
        with tarfile.open(archive) as tar:
            self.assertIn("environment-before.json", tar.getnames())
            self.assertIn("environment-after.json", tar.getnames())
            self.assertIn("artifact-manifest.json", tar.getnames())
            self.assertTrue(all(m.isfile() and not m.name.startswith("/") for m in tar))
        self.assertGreater(len(manifest), 10)

    def test_correctness_and_focused_test(self):
        for label, spec in (("correctness", correctness("seedfinder", self.sha)),
                            ("focused", correctness("seedfinder", self.sha, "smoke"))):
            runner = Runner(Policy(self.config), self.base / label, spec, threading.Event())
            try:
                self.assertGreater(runner.run()["steps_completed"], 0)
            except Exception:
                for path in runner.root.rglob("*.stderr*"):
                    text = path.read_text(errors="replace")
                    if text:
                        print(f"\n{path.relative_to(runner.root)}:\n{text}")
                raise

    def test_full_worker_delivery_over_http(self):
        state = self.base / "delivery-worker"
        token = self.base / "worker.token"
        token.write_text("w" * 48)
        token.chmod(0o600)
        store = Store(self.base / "delivery-server")
        server = Server(("127.0.0.1", 0), store, "s" * 48, "w" * 48)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            submitted = store.submit(correctness("seedfinder", self.sha, "smoke"), "delivery")
            claim = store.claim("fixture", ["seedfinder"])
            config = {**self.config, "state_dir": str(state), "server_url": f"http://127.0.0.1:{server.server_port}",
                      "token_file": str(token), "worker_id": "fixture", "min_free_bytes": 0}
            worker = Worker(config)
            worker.handle(claim)
            finished = store.get(submitted["id"])
            self.assertEqual("succeeded", finished["status"], finished["result"])
            self.assertTrue(finished["has_artifact"])
            self.assertTrue(store.logs(submitted["id"], -1))
            receipt = json.loads((state / (submitted["id"] + ".result.json")).read_text())
            self.assertTrue(receipt["delivered"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_pgo_build_training_merge_and_rebuild(self):
        llvm = Path(self.toolchain) / "lib/rustlib/aarch64-apple-darwin/bin/llvm-profdata"
        if not llvm.is_file():
            self.skipTest("llvm-tools-preview is not installed")
        config = {**self.config, "capabilities": ["cargo", "benchmark", "inspect", "pgo"]}
        spec = pgo("seedfinder", self.sha, query={}, seeds=[1, 2, 3], seed_count=3, warmups=0, samples=1)
        root = self.base / "pgo-job"
        runner = Runner(Policy(config), root, spec, threading.Event())
        try:
            result = runner.run()
        except Exception:
            for path in root.rglob("*.stderr*"):
                text = path.read_text(errors="replace")
                if text:
                    print(f"\n{path.relative_to(root)}:\n{text}")
            raise
        self.assertEqual({"baseline", "candidate", "training"}, set(result["frozen"]))
        self.assertGreater((root / "artifacts/pgo/merged.profdata").stat().st_size, 0)
        self.assertTrue(list((root / "work/pgo/raw").glob("*.profraw")))

    def test_profiling_build_preserves_symbols(self):
        config = {**self.config, "capabilities": ["cargo", "benchmark", "profiling"]}
        spec = benchmark("seedfinder", self.sha, self.sha, query={}, seeds=[1], profiling=True, warmups=0, samples=1)
        spec["steps"] = spec["steps"][:2]
        spec["sources"] = {"baseline": self.sha}
        root = self.base / "profiling-job"
        runner = Runner(Policy(config), root, spec, threading.Event())
        try:
            runner.run()
        except Exception:
            for path in root.rglob("*.stderr*"):
                text = path.read_text(errors="replace")
                if text:
                    print(f"\n{path.relative_to(root)}:\n{text}")
            raise
        frozen = root / "artifacts/frozen/baseline"
        self.assertTrue((frozen / "equivalence").is_file())
        # The worker packs unpacked DWARF into standalone bundles when necessary.
        self.assertTrue(list(frozen.glob("*.dSYM")))
