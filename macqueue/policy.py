import json
import os
import re
import subprocess
from pathlib import Path

from .common import keys, relative, require, safe_path
from .schema import BUILD, CLIPPY, FMT, FOCUSED_TEST, PROFILE_BUILD, TEST, TARGET, seed_request, validate_job


def job_argument(value, prefix):
    require(value.startswith("${JOB}/" + prefix), f"expected a job path below {prefix}")
    return relative(value[len("${JOB}/"):])


class Policy:
    def __init__(self, config):
        self.config = config
        self.capabilities = set(config.get("capabilities", ["cargo", "benchmark", "inspect"]))
        require(self.capabilities <= {"cargo", "benchmark", "inspect", "profiling", "pgo"}, "unknown capability")
        self.toolchain = Path(config["rust_toolchain"]).resolve()
        self.tools = {
            "cargo": self.toolchain / "bin/cargo", "rustc": self.toolchain / "bin/rustc",
            "git": Path("/usr/bin/git"), "uname": Path("/usr/bin/uname"),
            "sw_vers": Path("/usr/bin/sw_vers"), "sysctl": Path("/usr/sbin/sysctl"),
            "xcrun": Path("/usr/bin/xcrun"), "sample": Path("/usr/bin/sample"),
            "nm": Path("/usr/bin/nm"), "size": Path("/usr/bin/size"),
            "time": Path("/usr/bin/time"), "caffeinate": Path("/usr/bin/caffeinate"),
            "dsymutil": Path("/usr/bin/dsymutil"),
            "llvm-objdump": self.toolchain / f"lib/rustlib/{TARGET}/bin/llvm-objdump",
            "llvm-profdata": self.toolchain / f"lib/rustlib/{TARGET}/bin/llvm-profdata",
        }
        self.developer = None
        self.sdk = None
        if os.uname().sysname == "Darwin" and self.toolchain.is_dir():
            # Resolve Apple's shims before sandbox entry; they otherwise try to write
            # xcrun caches in the account's shared temporary directory.
            self.developer = Path(config.get("developer_dir") or subprocess.check_output(
                ["/usr/bin/xcode-select", "-p"], text=True, timeout=20).strip()).resolve()
            local_env = {"PATH": "/usr/bin:/bin", "DEVELOPER_DIR": str(self.developer)}
            self.sdk = subprocess.check_output(["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"],
                                               env=local_env, text=True, timeout=20).strip()
            for tool in ("git", "nm", "size", "dsymutil"):
                self.tools[tool] = Path(subprocess.check_output(["/usr/bin/xcrun", "--find", tool],
                                        env=local_env, text=True, timeout=20).strip())

    def capability(self, cap):
        require(cap in self.capabilities, f"capability disabled locally: {cap}")

    def validate(self, spec):
        validate_job(spec)
        require(spec["project"] in self.config["projects"], "project is not allowlisted on this worker")
        for step in spec["steps"]:
            if step["op"] == "exec":
                self.command(step["command"])
            elif step["op"] == "compare":
                self.capability("benchmark")
                for variant, cmd in step["commands"].items():
                    self.command(cmd, template=True)
                    binary = "match_benchmark" if step["mode"] == "jsonl" else "seed-seeker"
                    require(cmd["argv"][0] == f"${{JOB}}/artifacts/frozen/{variant}/{binary}", "compare must use frozen variant binaries")
            elif step["op"] == "profile":
                self.capability("profiling")
                self.command(step["command"])
                require(step["command"]["argv"][0].startswith("${JOB}/artifacts/frozen/"), "profile only frozen job binaries")
                require(not step["command"].get("wrappers"), "profiling commands cannot have wrappers")
                require(step["command"]["stdin"] == "", "profiling commands require empty stdin")
            elif step["op"] == "pgo_merge":
                self.capability("pgo")
        return spec

    def command(self, cmd, *, template=False):
        argv = cmd["argv"]
        env = cmd["env"]
        require(set(env) <= {"CARGO_TARGET_DIR", "RUSTFLAGS", "LLVM_PROFILE_FILE"}, "environment variable is not allowlisted")
        if "CARGO_TARGET_DIR" in env:
            job_argument(env["CARGO_TARGET_DIR"], "work/targets/")
        flags = env.get("RUSTFLAGS", "")
        if flags:
            self.capability("pgo")
            require(flags in ("-Cprofile-generate=${JOB}/work/pgo/raw", "-Cprofile-use=${JOB}/work/pgo/merged.profdata"),
                    "only fixed PGO RUSTFLAGS are allowed")
        if "LLVM_PROFILE_FILE" in env:
            self.capability("pgo")
            require(env["LLVM_PROFILE_FILE"] == "${JOB}/work/pgo/raw/%m-%p.profraw", "invalid LLVM_PROFILE_FILE")
        if flags.startswith("-Cprofile-generate"):
            require("LLVM_PROFILE_FILE" in env, "PGO generation requires LLVM_PROFILE_FILE")
        if argv[0] == "cargo":
            self.capability("cargo")
            require("CARGO_TARGET_DIR" in env and "RUSTFLAGS" in env, "Cargo requires explicit job target dir and RUSTFLAGS")
            require(re.fullmatch(r"work/checkouts/[A-Za-z0-9_-]+", cmd["cwd"]), "Cargo cwd must be a disposable checkout root")
            approved = argv in (BUILD, FMT, CLIPPY, TEST, PROFILE_BUILD)
            if argv == PROFILE_BUILD:
                self.capability("profiling")
            if argv[:len(FOCUSED_TEST)] == FOCUSED_TEST and len(argv) in (len(FOCUSED_TEST), len(FOCUSED_TEST) + 1):
                approved = len(argv) == len(FOCUSED_TEST) or bool(re.fullmatch(r"[A-Za-z0-9_:.-]{1,200}", argv[-1])) and not argv[-1].startswith("-")
            require(approved, "Cargo argv does not match an approved build/check/test")
            require(not flags or argv in (BUILD, PROFILE_BUILD), "PGO flags apply only to approved builds")
            require(cmd["stdin"] == "", "Cargo stdin must be empty")
        elif argv[0].startswith("${JOB}/artifacts/frozen/"):
            self.capability("benchmark")
            path = job_argument(argv[0], "artifacts/frozen/")
            require(len(path.split("/")) == 4, "binary must be in a frozen variant directory")
            binary = path.split("/")[-1]
            if binary == "seed-seeker":
                require(len(argv) == 5 and argv[1] == "--benchmark" and argv[3] == "--workers", "invalid seed-seeker arguments")
                require(argv[2].isdecimal() and 1 <= int(argv[2]) <= 10**10, "invalid seed count")
                workers = argv[4]
                require(cmd["stdin"] == "", "seed-seeker stdin must be empty")
            elif binary == "match_benchmark":
                require(len(argv) == 3, "invalid match_benchmark arguments")
                require(isinstance(json.loads(argv[1]), dict), "query must be a JSON object")
                workers = argv[2]
                for line in cmd["stdin"].splitlines():
                    seed_request(json.loads(line), allow_range=False)
            elif binary == "equivalence":
                self.capability("profiling")
                require(len(argv) == 1 and cmd["stdin"] == "", "equivalence only supports its default invocation")
                workers = "1"
            else:
                require(False, "binary is not allowlisted")
            require(workers == "${WORKERS}" and template or workers.isdecimal() and 1 <= int(workers) <= 1024,
                    "invalid worker count")
            require(not flags, "RUSTFLAGS are only valid for builds")
        elif argv[0] in ("llvm-objdump", "nm", "size"):
            self.capability("inspect")
            require(len(argv) >= 2, "inspector requires a frozen executable")
            job_argument(argv[-1], "artifacts/frozen/")
            allowed = {"llvm-objdump": {"--disassemble", "--demangle", "--section-headers", "--syms", "--macho", "--full-contents"},
                       "nm": {"-n", "-m", "-C", "-g"}, "size": {"-m", "-l"}}[argv[0]]
            require(all(a in allowed for a in argv[1:-1]), "invalid inspector arguments")
            require(not env and not cmd["stdin"], "inspection requires empty env/stdin")
        elif argv[:3] == ["xcrun", "xctrace", "list"]:
            self.capability("profiling")
            require(argv == ["xcrun", "xctrace", "list", "templates"], "invalid xctrace list")
            require(not env and not cmd["stdin"], "xctrace requires empty env/stdin")
        elif argv[:3] == ["xcrun", "xctrace", "export"]:
            self.capability("profiling")
            require(len(argv) in (8, 9) and argv[3] == "--input" and argv[5] == "--output", "invalid xctrace export")
            job_argument(argv[4], "artifacts/")
            out = job_argument(argv[6], "artifacts/")
            require(not out.startswith("artifacts/frozen/"), "cannot export to frozen binaries")
            require(argv[7:] == ["--toc"] or len(argv) == 9 and argv[7] == "--xpath", "invalid export selector")
            require(not env and not cmd["stdin"], "xctrace requires empty env/stdin")
        else:
            require(False, "executable is not allowlisted")

    def expand(self, value, root, workers=None):
        value = value.replace("${JOB}", str(root))
        if workers is not None:
            value = value.replace("${WORKERS}", str(workers))
        require("${" not in value, "unknown template variable")
        return value

    def argv(self, cmd, root, workers=None):
        # Paths are checked again at execution, after earlier steps have run.
        for arg in cmd["argv"]:
            if arg.startswith("${JOB}/"):
                safe_path(root, arg[len("${JOB}/"):])
        argv = [self.expand(a, root, workers) for a in cmd["argv"]]
        if not cmd["argv"][0].startswith("${JOB}/"):
            argv[0] = str(self.tools[argv[0]])
        else:
            require(safe_path(root, cmd["argv"][0][len("${JOB}/"):], exists=True).is_file(), "binary is missing")
        for wrapper in reversed(cmd.get("wrappers", [])):
            argv = [str(self.tools[wrapper]), "-l" if wrapper == "time" else "-i", *argv]
        return argv

    def environment(self, root, additions):
        cargo_home = Path(self.config["cargo_home"]).resolve()
        env = {"PATH": f"{self.toolchain / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin",
               "HOME": str(root / "work/home"), "TMPDIR": str(root / "work/tmp") + "/",
               "CARGO_HOME": str(cargo_home), "CARGO_NET_OFFLINE": "true",
               "RUSTC": str(self.tools["rustc"]), "RUSTDOC": str(self.toolchain / "bin/rustdoc"),
               "RUSTC_WRAPPER": "", "RUSTC_WORKSPACE_WRAPPER": "", "RUSTFLAGS": "",
               "CARGO_INCREMENTAL": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
               "GIT_TERMINAL_PROMPT": "0", "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
               "MACOSX_DEPLOYMENT_TARGET": self.config.get("deployment_target", "13.0")}
        env.update({k: self.expand(v, root) for k, v in additions.items()})
        if self.developer:
            compiler_bin = self.developer / "Toolchains/XcodeDefault.xctoolchain/usr/bin"
            if not compiler_bin.is_dir():
                compiler_bin = self.developer / "usr/bin"
            env.update({"DEVELOPER_DIR": str(self.developer), "SDKROOT": self.sdk,
                        "CC": str(compiler_bin / "clang"), "CXX": str(compiler_bin / "clang++"),
                        "CARGO_TARGET_AARCH64_APPLE_DARWIN_LINKER": str(compiler_bin / "clang"),
                        "xcrun_nocache": "1"})
            env["PATH"] = f"{self.toolchain / 'bin'}:{compiler_bin}:{self.developer / 'usr/bin'}:/usr/bin:/bin:/usr/sbin:/sbin"
        return env

    def sandbox(self, root, *, extra_read=(), writable=()):
        """Seatbelt is defense in depth; run unreviewed code inside a disposable VM."""
        def literal(path):
            return json.dumps(str(Path(path).resolve()))
        roots = ["/System/Library", "/usr", "/bin", "/sbin", "/Library/Apple", "/Library/Developer",
                 "/Applications/Xcode.app", "/private/var/db/dyld", "/private/var/db/xcode_select_link",
                 "/private/etc/localtime", "/private/etc/passwd", "/private/etc/group", "/private/etc/ssl",
                 "/Library/Preferences/com.apple.dt.Xcode.plist", "/Library/Preferences/com.apple.dt.CommandLineTools.plist",
                 self.toolchain, self.config["cargo_home"], root,
                 *self.config.get("read_roots", []), *extra_read]
        if self.developer:
            roots.append(self.developer)
        reads = "\n".join(f"(allow file-read* (subpath {literal(p)}))" for p in roots)
        writes = "\n".join(f"(allow file-write* (subpath {literal(p)}))" for p in writable)
        return f'''(version 1)
(deny default)
(allow process-fork process-exec)
(allow signal (target children))
(allow process-info* sysctl-read mach-lookup)
(allow file-read-metadata)
(allow file-read* (literal "/"))
(allow file-read* (literal "/dev/null") (literal "/dev/zero") (literal "/dev/random") (literal "/dev/urandom"))
(allow file-write* (literal "/dev/null"))
{reads}
(allow file-write* (subpath {literal(root / 'work')}) (subpath {literal(self.config['cargo_home'])}))
{writes}
(deny file-write* (subpath {literal(root / 'artifacts/frozen')}))
'''
