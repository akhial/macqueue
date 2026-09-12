#!/usr/bin/env python3
"""Stage offline dependencies as the operator; install a reversible worker snapshot."""
import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

BASE = Path("/Library/Macqueue")
LABEL = "system/local.macqueue.worker"


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def queue_url(value):
    url = urlsplit(value)
    require(url.scheme == "http" and not url.username and not url.password and
            url.path in ("", "/") and not url.query and not url.fragment and url.port == 8787,
            "expected a private queue endpoint on port 8787")
    require(ipaddress.ip_address(url.hostname) in ipaddress.ip_network("100.64.0.0/10"),
            "queue must use a Tailscale IPv4 address")


def run(argv, **kwargs):
    result = subprocess.run(list(map(str, argv)), text=True, capture_output=True,
                            timeout=kwargs.pop("timeout", 120), **kwargs)
    require(result.returncode == 0, f"{argv[0]} failed: {result.stderr[-6000:] or result.stdout[-6000:]}")
    return result.stdout


def sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def tree(path):
    result = {}
    require(path.is_dir() and not path.is_symlink(), "expected a real directory")
    for item in sorted(path.rglob("*")):
        require(not item.is_symlink(), f"symlink not accepted in provisioning snapshot: {item}")
        if item.is_file():
            result[str(item.relative_to(path))] = sha(item)
        else:
            require(item.is_dir(), "snapshot contains a special file")
    return result


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".new")
    with temporary.open("x") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream, indent=2)
        stream.write("\n")
    temporary.replace(path)


def prepare(args):
    require(os.geteuid() != 0, "prepare dependencies as your normal account, not root")
    stage = args.stage.resolve()
    require(not (stage / "plan.json").exists(), "already prepared; use a fresh stage")
    require((stage / "worker.token").stat().st_mode & 0o077 == 0, "worker token must be private")
    require(len((stage / "worker.token").read_text().strip()) >= 32, "invalid worker credential")
    queue_url(args.url)
    toolchain = args.toolchain.resolve()
    env = {"PATH": str(toolchain / "bin") + ":/usr/bin:/bin:/usr/sbin:/sbin",
           "HOME": str(stage / "home"), "TMPDIR": str(stage / "tmp") + "/",
           "CARGO_HOME": str(stage / "cargo"), "RUSTC": str(toolchain / "bin/rustc"),
           "RUSTDOC": str(toolchain / "bin/rustdoc"), "RUSTFLAGS": "",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_TERMINAL_PROMPT": "0", "LANG": "en_US.UTF-8"}
    for name in ("cargo", "home", "tmp", "checkouts"):
        (stage / name).mkdir(mode=0o700, exist_ok=True)
    mirror = stage / "mirror.git"
    if not mirror.exists():
        run(["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "clone", "--bare", stage / "source.bundle", mirror], env=env)
    require(not (mirror / "objects/info/alternates").exists(), "mirror must be self-contained")
    for revision in args.revision:
        require(re.fullmatch(r"[0-9a-f]{40}", revision), "use full commit SHAs")
        checkout = stage / "checkouts" / revision
        if not checkout.exists():
            run(["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "clone", "--no-hardlinks", "--no-checkout", mirror, checkout], env=env)
            run(["/usr/bin/git", "-C", checkout, "-c", "core.hooksPath=/dev/null", "checkout", "--detach", revision], env=env)
        require(run(["/usr/bin/git", "-C", checkout, "rev-parse", "HEAD"], env=env).strip() == revision, "checkout revision changed")
        require(not any((checkout / name).exists() for name in (".cargo/config", ".cargo/config.toml")),
                "repository Cargo configuration needs a separate provisioning review")
        before = sha(checkout / "Cargo.lock")
        print("Fetching locked dependencies for " + revision, flush=True)
        with (stage / (revision + "-fetch.log")).open("w") as log:
            result = subprocess.run([str(toolchain / "bin/cargo"), "fetch", "--locked"], cwd=checkout,
                                    env=env, stdout=log, stderr=subprocess.STDOUT, timeout=900)
        require(result.returncode == 0, "fetch failed; inspect the private staging fetch log")
        # Fetch all targets, including host/test dependencies; metadata must now
        # resolve the entire locked graph without network access or compilation.
        run([toolchain / "bin/cargo", "metadata", "--locked", "--offline", "--format-version", "1"],
            cwd=checkout, env=env, timeout=180)
        require(sha(checkout / "Cargo.lock") == before, "dependency provisioning changed Cargo.lock")
    require(not any((stage / "cargo" / name).exists() for name in ("credentials", "credentials.toml", "config", "config.toml")),
            "cache must not contain credentials or operator Cargo configuration")
    value = {"version": 1, "id": uuid.uuid4().hex, "stage": str(stage), "url": args.url,
             "revisions": args.revision, "rust_version": run([toolchain / "bin/rustc", "--version"]).strip(),
             "source_bundle_sha256": sha(stage / "source.bundle"), "token_sha256": sha(stage / "worker.token"),
             "mirror_files": tree(mirror), "cargo_files": tree(stage / "cargo"),
             "installer_sha256": sha(Path(__file__)), "resume": args.resume}
    save(stage / "plan.json", value)
    print(json.dumps({"plan": str(stage / "plan.json"), "revisions": args.revision,
                      "cache_files": len(value["cargo_files"]), "offline_resolution": "passed", "resume": args.resume}, indent=2))


def ownership(path, uid, gid, *, readonly):
    for item in [path, *path.rglob("*")]:
        require(not item.is_symlink(), "symlink in installed snapshot")
        executable = item.stat().st_mode & 0o111
        os.chown(item, uid, gid)
        item.chmod((0o750 if item.is_dir() or executable else 0o640) if readonly else
                   (0o700 if item.is_dir() or executable else 0o600))


def idle():
    require((BASE / "state/PAUSED").is_file(), "pause the worker locally before provisioning")
    require(not (BASE / "state/ACTIVE.json").exists(), "wait for the active job to finish")


def restart():
    log = BASE / "logs/worker.stderr.log"
    offset = log.stat().st_size
    run(["/bin/launchctl", "kickstart", "-k", LABEL])
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        with log.open() as stream:
            stream.seek(offset)
            latest = stream.read()
        if "ready; paused=True" in latest:
            return
        time.sleep(0.5)
    raise RuntimeError("worker did not restart paused; inspect its private log")


def apply(plan):
    require(os.geteuid() == 0, "apply requires macOS administrator authentication")
    idle()
    value = json.loads(plan.read_text())
    require(value["version"] == 1 and re.fullmatch(r"[a-f0-9]{32}", value["id"]), "invalid provisioning plan")
    queue_url(value["url"])
    require(sha(Path(__file__)) == value["installer_sha256"], "provisioner changed after preparation")
    stage = Path(value["stage"])
    require(sha(stage / "source.bundle") == value["source_bundle_sha256"] and
            sha(stage / "worker.token") == value["token_sha256"], "staged source or credential changed")
    require(tree(stage / "mirror.git") == value["mirror_files"] and tree(stage / "cargo") == value["cargo_files"],
            "staged mirror/cache changed after preparation")
    installation = json.loads((BASE / "installation.json").read_text())
    uid, gid = installation["uid"], installation["gid"]
    require(run([BASE / "toolchain/rust/bin/rustc", "--version"]).strip() == value["rust_version"], "staging and worker Rust versions differ")
    config = json.loads((BASE / "config/worker.json").read_text())
    require(config["projects"]["seedfinder"] == str(BASE / "mirrors/seedfinder.git") and
            config["cargo_home"] == str(BASE / "cargo") and config["token_file"] == str(BASE / "config/worker.token"), "unexpected worker paths")
    installed_script = BASE / "provision.py"
    require(not installed_script.exists() or sha(installed_script) == value["installer_sha256"], "a different provisioner is installed")
    backup = BASE / "provisioning" / value["id"]
    backup.mkdir(parents=True, mode=0o700)
    backup.parent.chmod(0o700)
    for name in ("worker.json", "worker.token"):
        shutil.copy2(BASE / "config" / name, backup / name)
    shutil.copy2(BASE / "state/PAUSED", backup / "PAUSED")
    receipt = {"id": value["id"], "installation_id": installation["installation_id"],
               "revisions": value["revisions"], "url": value["url"], "status": "installing", "moved": []}
    save(backup / "receipt.json", receipt)
    try:
        for name, live, source in (("mirror.git", BASE / "mirrors/seedfinder.git", stage / "mirror.git"),
                                   ("cargo", BASE / "cargo", stage / "cargo")):
            replacement = backup / ("new-" + name)
            shutil.copytree(source, replacement)
            ownership(replacement, 0 if name == "mirror.git" else uid, gid, readonly=name == "mirror.git")
            receipt["moved"].append(name)
            save(backup / "receipt.json", receipt)
            live.rename(backup / name)
            replacement.rename(live)
        config.update(server_url=value["url"], allow_http=True)
        save(BASE / "config/worker.json", config)
        os.chown(BASE / "config/worker.json", 0, gid)
        (BASE / "config/worker.json").chmod(0o640)
        temporary = BASE / "config/worker.token.new"
        shutil.copyfile(stage / "worker.token", temporary)
        os.chown(temporary, uid, gid)
        temporary.chmod(0o600)
        temporary.replace(BASE / "config/worker.token")
        shutil.copyfile(Path(__file__), installed_script)
        os.chown(installed_script, 0, gid)
        installed_script.chmod(0o640)
        receipt["config_sha256"] = sha(BASE / "config/worker.json")
        receipt["status"] = "installed-paused"
        save(backup / "receipt.json", receipt)
        # All project execution remains inside the normal sandboxed worker.
        run(["/usr/bin/sudo", "-u", "_macqueue", "--", "/usr/bin/env", "-i",
             "HOME=" + str(BASE / "home"), "PATH=/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONDONTWRITEBYTECODE=1",
             installation["python"], "-I", BASE / "app/run-worker.py", "doctor", "--config", BASE / "config/worker.json"], timeout=180)
        restart()
        if value["resume"]:
            (BASE / "state/PAUSED").unlink()
            receipt["status"] = "active"
        save(backup / "receipt.json", receipt)
        result = {"ok": True, "server_url": value["url"], "revisions": value["revisions"],
                  "worker_state": receipt["status"], "sandbox": "passed",
                  "rollback": "sudo /usr/bin/python3 /Library/Macqueue/provision.py rollback --receipt " + str(backup / "receipt.json")}
        save(stage / "result.json", result)
        print(json.dumps(result, indent=2), flush=True)
    except BaseException:
        (BASE / "state/PAUSED").touch(mode=0o600)
        print("Provisioning incomplete; worker stays paused. Roll back using receipt " + str(backup / "receipt.json"), file=sys.stderr)
        raise


def rollback(path):
    require(os.geteuid() == 0, "rollback requires administrator authentication")
    path = path.resolve()
    require(path.parent.parent == BASE / "provisioning" and path.name == "receipt.json", "unexpected receipt path")
    receipt = json.loads(path.read_text())
    require(receipt["status"] != "rolled-back", "already rolled back")
    require(json.loads((BASE / "installation.json").read_text())["installation_id"] == receipt["installation_id"], "installation changed")
    if "config_sha256" in receipt:
        require(sha(BASE / "config/worker.json") == receipt["config_sha256"], "configuration changed; review before rollback")
    (BASE / "state/PAUSED").touch(mode=0o600)
    idle()
    backup = path.parent
    for name, live in (("mirror.git", BASE / "mirrors/seedfinder.git"), ("cargo", BASE / "cargo")):
        if name in receipt["moved"] and (backup / name).exists():
            if live.exists():
                live.rename(backup / ("removed-" + name))
            (backup / name).rename(live)
    installation = json.loads((BASE / "installation.json").read_text())
    for name in ("worker.json", "worker.token"):
        shutil.copy2(backup / name, BASE / "config" / name)
        os.chown(BASE / "config" / name, 0 if name == "worker.json" else installation["uid"], installation["gid"])
    restart()
    receipt["status"] = "rolled-back"
    save(path, receipt)
    print(json.dumps({"rolled_back": True, "worker_state": "paused", "retained_private_backup": str(backup)}))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--stage", type=Path, required=True)
    p.add_argument("--revision", action="append", required=True)
    p.add_argument("--toolchain", type=Path, required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--resume", action="store_true")
    p = sub.add_parser("apply")
    p.add_argument("--plan", type=Path, required=True)
    p = sub.add_parser("rollback")
    p.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args)
    elif args.action == "apply":
        apply(args.plan)
    else:
        rollback(args.receipt)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(str(exc))
