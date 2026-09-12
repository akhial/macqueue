import fcntl
import grp
import json
import logging
import os
import platform
import queue
import signal
import threading
import time
from pathlib import Path

from .client import Client, RemoteError
from .common import Invalid, integer, json_bytes, keys, name, private_write, read_secret, require
from .policy import Policy
from .process import DEFAULT_MAX_OUTPUT_BYTES, CleanupError
from .runner import Runner, pack_artifacts

LOG = logging.getLogger("macqueue")


def load_config(path, *, check_account=True):
    path = Path(path).resolve()
    require(path.stat().st_mode & 0o022 == 0, "worker config must not be group/world writable")
    config = json.loads(path.read_text())
    keys(config, ("server_url", "token_file", "worker_id", "state_dir", "cargo_home", "rust_toolchain", "projects"),
         ("allow_http", "capabilities", "read_roots", "deployment_target", "max_output_bytes", "max_artifact_bytes",
          "max_job_bytes", "min_free_bytes", "retention_days", "developer_dir"))
    name(config["worker_id"])
    require(platform.system() == "Darwin" and platform.machine() == "arm64", "production worker requires Apple Silicon macOS")
    require(Path("/usr/bin/sandbox-exec").is_file(), "sandbox-exec is required; no unsandboxed fallback")
    if check_account:
        require(os.geteuid() != 0, "worker must not run as root")
        require(grp.getgrnam("admin").gr_gid not in os.getgroups(), "use a dedicated non-administrator worker account or VM account")
    for key in ("token_file", "state_dir", "cargo_home", "rust_toolchain"):
        require(isinstance(config[key], str) and Path(config[key]).is_absolute(), f"{key} must be an absolute path")
        config[key] = str(Path(config[key]).resolve())
    require(isinstance(config["projects"], dict) and 1 <= len(config["projects"]) <= 100, "expected local project mirrors")
    for project, mirror in config["projects"].items():
        name(project)
        require(isinstance(mirror, str) and Path(mirror).is_absolute() and Path(mirror).is_dir(), "mirror must be an existing absolute directory")
        config["projects"][project] = str(Path(mirror).resolve())
    for key, default, low, high in (("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES, 1024, 512 * 1024**2),
                                  ("max_artifact_bytes", 512 * 1024**2, 1024, 2 * 1024**3),
                                  ("max_job_bytes", 20 * 1024**3, 1024**2, 1024**4),
                                  ("min_free_bytes", 5 * 1024**3, 0, 1024**4),
                                  ("retention_days", 7, 1, 365)):
        integer(config.get(key, default), low, high, key)
    require(type(config.get("allow_http", False)) is bool, "allow_http must be boolean")
    require(isinstance(config.get("read_roots", []), list), "read_roots must be a list")
    for root in config.get("read_roots", []):
        require(isinstance(root, str) and Path(root).is_absolute(), "read roots must be absolute paths")
        require(str(Path(root).resolve()) not in ("/", "/Users", str(Path.home())), "read root is too broad")
    state = Path(config["state_dir"])
    cache = Path(config["cargo_home"])
    toolchain = Path(config["rust_toolchain"])
    require((toolchain / "bin/cargo").is_file() and (toolchain / "bin/rustc").is_file(), "use a pinned installed Rust toolchain directory")
    require(cache.is_dir(), "provision the dedicated Cargo cache before starting the worker")
    require(not (cache / "credentials").exists() and not (cache / "credentials.toml").exists(), "Cargo cache must not contain credentials")
    protected = [path, Path(config["token_file"]), state]
    readable = [cache, toolchain, *map(Path, config["projects"].values()), *map(Path, config.get("read_roots", [])),
                Path("/usr"), Path("/System/Library"), Path("/Applications/Xcode.app"), Path("/Library/Developer")]
    for secret in protected:
        require(not any(secret.is_relative_to(root.resolve()) for root in readable), "control files/state must be outside sandbox read roots")
    require(not any(p.is_relative_to(state) or state.is_relative_to(p) for p in (cache, toolchain, *map(Path, config["projects"].values()))),
            "state, Cargo cache, toolchain, and source mirrors must be separate directories")
    require(not any(cache.is_relative_to(p) or p.is_relative_to(cache) for p in (toolchain, *map(Path, config["projects"].values()))),
            "writable Cargo cache must be separate from toolchain and source mirrors")
    require(not any(Path(config["token_file"]).is_relative_to(p) for p in (state / "jobs",)), "token must be outside job workspaces")
    read_secret(config["token_file"])
    Policy(config)
    return config


class Lease:
    def __init__(self, client, claim, root, config):
        self.client, self.claim, self.root, self.config = client, claim, root, config
        self.cancel = threading.Event()
        self.done = threading.Event()
        self.events = queue.Queue(maxsize=256)
        self.sequence = 0
        self.pending = None
        self.last_ok = claim.get("requested_at", time.monotonic())
        self.reason = None
        self.thread = threading.Thread(target=self.loop, name="job-lease", daemon=True)

    def event(self, value):
        # Full command streams remain in artifacts; live preview is intentionally bounded.
        encoded = json_bytes({"at": time.time(), **value}).decode()
        if len(encoded.encode()) > 60000:
            encoded = json_bytes({"at": time.time(), "event": "large_record", "detail": "see artifact for full record"}).decode()
        try:
            self.events.put_nowait(encoded + "\n")
        except queue.Full:
            pass

    def loop(self):
        prefix = f"/v1/worker/jobs/{self.claim['job']['id']}"
        lease = self.claim["lease"]
        next_heartbeat = 0
        while not self.done.is_set():
            now = time.monotonic()
            if now >= next_heartbeat:
                try:
                    started = time.monotonic()
                    response = self.client.request("POST", prefix + "/heartbeat", {}, lease=lease)
                    self.last_ok = started
                    if response["cancel"]:
                        self.reason = "cancelled"
                        self.cancel.set()
                    next_heartbeat = time.monotonic() + 10
                except RemoteError as exc:
                    if exc.status < 500:
                        self.reason = "lease lost"
                        self.cancel.set()
                    next_heartbeat = time.monotonic() + 2
                except (OSError, ValueError):
                    next_heartbeat = time.monotonic() + 2
            if time.monotonic() - self.last_ok >= 40:
                self.reason = "lease renewal unavailable"
                self.cancel.set()
            try:
                if self.pending is None:
                    chunks = []
                    size = 0
                    while size < 4096:
                        try:
                            item = self.events.get_nowait()
                        except queue.Empty:
                            break
                        # Events individually fit; one large record gets its own request.
                        if size and size + len(item.encode()) > 65536:
                            self.events.put_nowait(item)
                            break
                        chunks.append(item)
                        size += len(item.encode())
                    if chunks:
                        self.pending = "".join(chunks)
                if self.pending is not None and not self.cancel.is_set():
                    self.client.request("POST", prefix + "/logs", {"seq": self.sequence, "text": self.pending}, lease=lease)
                    self.sequence += 1
                    self.pending = None
            except (OSError, ValueError, RemoteError):
                pass
            self.done.wait(0.5)

    def stop(self):
        self.done.set()
        self.thread.join(timeout=35)


class Worker:
    def __init__(self, config):
        self.config = config
        self.policy = Policy(config)
        self.state = Path(config["state_dir"])
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.state / "jobs").mkdir(exist_ok=True, mode=0o700)
        self.client = Client(config["server_url"], read_secret(config["token_file"]), allow_http=config.get("allow_http", False))
        self.stop = threading.Event()
        self.active = None
        self.lock = None

    def signal(self, *_):
        self.stop.set()
        if self.active:
            self.active.reason = "worker stopped locally"
            self.active.cancel.set()

    def watchdog(self, lease):
        import shutil
        deadline = time.monotonic() + lease.claim["job"]["spec"].get("timeout_seconds", 14400)
        while not lease.done.wait(1):
            if time.monotonic() >= deadline:
                lease.reason = "job timed out"
                lease.cancel.set()
            # Separate from network IO: a stalled heartbeat call cannot extend a local lease.
            if time.monotonic() - lease.last_ok >= 40:
                lease.reason = "lease renewal unavailable"
                lease.cancel.set()
            if not lease.root.exists():
                continue
            try:
                total = 0
                for base, dirs, files in os.walk(lease.root, followlinks=False):
                    dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
                    for filename in files:
                        try:
                            total += (Path(base) / filename).lstat().st_size
                        except FileNotFoundError:
                            pass
                    if total > self.config.get("max_job_bytes", 20 * 1024**3):
                        lease.reason = "job disk budget exceeded"
                        lease.cancel.set()
                        break
                if shutil.disk_usage(self.state).free < self.config.get("min_free_bytes", 5 * 1024**3):
                    lease.reason = "minimum free disk space reached"
                    lease.cancel.set()
            except OSError:
                lease.reason = "cannot inspect job disk usage"
                lease.cancel.set()
            lease.done.wait(4)

    def retry(self, function, lease, *, seconds=90):
        deadline = time.monotonic() + seconds
        while True:
            try:
                return function()
            except RemoteError as exc:
                if exc.status < 500:
                    raise
            except (OSError, ValueError):
                pass
            if time.monotonic() >= deadline or lease.reason in ("lease lost", "lease renewal unavailable"):
                raise RuntimeError("result delivery unavailable; job files retained locally")
            lease.done.wait(2)

    def handle(self, claim):
        job_id = claim["job"]["id"]
        require(isinstance(job_id, str) and len(job_id) == 32 and all(c in "0123456789abcdef" for c in job_id), "invalid server job ID")
        root = self.state / "jobs" / job_id
        marker = self.state / "ACTIVE.json"
        private_write(marker, json_bytes({"job_id": job_id, "worker_pid": os.getpid(), "started": time.time()}))
        cleanup_ok = True
        lease = Lease(self.client, claim, root, self.config)
        self.active = lease
        lease.thread.start()
        guard = threading.Thread(target=self.watchdog, args=(lease,), daemon=True)
        guard.start()
        result, status = {}, "failed"
        try:
            runner = Runner(self.policy, root, claim["job"]["spec"], lease.cancel, lease.event,
                            download_input=lambda sha, destination, check: self.client.download_input(
                                sha, destination, job_id, claim['lease'], check))
            result = runner.run()
            status = "succeeded"
        except Exception as exc:
            if isinstance(exc, CleanupError):
                cleanup_ok = False
                self.stop.set()
            result = {"error": str(exc), "type": type(exc).__name__}
            status = "cancelled" if lease.reason == "cancelled" else "failed"
            LOG.warning("Job %s failed: %s", job_id, exc)
        try:
            result["stop_reason"] = lease.reason
            result["worker_id"] = self.config["worker_id"]
            # A worker-owned receipt outside the sandbox survives any packaging/upload failure.
            receipt = self.state / (job_id + ".result.json")
            receipt.write_bytes(json_bytes({"status": status, "result": result}))
            if (root / "artifacts").is_dir():
                try:
                    result_path = root / "artifacts/worker-result.json"
                    require(not result_path.exists() and not result_path.is_symlink(), "reserved result path already exists")
                    result_path.write_bytes(json_bytes({"status": status, "result": result}))
                    archive = self.state / (job_id + ".tar.gz")
                    pack_artifacts(root, archive, self.config.get("max_artifact_bytes", 512 * 1024**2))
                    self.retry(lambda: self.client.upload(job_id, claim["lease"], archive), lease)
                except Exception as exc:
                    status = "failed" if status == "succeeded" else status
                    result["artifact_error"] = str(exc)
                    LOG.warning("Artifact delivery for %s: %s", job_id, exc)
            lease.event({"event": "job_finished", "status": status, "result": result})
            completed = self.retry(lambda: self.client.request("POST", f"/v1/worker/jobs/{job_id}/finish",
                                  {"status": status, "result": result}, lease=claim["lease"]), lease)
            status = completed["status"]
            receipt.write_bytes(json_bytes({"status": status, "result": result, "delivered": True}))
            LOG.info("Job %s: %s", job_id, status)
        except Exception as exc:
            LOG.error("Could not finish %s: %s; local files retained in %s", job_id, exc, root)
        finally:
            lease.stop()
            guard.join(timeout=6)
            self.active = None
            if cleanup_ok:
                marker.unlink()

    def run(self, *, once=False):
        try:
            self.run_locked(once=once)
        finally:
            if self.lock:
                self.lock.close()

    def run_locked(self, *, once=False):
        import shutil
        self.lock = (self.state / "worker.lock").open("a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Invalid("another worker already owns this state directory") from None
        require(not (self.state / "ACTIVE.json").exists(),
                "previous worker exited with an active job; inspect/reset the execution environment, then remove ACTIVE.json before restarting")
        from .doctor import doctor
        doctor(self.config)
        signal.signal(signal.SIGTERM, self.signal)
        signal.signal(signal.SIGINT, self.signal)
        LOG.info("Worker %s ready; paused=%s", self.config["worker_id"], (self.state / "PAUSED").exists())
        while not self.stop.is_set():
            if (self.state / "PAUSED").exists():
                if once:
                    return
                self.stop.wait(2)
                continue
            if shutil.disk_usage(self.state).free < self.config.get("min_free_bytes", 5 * 1024**3):
                LOG.warning("Waiting for free disk space")
                if once:
                    return
                self.stop.wait(10)
                continue
            try:
                requested_at = time.monotonic()
                claim = self.client.request("POST", "/v1/worker/claim", {"worker": self.config["worker_id"], "projects": list(self.config["projects"])})
                if claim:
                    claim["requested_at"] = requested_at
                    if self.stop.is_set() or (self.state / "PAUSED").exists():
                        self.client.request("POST", f"/v1/worker/jobs/{claim['job']['id']}/finish",
                                            {"status": "cancelled", "result": {"error": "worker paused/stopped while claiming"}}, lease=claim["lease"])
                    else:
                        self.handle(claim)
                if once:
                    return
            except (OSError, RemoteError, ValueError) as exc:
                LOG.warning("Queue unavailable: %s", exc)
                if once:
                    raise
                self.stop.wait(5)


def prune_worker(config, days):
    """Delete only completed, delivered jobs. Hold the same lock as the worker."""
    import shutil
    state = Path(config["state_dir"])
    count = 0
    with (state / "worker.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Invalid("stop the worker before pruning local jobs") from None
        for receipt in state.glob("*.result.json"):
            if receipt.stat().st_mtime >= time.time() - days * 86400:
                continue
            outcome = json.loads(receipt.read_text())
            if not outcome.get("delivered") or outcome.get("result", {}).get("artifact_error"):
                continue
            job_id = receipt.name.split(".")[0]
            require(len(job_id) == 32 and all(c in "0123456789abcdef" for c in job_id), "invalid local receipt name")
            root = state / "jobs" / job_id
            require(not root.is_symlink(), "job directory cannot be a symlink")
            if root.exists():
                for base, dirs, _ in os.walk(root, followlinks=False):
                    Path(base).chmod(0o700)
                    dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
                shutil.rmtree(root)
            (state / (job_id + ".tar.gz")).unlink(missing_ok=True)
            receipt.unlink()
            count += 1
    return count
