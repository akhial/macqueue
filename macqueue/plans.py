"""Generate reviewable structured JSON; no uploaded scripts or shell fragments."""
import json

from .common import require
from .schema import BUILD, CLIPPY, FMT, FOCUSED_TEST, PROFILE_BUILD, TEST, seed_request, validate_job


def command(argv, variant="candidate", *, target=None, timeout=3600, log=None, env=None):
    log = log or variant + "-" + argv[0].split("/")[-1]
    return {"argv": list(argv), "cwd": f"work/checkouts/{variant}",
            "env": env if env is not None else {"CARGO_TARGET_DIR": "${JOB}/work/targets/" + (target or variant), "RUSTFLAGS": ""},
            "stdin": "", "timeout_seconds": timeout,
            "stdout": f"artifacts/logs/{log}.stdout", "stderr": f"artifacts/logs/{log}.stderr"}


def build_steps(variant, *, profiling=False, target=None, flags=""):
    target = target or variant
    env = {"CARGO_TARGET_DIR": "${JOB}/work/targets/" + target, "RUSTFLAGS": flags}
    if "profile-generate" in flags:
        env["LLVM_PROFILE_FILE"] = "${JOB}/work/pgo/raw/%m-%p.profraw"
    return [{"id": "build-" + variant, "op": "exec", "command": command(PROFILE_BUILD if profiling else BUILD,
            variant, target=target, env=env)},
            {"id": "freeze-" + variant, "op": "freeze", "variant": variant,
             "target_dir": "work/targets/" + target, "profile": "profiling" if profiling else "release"}]


def compare_step(mode, query, request, seed_count, workers, warmups, samples):
    commands = {}
    binary = "match_benchmark" if mode == "jsonl" else "seed-seeker"
    for variant in ("baseline", "candidate"):
        argv = [f"${{JOB}}/artifacts/frozen/{variant}/{binary}"]
        argv += [json.dumps(query, separators=(",", ":")), "${WORKERS}"] if mode == "jsonl" else ["--benchmark", str(seed_count), "--workers", "${WORKERS}"]
        commands[variant] = command(argv, variant, timeout=300, log=f"{variant}-{binary}", env={})
    return {"id": "compare-" + binary, "op": "compare", "mode": mode, "commands": commands,
            "worker_counts": workers, "warmups": warmups, "samples": samples,
            "requests": [request] if mode == "jsonl" else [],
            "output": f"artifacts/results/{binary}.jsonl"}


def benchmark(project, baseline, candidate, *, query, seeds=None, seed_range=None, seed_count=100000, warmups=2, samples=10, profiling=False):
    require(isinstance(query, dict), "benchmark query must be a JSON object")
    require((seeds is None) != (seed_range is None), "provide either seeds or seed_range")
    request = seed_request({"seed_range": seed_range} if seed_range is not None else {"seeds": seeds})
    spec = {"version": 1, "project": project, "label": "baseline/candidate benchmark",
            "sources": {"baseline": baseline, "candidate": candidate}, "timeout_seconds": 14400,
            "expires_in_seconds": 86400, "steps": []}
    for variant in ("baseline", "candidate"):
        spec["steps"].extend(build_steps(variant, profiling=profiling))
    for mode in ("process", "jsonl"):
        spec["steps"].append(compare_step(mode, query, request, seed_count, [1, "performance", "available"], warmups, samples))
    return validate_job(spec)


def correctness(project, commit, test_filter=None):
    argv_sets = [("fmt", FMT), ("clippy", CLIPPY), ("test", TEST)] if test_filter is None else [("focused-test", [*FOCUSED_TEST, test_filter])]
    return validate_job({"version": 1, "project": project, "sources": {"candidate": commit},
                         "steps": [{"id": name, "op": "exec", "command": command(argv, log=name)} for name, argv in argv_sets]})


def pgo(project, commit, *, query, seeds, seed_count=100000, warmups=2, samples=10):
    """Train a separate instrumented executable, then compare ordinary vs PGO builds."""
    spec = benchmark(project, commit, commit, query=query, seeds=seeds, seed_count=seed_count, warmups=warmups, samples=samples)
    spec["label"] = "PGO training and comparison"
    steps = build_steps("baseline")
    training = build_steps("training", flags="-Cprofile-generate=${JOB}/work/pgo/raw")
    # Compile training and optimized code from the same source path so Rust's
    # crate identity and LLVM profile names remain stable across the two builds.
    training[0]["command"]["cwd"] = "work/checkouts/candidate"
    steps.extend(training)
    argv = ["${JOB}/artifacts/frozen/training/match_benchmark", json.dumps(query, separators=(",", ":")), "1"]
    train = command(argv, "candidate", timeout=600, log="pgo-training", env={"LLVM_PROFILE_FILE": "${JOB}/work/pgo/raw/%m-%p.profraw"})
    train["stdin"] = json.dumps({"seeds": seeds}, separators=(",", ":")) + "\n"
    steps.append({"id": "train", "op": "exec", "command": train})
    steps.append({"id": "merge", "op": "pgo_merge", "raw_dir": "work/pgo/raw", "output": "work/pgo/merged.profdata"})
    steps.append({"id": "preserve-profile", "op": "copy", "source": "work/pgo/merged.profdata", "destination": "artifacts/pgo/merged.profdata"})
    steps.extend(build_steps("candidate", flags="-Cprofile-use=${JOB}/work/pgo/merged.profdata"))
    steps.extend(spec["steps"][-2:])
    spec["steps"] = steps
    return validate_job(spec)
