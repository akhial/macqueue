"""Trusted local installer check, never accepted as a remote job script."""
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from macqueue.plans import benchmark, correctness
from macqueue.policy import Policy
from macqueue.runner import Runner, pack_artifacts
from macqueue.worker import load_config


def smoke(config_path):
    config = load_config(config_path)
    policy = Policy(config)
    files = {
        "Cargo.toml": '[workspace]\nmembers = ["cli", "ffi", "core"]\nresolver = "2"\n[profile.release]\nopt-level = 3\nlto = "fat"\ncodegen-units = 1\n',
        "cli/Cargo.toml": '[package]\nname = "shpd-seedfinder-cli"\nversion = "0.1.0"\nedition = "2021"\n[[bin]]\nname = "seed-seeker"\npath = "src/main.rs"\n',
        "cli/src/main.rs": 'fn main() { println!("benchmark completed"); }\n',
        "ffi/Cargo.toml": '[package]\nname = "shpd-seedfinder-ffi"\nversion = "0.1.0"\nedition = "2021"\n',
        "ffi/src/lib.rs": 'pub fn value() -> u64 { 42 }\n',
        "ffi/examples/match_benchmark.rs": 'use std::io::{self, BufRead};\nfn main() {\n println!("{{\\"event\\":\\"ready\\"}}");\n for line in io::stdin().lock().lines() {\n  let _ = line.unwrap();\n  println!("{{\\"elapsed_ns\\":100,\\"tested\\":3,\\"matches\\":[],\\"recipes\\":[],\\"witnesses\\":[]}}");\n }\n}\n',
        "core/Cargo.toml": '[package]\nname = "shpd-seedfinder-core"\nversion = "0.1.0"\nedition = "2021"\n',
        "core/src/lib.rs": '#[test]\nfn smoke() { assert_eq!(2 + 2, 4); }\n',
    }
    with tempfile.TemporaryDirectory(prefix="installation-smoke-", dir=config["state_dir"]) as temp:
        root = Path(temp)
        source = root / "source"
        source.mkdir()
        for name, text in files.items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        provision = root / "provision"
        for directory in ("work/home", "work/tmp", "work/targets"):
            (provision / directory).mkdir(parents=True, exist_ok=True)
        env = policy.environment(provision, {"CARGO_TARGET_DIR": "${JOB}/work/targets/provision"})
        def run(argv):
            result = subprocess.run(list(map(str, argv)), cwd=source, env=env, capture_output=True, text=True, timeout=120)
            if result.returncode:
                raise RuntimeError(result.stderr or result.stdout)
            return result.stdout.strip()
        run([policy.tools["cargo"], "generate-lockfile", "--offline"])
        run([policy.tools["cargo"], "fmt", "--all"])
        run([policy.tools["git"], "init", "-q"])
        run([policy.tools["git"], "add", "."])
        run([policy.tools["git"], "-c", "user.name=Macqueue setup", "-c", "user.email=setup@example.invalid",
             "-c", "core.hooksPath=/dev/null", "commit", "-qm", "local fixture"])
        sha = run([policy.tools["git"], "rev-parse", "HEAD"])
        fixture = {**config, "projects": {"seedfinder": str(source)}}
        plans = [("correctness", correctness("seedfinder", sha)),
                 ("benchmark", benchmark("seedfinder", sha, sha, query={}, seeds=[123, 456, 789],
                                          seed_count=3, warmups=0, samples=1))]
        results = {}
        for label, spec in plans:
            job_root = root / label
            runner = Runner(Policy(fixture), job_root, spec, threading.Event())
            try:
                results[label] = runner.run()
            except Exception:
                for path in job_root.rglob("*.stderr*"):
                    text = path.read_text(errors="replace")
                    if text:
                        print(str(path.relative_to(root)) + ":\n" + text, file=sys.stderr)
                raise
        manifest = pack_artifacts(root / "benchmark", root / "artifacts.tar.gz")
        return {"ok": True, "uid": os.getuid(), "groups": os.getgroups(), "offline_cargo": True,
                "correctness_steps": results["correctness"]["steps_completed"],
                "benchmark_variants": list(results["benchmark"]["frozen"]), "artifact_files": len(manifest)}


if __name__ == "__main__":
    print(json.dumps(smoke(sys.argv[1]), indent=2))

