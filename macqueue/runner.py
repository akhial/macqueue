import copy
import json
import shutil
import stat
import tarfile
import time
from pathlib import Path

from .common import Invalid, digest, json_bytes, require, safe_path
from .process import Process, Stopped
from .schema import TARGET, expand_seed_request


class Runner:
    def __init__(self, policy, root, spec, cancel, event=lambda _: None, *, sandboxed=True):
        self.policy, self.root, self.spec = policy, Path(root).resolve(), spec
        self.cancel, self.event = cancel, event
        self.sandboxed = sandboxed
        self.deadline = time.monotonic() + spec.get("timeout_seconds", 14400)
        self.sequence = 0
        self.frozen = {}
        self.cores = {}

    def check(self):
        if self.cancel.is_set():
            raise Stopped("job cancelled or worker lease lost")
        if time.monotonic() >= self.deadline:
            raise Stopped("job timed out")

    def path(self, value, exists=False):
        return safe_path(self.root, value, exists=exists)

    def write(self, path, data):
        self.check()
        dest = self.path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("xb") as stream:
            stream.write(data)

    def start(self, cmd, *, workers=None, persistent=False, extra_read=(), writable=(), internal_argv=None):
        self.check()
        argv = internal_argv or self.policy.argv(cmd, self.root, workers)
        stdout, stderr = self.path(cmd["stdout"]), self.path(cmd["stderr"])
        for value in cmd["env"].values():
            if value.startswith("${JOB}/"):
                self.path(value[len("${JOB}/"):])
        cwd = self.path(cmd["cwd"], exists=True)
        require(cwd.is_dir(), "cwd must be a directory")
        env = self.policy.environment(self.root, cmd["env"])
        sandbox = self.policy.sandbox(self.root, extra_read=extra_read, writable=[stdout, stderr, *writable]) if self.sandboxed else None
        self.event({"event": "command", "argv": argv, "cwd": cmd["cwd"], "env": cmd["env"],
                    "stdout": cmd["stdout"], "stderr": cmd["stderr"]})
        deadline = self.deadline if persistent else min(self.deadline, time.monotonic() + cmd["timeout_seconds"])
        return Process(argv, cwd, env, stdout, stderr, sandbox=sandbox, cancel=self.cancel, deadline=deadline,
                       event=self.event, max_output=self.policy.config.get("max_output_bytes", 32 * 1024 * 1024))

    def execute(self, cmd, **kwargs):
        with self.start(cmd, **kwargs) as process:
            return process.run(cmd["stdin"])

    def internal(self, argv, *, cwd="work", label="internal", timeout=120, extra_read=(), env=None, writable=()):
        self.sequence += 1
        stem = f"artifacts/internal/{self.sequence:03d}-{label}"
        cmd = {"argv": argv, "cwd": cwd, "env": env or {}, "stdin": "", "timeout_seconds": timeout,
               "stdout": stem + ".stdout", "stderr": stem + ".stderr"}
        self.execute(cmd, internal_argv=argv, extra_read=extra_read, writable=writable)
        return self.path(cmd["stdout"]).read_text(errors="replace")

    def prepare(self):
        self.policy.validate(self.spec)
        self.root.mkdir(parents=True, mode=0o700, exist_ok=False)
        for directory in ("work/home", "work/tmp", "work/checkouts", "work/targets", "work/pgo/raw",
                          "artifacts/frozen", "artifacts/internal"):
            self.path(directory).mkdir(parents=True, exist_ok=True)
        self.write("artifacts/job.json", json_bytes(self.spec))
        mirror = Path(self.policy.config["projects"][self.spec["project"]]).resolve()
        require(mirror.is_dir(), "configured repository mirror does not exist")
        # Git's upload-pack subprocess discards command-scoped safe.directory.
        # Give only these clone processes an isolated config naming the trusted,
        # administrator-owned mirror. No user or system Git config is changed.
        self.write("work/mirror.gitconfig", ("[safe]\n\tdirectory = " +
                   json.dumps(str(mirror), ensure_ascii=False) + "\n").encode())
        git = str(self.policy.tools["git"])
        for variant, sha in self.spec["sources"].items():
            # No remote URL, fetch, hooks, submodule update, or arbitrary revision expression.
            self.internal([git, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "clone",
                           "--no-local", "--no-checkout", "--config", "core.hooksPath=/dev/null",
                           "--config", "core.fsmonitor=false", str(mirror), str(self.path(f"work/checkouts/{variant}"))],
                          label=f"clone-{variant}", extra_read=[mirror],
                          env={"GIT_CONFIG_GLOBAL": "${JOB}/work/mirror.gitconfig"})
            cwd = f"work/checkouts/{variant}"
            actual = self.internal([git, "rev-parse", "--verify", sha + "^{commit}"], cwd=cwd, label=f"resolve-{variant}").strip()
            require(actual.lower() == sha.lower(), "requested revision is not an exact commit")
            self.internal([git, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "checkout", "--detach", sha],
                          cwd=cwd, label=f"checkout-{variant}")
        self.metadata("before")

    def metadata(self, label):
        from . import __version__
        info = {"runner": {"version": __version__, "files": {
            path.name: digest(path) for path in sorted(Path(__file__).parent.glob("*.py"))}}}
        info["developer_dir"] = str(self.policy.developer) if self.policy.developer else None
        info["sdk"] = self.policy.sdk
        commands = {"rustc": [str(self.policy.tools["rustc"]), "-Vv"],
                    "cargo": [str(self.policy.tools["cargo"]), "--version"],
                    "architecture": ["/usr/bin/uname", "-m"], "macos": ["/usr/bin/sw_vers"],
                    "hardware": ["/usr/sbin/sysctl", "-n", "hw.model", "hw.physicalcpu", "hw.logicalcpu", "hw.memsize"],
                    "performance_cores": ["/usr/sbin/sysctl", "-n", "hw.perflevel0.physicalcpu"],
                    "available_cores": ["/usr/sbin/sysctl", "-n", "hw.activecpu"]}
        for key, argv in commands.items():
            try:
                info[key] = self.internal(argv, label=key).strip()
            except Invalid as exc:
                info[key] = {"unavailable": str(exc)}
        for key, field in (("performance", "performance_cores"), ("available", "available_cores")):
            if isinstance(info[field], str) and info[field].isdecimal():
                self.cores[key] = int(info[field])
        git = str(self.policy.tools["git"])
        info["sources"] = {}
        for variant in self.spec["sources"]:
            cwd = f"work/checkouts/{variant}"
            info["sources"][variant] = {
                "commit": self.internal([git, "rev-parse", "HEAD"], cwd=cwd, label=f"head-{variant}").strip(),
                "status": self.internal([git, "-c", "core.fsmonitor=false", "status", "--porcelain"], cwd=cwd, label=f"status-{variant}")}
            self.internal([git, "--no-pager", "diff", "--no-ext-diff", "--no-textconv", "--binary"], cwd=cwd, label=f"diff-{variant}")
        info["frozen"] = self.frozen
        info["recorded_at"] = time.time()
        self.write(f"artifacts/environment-{label}.json", json_bytes(info))

    def copy(self, source, destination):
        self.check()
        source = self.path(source, exists=True)
        destination = self.path(destination)
        require(not destination.is_relative_to(source), "cannot copy a directory into itself")
        require(not destination.exists(), "copy destination already exists")
        if source.is_dir():
            destination.mkdir(parents=True)
            for child in sorted(source.iterdir()):
                self.copy(str(child.relative_to(self.root)), str((destination / child.name).relative_to(self.root)))
        else:
            require(stat.S_ISREG(source.stat().st_mode), "can only copy regular files and directories")
            require(source.stat().st_size <= self.policy.config.get("max_artifact_bytes", 512 * 1024 * 1024), "copy exceeds artifact size limit")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as src, destination.open("xb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            destination.chmod(stat.S_IMODE(source.stat().st_mode) & 0o777)

    def freeze(self, step):
        variant = step["variant"]
        target = f"{step['target_dir']}/{TARGET}/{step['profile']}"
        dest = f"artifacts/frozen/{variant}"
        require(not self.path(dest).exists(), "frozen variant already exists")
        self.path(dest).mkdir()
        items = [(f"{target}/seed-seeker", "seed-seeker"), (f"{target}/examples/match_benchmark", "match_benchmark")]
        if step["profile"] == "profiling":
            items.append((f"{target}/examples/equivalence", "equivalence"))
        manifest = {}
        for source, binary in items:
            self.copy(source, f"{dest}/{binary}")
            if step["profile"] == "profiling" and not self.path(source + ".dSYM").exists():
                # Darwin can leave DWARF in build objects (unpacked debug info).
                # Materialize a standalone symbol bundle before discarding targets.
                symbols = f"work/symbols/{variant}/{binary}.dSYM"
                self.path(symbols).parent.mkdir(parents=True, exist_ok=True)
                self.internal([str(self.policy.tools["dsymutil"]), str(self.path(source)), "-o", str(self.path(symbols))],
                              label=f"symbols-{variant}-{binary}", timeout=600)
                self.copy(symbols, f"{dest}/{binary}.dSYM")
            for extension in (".dSYM", ".dwp", ".pdb"):
                if self.path(source + extension).exists():
                    self.copy(source + extension, f"{dest}/{binary}{extension}")
            path = self.path(f"{dest}/{binary}")
            path.chmod(0o555)
            manifest[binary] = {"sha256": digest(path), "bytes": path.stat().st_size}
        self.write(f"{dest}/manifest.json", json_bytes(manifest))
        for path in self.path(dest).rglob("*"):
            require(not path.is_symlink(), "symlink in frozen output")
            path.chmod(0o555 if path.is_dir() or path.name in manifest else 0o444)
        self.path(dest).chmod(0o555)
        self.frozen[variant] = manifest
        self.event({"event": "frozen", "variant": variant, "executables": manifest})

    @staticmethod
    def sample_command(original, suffix):
        cmd = copy.deepcopy(original)
        for output in ("stdout", "stderr"):
            cmd[output] += "." + suffix
        return cmd

    def compare(self, step):
        counts = []
        for count in step["worker_counts"]:
            if isinstance(count, str):
                require(count in self.cores, f"could not determine {count} core count")
                count = self.cores[count]
            if count not in counts:
                counts.append(count)
        for variant, cmd in step["commands"].items():
            binary = cmd["argv"][0].split("/")[-1]
            require(variant in self.frozen and binary in self.frozen[variant], "benchmark requires binaries frozen in this job")
            require(digest(self.path(f"artifacts/frozen/{variant}/{binary}")) == self.frozen[variant][binary]["sha256"],
                    "frozen binary checksum changed")
        path = self.path(step["output"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as output:
            def record(data):
                output.write(json_bytes(data) + b"\n")
                output.flush()
                self.event({"event": "benchmark", **data})
            for workers in counts:
                sessions = {}
                try:
                    if step["mode"] == "jsonl":
                        for variant, cmd in step["commands"].items():
                            session = self.start(self.sample_command(cmd, f"w{workers}"), workers=workers, persistent=True)
                            sessions[variant] = session
                            session.keep_lines = True
                            ready = session.record(cmd["timeout_seconds"])
                            if "ready" in step:
                                require(ready.get(step["ready"]["field"]) == step["ready"]["equals"], "unexpected readiness record")
                            record({"kind": "ready", "variant": variant, "workers": workers, "record": ready})
                    requests = step["requests"] or [None]
                    for phase, rounds in (("warmup", step["warmups"]), ("sample", step["samples"])):
                        for index in range(rounds):
                            order = ["baseline", "candidate"] if index % 2 == 0 else ["candidate", "baseline"]
                            for request_index, request in enumerate(requests):
                                # Expand once per pair, outside adapter timing. Keep the
                                # compact descriptor in artifacts and send identical
                                # ordinary seeds arrays to both existing executables.
                                expanded = expand_seed_request(request) if sessions else None
                                for variant in order:
                                    self.check()
                                    cmd = step["commands"][variant]
                                    if sessions:
                                        result = sessions[variant].request(expanded, cmd["timeout_seconds"])
                                    else:
                                        suffix = f"w{workers}-{phase}-{index}"
                                        sampled = self.sample_command(cmd, suffix)
                                        result = self.execute(sampled, workers=workers)
                                        result["stdout"] = sampled["stdout"]
                                        result["stderr"] = sampled["stderr"]
                                    record({"kind": phase, "sample": index, "order": order, "variant": variant,
                                            "workers": workers, "request_index": request_index, "request": request,
                                            "identity": self.frozen[variant], **result})
                    for session in sessions.values():
                        session.finish_session()
                finally:
                    for session in sessions.values():
                        session.close()

    def profile(self, step):
        output = self.path(step["output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        require(not output.exists(), "profiling output already exists")
        cmd = step["command"]
        if step["tool"] == "xctrace":
            argv = ["/usr/bin/xcrun", "xctrace", "record", "--template", step.get("template", "Time Profiler"),
                    "--time-limit", f"{step['duration_seconds']}s", "--output", str(output), "--launch", "--",
                    *self.policy.argv(cmd, self.root)]
            self.execute(cmd, internal_argv=argv, writable=[output])
        else:
            with self.start(cmd) as target:
                target.proc.stdin.close()
                # Only the PID just spawned by this job is accepted; no remote PID input exists.
                argv = ["/usr/bin/sample", str(target.proc.pid), str(step["duration_seconds"]), "-file", str(output)]
                profile_cmd = self.sample_command(cmd, "sampler")
                with self.start(profile_cmd, internal_argv=argv, writable=[output]) as sampler:
                    sampler.proc.stdin.close()
                    while sampler.selector.get_map() or sampler.proc.poll() is None:
                        target.pump()
                        sampler.pump()
                    require(sampler.proc.wait() == 0, "sample failed")

    def pgo_merge(self, step):
        tool = self.policy.tools["llvm-profdata"]
        require(tool.is_file(), "install llvm-tools-preview in the pinned Rust toolchain")
        raw = self.path(step["raw_dir"], exists=True)
        profiles = [self.path(str(p.relative_to(self.root)), exists=True) for p in sorted(raw.glob("*.profraw"))]
        require(0 < len(profiles) <= 4096 and all(p.is_file() for p in profiles), "expected 1..4096 raw profiles")
        output = self.path(step["output"])
        require(not output.exists(), "merged profile already exists")
        self.internal([str(tool), "--version"], label="llvm-profdata-version")
        self.internal([str(tool), "merge", "-o", str(output), *map(str, profiles)], label="pgo-merge", timeout=600)

    def run(self):
        self.prepare()
        for step in self.spec["steps"]:
            self.check()
            self.event({"event": "step_started", "step": step["id"], "op": step["op"]})
            op = step["op"]
            if op == "exec":
                cmd = step["command"]
                writable = []
                if cmd["argv"][:3] == ["xcrun", "xctrace", "export"]:
                    dest = self.path(cmd["argv"][6][len("${JOB}/"):])
                    require(not dest.exists(), "export destination exists")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    writable.append(dest)
                self.execute(cmd, writable=writable)
            elif op == "metadata":
                self.metadata(step["id"])
            elif op == "mkdir":
                self.path(step["path"]).mkdir(parents=True, exist_ok=True)
            elif op == "write":
                self.write(step["path"], step["text"].encode())
            elif op == "copy":
                self.copy(step["source"], step["destination"])
            elif op == "freeze":
                self.freeze(step)
            elif op == "compare":
                self.compare(step)
            elif op == "profile":
                self.profile(step)
            elif op == "pgo_merge":
                self.pgo_merge(step)
            self.event({"event": "step_finished", "step": step["id"]})
        self.metadata("after")
        return {"frozen": self.frozen, "steps_completed": len(self.spec["steps"])}


def pack_artifacts(root, destination, max_bytes=512 * 1024 * 1024):
    root = Path(root).resolve()
    artifacts = safe_path(root, "artifacts", exists=True)
    manifest = []
    total = 0
    files = []
    for path in sorted(artifacts.rglob("*")):
        safe_path(root, str(path.relative_to(root)), exists=True)
        mode = path.lstat().st_mode
        require(stat.S_ISDIR(mode) or stat.S_ISREG(mode), "artifact contains a special file")
        if path.is_file():
            total += path.stat().st_size
            require(total <= max_bytes, "uncompressed artifacts exceed size limit; retained locally")
            require(len(files) < 50000, "artifact file count limit exceeded")
            files.append(path)
            manifest.append({"path": str(path.relative_to(artifacts)), "bytes": path.stat().st_size, "sha256": digest(path)})
    manifest_path = artifacts / "artifact-manifest.json"
    require(not manifest_path.exists(), "reserved artifact manifest path already exists")
    manifest_path.write_bytes(json_bytes(manifest))
    files.append(manifest_path)
    with tarfile.open(destination, "w:gz", dereference=True) as archive:
        for path in files:
            info = archive.gettarinfo(str(path), arcname=str(path.relative_to(artifacts)))
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode &= 0o777
            with path.open("rb") as stream:
                archive.addfile(info, stream)
    require(Path(destination).stat().st_size <= max_bytes, "compressed artifact exceeds upload limit")
    return manifest
