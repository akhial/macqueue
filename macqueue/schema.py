"""Strict wire format. The Mac also applies its local capability/tool policy."""
import json
import re

from .common import integer, keys, name, relative, require

TARGET = "aarch64-apple-darwin"
MAX_JOB_BYTES = 32 * 1024 * 1024
MAX_EXPLICIT_SEEDS = 1048576
MAX_RANGE_SEEDS = 1048576


def seed_request(value, *, allow_range=True):
    """Validate the compact job description without allocating an expanded range."""
    require(isinstance(value, dict), "matching request must be an object")
    if "seed_range" in value:
        require(allow_range, "seed_range requires a structured compare or train step")
        keys(value, ("seed_range",))
        item = value["seed_range"]
        keys(item, ("start", "count"))
        integer(item["start"], 0, 2**64 - 1, "seed range start")
        integer(item["count"], 1, MAX_RANGE_SEEDS, "seed range count")
        require(item["start"] + item["count"] <= 2**64, "seed range exceeds uint64")
    else:
        keys(value, ("seeds",))
        require(isinstance(value["seeds"], list) and 1 <= len(value["seeds"]) <= MAX_EXPLICIT_SEEDS,
                f"seeds must contain 1..{MAX_EXPLICIT_SEEDS} values; use seed_range for larger comparisons")
        for seed in value["seeds"]:
            integer(seed, 0, 2**64 - 1, "seed")
    return value


def expand_seed_request(value):
    seed_request(value)
    if "seeds" in value:
        return value
    item = value["seed_range"]
    return {"seeds": list(range(item["start"], item["start"] + item["count"]))}


BUILD = ["cargo", "build", "--locked", "--offline", "--release", "--target", TARGET,
         "-p", "shpd-seedfinder-cli", "-p", "shpd-seedfinder-ffi",
         "--bin", "seed-seeker", "--example", "match_benchmark"]
PROFILE_BUILD = ["cargo", "build", "--locked", "--offline", "--profile", "profiling", "--target", TARGET,
                 "-p", "shpd-seedfinder-cli", "-p", "shpd-seedfinder-ffi", "-p", "shpd-seedfinder-core",
                 "--bin", "seed-seeker", "--example", "match_benchmark", "--example", "equivalence"]
FMT = ["cargo", "fmt", "--all", "--", "--check"]
CLIPPY = ["cargo", "clippy", "--locked", "--offline", "--workspace", "--exclude",
          "shpd-seedfinder-gtk", "--all-targets", "--", "-D", "warnings"]
TEST = ["cargo", "test", "--locked", "--offline", "--workspace", "--exclude",
        "shpd-seedfinder-gtk", "--release"]
FOCUSED_TEST = ["cargo", "test", "--locked", "--offline", "-p", "shpd-seedfinder-core", "--release"]


def output(value):
    relative(value)
    require(value.startswith("artifacts/") and not value.startswith("artifacts/frozen/"),
            "output must be below artifacts/, outside frozen/")
    require(value not in ("artifacts/job.json", "artifacts/worker-result.json", "artifacts/artifact-manifest.json")
            and not value.startswith(("artifacts/internal/", "artifacts/environment-")), "output path is reserved for worker records")


def command(value, *, template=False):
    keys(value, ("argv", "cwd", "env", "stdin", "timeout_seconds", "stdout", "stderr"), ("wrappers",))
    argv = value["argv"]
    require(isinstance(argv, list) and 1 <= len(argv) <= 100, "argv must contain 1..100 arguments")
    require(all(isinstance(a, str) and "\x00" not in a and len(a) <= 65536 for a in argv), "invalid argv")
    relative(value["cwd"])
    require(value["cwd"].startswith("work/"), "cwd must be under work/")
    require(isinstance(value["env"], dict) and all(isinstance(k, str) and isinstance(v, str)
            and "\x00" not in v and len(v) <= 1024 for k, v in value["env"].items()), "invalid env")
    require(isinstance(value["stdin"], str) and len(value["stdin"].encode()) <= 65536, "stdin exceeds 64 KiB")
    integer(value["timeout_seconds"], 1, 86400, "command timeout")
    output(value["stdout"])
    output(value["stderr"])
    require(value["stdout"] != value["stderr"], "stdout and stderr paths must differ")
    wrappers = value.get("wrappers", [])
    require(isinstance(wrappers, list) and len(wrappers) == len(set(wrappers)) and
            all(w in ("time", "caffeinate") for w in wrappers), "unsupported execution wrapper")
    if not template:
        require(not any("${WORKERS}" in a for a in argv), "WORKERS is only valid in compare commands")


def validate_job(job):
    keys(job, ("version", "project", "sources", "steps"),
         ("label", "timeout_seconds", "expires_in_seconds"))
    require(job["version"] == 1 and type(job["version"]) is int, "unsupported job version")
    name(job["project"])
    if "label" in job:
        require(isinstance(job["label"], str) and len(job["label"]) <= 200, "invalid label")
    integer(job.get("timeout_seconds", 14400), 1, 86400, "job timeout")
    integer(job.get("expires_in_seconds", 86400), 1, 604800, "queue expiry")
    require(isinstance(job["sources"], dict) and 1 <= len(job["sources"]) <= 4, "expected 1..4 sources")
    for variant, commit in job["sources"].items():
        name(variant)
        require(isinstance(commit, str) and re.fullmatch(r"[a-fA-F0-9]{40}", commit), "use full 40-character commit SHA")
    require(isinstance(job["steps"], list) and 1 <= len(job["steps"]) <= 100, "expected 1..100 steps")
    ids = set()
    for step in job["steps"]:
        require(isinstance(step, dict) and "id" in step and "op" in step, "step needs id and op")
        name(step["id"])
        require(step["id"] not in ids, "duplicate step id")
        ids.add(step["id"])
        op = step["op"]
        base = ("id", "op")
        if op == "exec":
            keys(step, (*base, "command"))
            command(step["command"])
        elif op == "metadata":
            keys(step, base)
        elif op == "mkdir":
            keys(step, (*base, "path"))
            relative(step["path"])
            require(step["path"].startswith(("work/", "artifacts/")) and
                    not step["path"].startswith("artifacts/frozen"), "invalid writable path")
        elif op == "write":
            keys(step, (*base, "path", "text"))
            output(step["path"])
            require(isinstance(step["text"], str) and len(step["text"].encode()) <= 65536, "write exceeds 64 KiB")
        elif op == "copy":
            keys(step, (*base, "source", "destination"))
            relative(step["source"])
            require(step["source"].startswith(("work/", "artifacts/")), "invalid copy source")
            output(step["destination"])
        elif op == "freeze":
            keys(step, (*base, "variant", "target_dir", "profile"))
            name(step["variant"])
            relative(step["target_dir"])
            require(step["target_dir"].startswith("work/targets/"), "invalid target directory")
            require(step["profile"] in ("release", "profiling"), "invalid build profile")
        elif op == "compare":
            keys(step, (*base, "mode", "commands", "worker_counts", "warmups", "samples", "requests", "output"), ("ready",))
            require(step["mode"] in ("process", "jsonl"), "invalid benchmark mode")
            keys(step["commands"], ("baseline", "candidate"))
            for cmd in step["commands"].values():
                command(cmd, template=True)
                require(cmd["stdin"] == "", "compare supplies stdin; command stdin must be empty")
                require("${WORKERS}" in cmd["argv"], "compare argv must include WORKERS")
            counts = step["worker_counts"]
            require(isinstance(counts, list) and 1 <= len(counts) <= 16, "invalid worker counts")
            for count in counts:
                require(count in ("performance", "available") or type(count) is int and 1 <= count <= 1024,
                        "invalid worker count")
            integer(step["warmups"], 0, 100, "warmups")
            integer(step["samples"], 1, 1000, "samples")
            require(isinstance(step["requests"], list) and len(step["requests"]) <= 100, "invalid requests")
            if step["mode"] == "jsonl":
                require(bool(step["requests"]), "jsonl mode requires seeds requests")
                for request in step["requests"]:
                    seed_request(request)
            else:
                require(not step["requests"], "process mode does not accept requests")
            if "ready" in step:
                keys(step["ready"], ("field", "equals"))
                require(isinstance(step["ready"]["field"], str), "invalid readiness field")
                require(isinstance(step["ready"]["equals"], (str, int, bool)), "invalid readiness value")
            output(step["output"])
        elif op == "train":
            keys(step, (*base, "command", "requests", "output"), ("ready",))
            command(step["command"])
            require(step["command"]["stdin"] == "", "train supplies stdin")
            require(isinstance(step["requests"], list) and 1 <= len(step["requests"]) <= 100, "expected 1..100 training requests")
            for request in step["requests"]:
                seed_request(request)
            if "ready" in step:
                keys(step["ready"], ("field", "equals"))
                require(isinstance(step["ready"]["field"], str), "invalid readiness field")
            output(step["output"])
        elif op == "pgo_import":
            keys(step, (*base, "source", "sha256"))
            require(step["source"] == f"work/checkouts/candidate/pgo/seed-seeker-{TARGET}.profdata", "only the checked-in target profile may be imported")
            require(isinstance(step["sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", step["sha256"]), "profile requires exact SHA-256")
        elif op == "pgo_merge":
            keys(step, (*base, "raw_dir", "output"))
            require(step["raw_dir"] == "work/pgo/raw" and step["output"] == "work/pgo/merged.profdata",
                    "PGO paths are fixed below work/pgo/")
        elif op == "profile":
            keys(step, (*base, "tool", "command", "duration_seconds", "output"), ("template",))
            require(step["tool"] in ("sample", "xctrace"), "invalid profiling tool")
            command(step["command"])
            integer(step["duration_seconds"], 1, 600, "profiling duration")
            output(step["output"])
            require(step.get("template", "Time Profiler") in ("Time Profiler", "CPU Profiler"), "invalid trace template")
        else:
            require(False, f"unknown operation: {op}")
    require(len(json.dumps(job).encode()) <= MAX_JOB_BYTES, "job exceeds 32 MiB")
    return job
