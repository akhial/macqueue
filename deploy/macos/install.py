#!/usr/bin/env python3
"""Reversible, new-install-only macOS setup. Compatible with Apple's Python 3.9.

Plan as a normal user; apply/verify/rollback as root. Never alters an existing
account, installation, loginwindow setting, home permission, or toolchain.
"""
import argparse
import hashlib
import json
import os
import platform
import plistlib
import re
import secrets
import shutil
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace
from pathlib import Path

BASE = Path("/Library/Macqueue")
PLIST = Path("/Library/LaunchDaemons/local.macqueue.worker.plist")
ACCOUNT = "_macqueue"
LABEL = "local.macqueue.worker"
REPO = Path(__file__).resolve().parents[2]


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def command(argv, check=True, **kwargs):
    result = subprocess.run(list(map(str, argv)), text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=kwargs.pop("timeout", 120), **kwargs)
    if check and result.returncode:
        raise RuntimeError("{} failed ({}): {}".format(argv[0], result.returncode, result.stderr.strip() or result.stdout.strip()))
    return result


def sha(path):
    result = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def ds(kind, attribute=None):
    argv = ["/usr/bin/dscl", ".", "-read", "/" + kind + "/" + ACCOUNT]
    if attribute:
        argv.append(attribute)
    return command(argv, check=False)


def ids(kind, attribute):
    text = command(["/usr/bin/dscl", ".", "-list", "/" + kind, attribute]).stdout
    return {int(line.split()[-1]) for line in text.splitlines() if line.strip()}


def sources(repo):
    paths = list((repo / "macqueue").glob("*.py"))
    paths += [repo / "deploy/macos/install.py", repo / "deploy/macos/smoke.py"]
    require(paths and all(p.is_file() and not p.is_symlink() for p in paths), "source files must be regular files")
    return {str(p.relative_to(repo)): sha(p) for p in sorted(paths)}


def plan(args):
    require(platform.system() == "Darwin" and platform.machine() == "arm64", "requires Apple Silicon macOS")
    occupied = ids("Users", "UniqueID") | ids("Groups", "PrimaryGroupID")
    uid = next((value for value in range(450, 500) if value not in occupied), None)
    require(uid is not None, "no unused service UID/GID in 450..499")
    repo = Path(args.source).resolve()
    python = Path(args.python).resolve()
    toolchain = Path(args.toolchain).resolve()
    result = {"version": 1, "installation_id": str(uuid.uuid4()), "account": ACCOUNT,
              "account_uuid": str(uuid.uuid4()).upper(), "group_uuid": str(uuid.uuid4()).upper(),
              "uid": uid, "gid": uid, "base": str(BASE), "plist": str(PLIST), "label": LABEL,
              "source": str(repo), "source_hashes": sources(repo), "python": str(python),
              "toolchain_source": str(toolchain),
              "rust_version": command([toolchain / "bin/rustc", "--version"]).stdout.strip(),
              "developer_dir": command(["/usr/bin/xcode-select", "-p"]).stdout.strip(),
              "server_url": "https://macqueue.invalid", "starts_paused": True,
              "mirror": "empty local seedfinder mirror; provision actual source separately",
              "changes": ["create hidden authentication-disabled _macqueue user and private group",
                          "create /Library/Macqueue with isolated home, state, cache, mirror, and copied Rust toolchain",
                          "install root-owned application snapshot, config, and rollback tool",
                          "generate a worker-only token; never print it",
                          "install system LaunchDaemon running as _macqueue without a GUI session",
                          "run local sandbox and Cargo smoke checks; leave worker paused"],
              "unchanged": ["existing accounts and memberships", "loginwindow and FileVault settings",
                            "personal home permissions", "existing Rust/Python installations", "SSH and network policy"]}
    preflight(result)
    return result


def preflight(value):
    require(value["base"] == str(BASE) and value["plist"] == str(PLIST) and
            value["account"] == ACCOUNT and value["label"] == LABEL, "installation paths/identity cannot be overridden")
    require(450 <= value["uid"] < 500 and value["uid"] == value["gid"], "invalid service UID/GID")
    require(not BASE.exists() and not BASE.is_symlink(), "installation directory already exists; refusing to modify it")
    require(not PLIST.exists() and not PLIST.is_symlink(), "LaunchDaemon plist already exists")
    require(ds("Users").returncode != 0 and ds("Groups").returncode != 0, "service account/group already exists")
    require(value["uid"] not in ids("Users", "UniqueID") and value["gid"] not in ids("Groups", "PrimaryGroupID"), "UID/GID is now in use")
    require(command(["/bin/launchctl", "print", "system/" + LABEL], check=False).returncode != 0, "service is already loaded")
    require(sources(Path(value["source"])) == value["source_hashes"], "source changed since the plan was made; generate a new plan")
    version = command([value["python"], "-I", "-c", "import sys; print('%d.%d' % sys.version_info[:2])"]).stdout.strip()
    require(tuple(map(int, version.split("."))) >= (3, 11), "worker Python must be 3.11 or newer")
    require(command([Path(value["toolchain_source"]) / "bin/rustc", "--version"]).stdout.strip() == value["rust_version"], "Rust toolchain changed since planning")
    require(shutil.disk_usage(BASE.parent).free > 5 * 1024**3, "at least 5 GiB free space is required")


def write(path, data, mode=0o600, uid=0, gid=0):
    with path.open("xb") as stream:
        stream.write(data)
    path.chmod(mode)
    os.chown(path, uid, gid)


def journal(value, status):
    value["status"] = status
    temp = BASE / "installation.json.new"
    temp.write_text(json.dumps(value, indent=2) + "\n")
    temp.chmod(0o640)
    os.chown(temp, 0, value["gid"])
    temp.replace(BASE / "installation.json")


def account_property(kind, key, value):
    command(["/usr/bin/dscl", ".", "-create", "/" + kind + "/" + ACCOUNT, key, str(value)])


def daemon(value):
    return {"Label": LABEL, "UserName": ACCOUNT, "GroupName": ACCOUNT,
            "ProgramArguments": [value["python"], "-I", str(BASE / "app/run-worker.py"),
                                 "worker", "--config", str(BASE / "config/worker.json")],
            "WorkingDirectory": str(BASE / "app"), "RunAtLoad": True, "KeepAlive": True,
            "ThrottleInterval": 15, "ExitTimeOut": 45, "Umask": 0o077,
            "ProcessType": "Standard", "SessionCreate": False,
            "EnvironmentVariables": {"HOME": str(BASE / "home"), "PYTHONDONTWRITEBYTECODE": "1"},
            "StandardOutPath": str(BASE / "logs/worker.stdout.log"),
            "StandardErrorPath": str(BASE / "logs/worker.stderr.log")}


def as_worker(value, argv, timeout=180):
    return command(["/usr/bin/sudo", "-u", ACCOUNT, "--", "/usr/bin/env", "-i",
                    "HOME=" + str(BASE / "home"), "PATH=/usr/bin:/bin:/usr/sbin:/sbin",
                    "PYTHONDONTWRITEBYTECODE=1", *argv], timeout=timeout, cwd=BASE / "app")


def apply(value):
    require(os.geteuid() == 0, "installation requires macOS administrator authentication")
    preflight(value)
    BASE.mkdir(mode=0o750)
    BASE.chmod(0o750)
    os.chown(BASE, 0, value["gid"])
    journal(value, "installing")
    try:
        account_property("Groups", "GeneratedUID", value["group_uuid"])
        account_property("Groups", "PrimaryGroupID", value["gid"])
        account_property("Groups", "RealName", "Macqueue service")
        account_property("Users", "GeneratedUID", value["account_uuid"])
        # No password or shadow hash is created, even temporarily.
        for key, item in (("Password", "*"), ("IsHidden", "1"), ("UserShell", "/usr/bin/false"),
                          ("UniqueID", value["uid"]), ("PrimaryGroupID", value["gid"]),
                          ("NFSHomeDirectory", BASE / "home"), ("RealName", "Macqueue Worker")):
            account_property("Users", key, item)
        account_property("Groups", "GroupMembership", ACCOUNT)
        account_property("Groups", "GroupMembers", value["account_uuid"])
        for name in ("app", "config", "toolchain", "mirrors", "diagnostics"):
            (BASE / name).mkdir(mode=0o750)
            (BASE / name).chmod(0o750)
            os.chown(BASE / name, 0, value["gid"])
        for name in ("home", "state", "cargo", "logs"):
            (BASE / name).mkdir(mode=0o700)
            os.chown(BASE / name, value["uid"], value["gid"])
        policy = {"policyCategoryAuthentication": [{"policyIdentifier": "local.macqueue.deny-login",
                   "policyContent": "FALSEPREDICATE"}]}
        write(BASE / "config/no-login.plist", plistlib.dumps(policy), 0o640, gid=value["gid"])
        command(["/usr/bin/pwpolicy", "-u", ACCOUNT, "-setaccountpolicies", BASE / "config/no-login.plist"])
        # Set policy before disabling authentication; pwpolicy can reject an
        # already-disabled target even when called by root.
        account_property("Users", "AuthenticationAuthority", ";DisabledUser;")
        repo = Path(value["source"])
        shutil.copytree(repo / "macqueue", BASE / "app/macqueue", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for source, dest in ((repo / "deploy/macos/install.py", BASE / "rollback.py"),
                             (repo / "deploy/macos/smoke.py", BASE / "app/smoke.py")):
            shutil.copy2(source, dest)
            dest.chmod(0o640)
            os.chown(dest, 0, value["gid"])
        entry = "from pathlib import Path\nimport sys\nsys.path.insert(0, str(Path(__file__).resolve().parent))\nfrom macqueue.cli import main\nmain()\n"
        write(BASE / "app/run-worker.py", entry.encode(), 0o640, gid=value["gid"])
        print("Copying the existing Rust toolchain into /Library/Macqueue/toolchain/rust ...", flush=True)
        shutil.copytree(value["toolchain_source"], BASE / "toolchain/rust", symlinks=True)
        for parent in (BASE / "app/macqueue", BASE / "toolchain/rust"):
            for path in [parent, *parent.rglob("*")]:
                if path.is_symlink():
                    require(path.resolve().is_relative_to(parent), "toolchain/application symlink escapes its copied tree")
                    continue
                os.chown(path, 0, value["gid"])
                path.chmod(0o750 if path.is_dir() or path.stat().st_mode & 0o111 else 0o640)
        git = Path(value["developer_dir"]) / "usr/bin/git"
        command([git, "-c", "core.hooksPath=/dev/null", "init", "--bare", BASE / "mirrors/seedfinder.git"],
                env={"PATH": "/usr/bin:/bin", "HOME": str(BASE / "home"), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
        for path in [BASE / "mirrors/seedfinder.git", *(BASE / "mirrors/seedfinder.git").rglob("*")]:
            os.chown(path, 0, value["gid"])
            path.chmod(0o750 if path.is_dir() or path.stat().st_mode & 0o111 else 0o640)
        # Only the worker token is generated; no queue or agent credential is installed.
        write(BASE / "config/worker.token", (secrets.token_urlsafe(48) + "\n").encode(), uid=value["uid"], gid=value["gid"])
        config = {"server_url": value["server_url"], "token_file": str(BASE / "config/worker.token"),
                  "worker_id": "macbook-pro", "state_dir": str(BASE / "state"), "cargo_home": str(BASE / "cargo"),
                  "rust_toolchain": str(BASE / "toolchain/rust"), "developer_dir": value["developer_dir"],
                  "projects": {"seedfinder": str(BASE / "mirrors/seedfinder.git")},
                  "capabilities": ["cargo", "benchmark", "inspect"], "read_roots": [],
                  "max_job_bytes": 20 * 1024**3, "min_free_bytes": 5 * 1024**3}
        write(BASE / "config/worker.json", (json.dumps(config, indent=2) + "\n").encode(), 0o640, gid=value["gid"])
        write(BASE / "state/PAUSED", b"Waiting for repository and VPS configuration.\n", uid=value["uid"], gid=value["gid"])
        print("Checking sandbox and Cargo execution as the disabled service account ...", flush=True)
        for name, argv in (("doctor", ["run-worker.py", "doctor", "--config", str(BASE / "config/worker.json")]),
                           ("smoke", ["smoke.py", str(BASE / "config/worker.json")])):
            result = as_worker(value, [value["python"], "-I", str(BASE / "app" / argv[0]), *argv[1:]], timeout=300)
            write(BASE / "diagnostics" / (name + ".json"), result.stdout.encode(), 0o640, gid=value["gid"])
        write(PLIST, plistlib.dumps(daemon(value)), 0o644)
        value["plist_sha256"] = sha(PLIST)
        journal(value, "starting")
        command(["/bin/launchctl", "bootstrap", "system", PLIST])
        deadline = time.monotonic() + 60
        while True:
            log = BASE / "logs/worker.stderr.log"
            if log.exists() and "ready; paused=True" in log.read_text():
                break
            require(time.monotonic() < deadline, "LaunchDaemon startup did not finish: " + (log.read_text()[-3000:] if log.exists() else "no log"))
            time.sleep(1)
        report = verify(value)
        write(BASE / "diagnostics/installation.json", (json.dumps(report, indent=2) + "\n").encode(), 0o640, gid=value["gid"])
        journal(value, "installed-paused")
        print(json.dumps(report, indent=2), flush=True)
    except BaseException as original:
        print("Original installation error: " + repr(original), flush=True)
        print("Installation failed; rolling back newly created system resources.", flush=True)
        try:
            rollback(value, purge=True)
        except Exception as cleanup_error:
            print("Rollback was blocked: " + str(cleanup_error), flush=True)
            print("Use an administrator Terminal session to recover; do not disable SIP.", flush=True)
        raise


def verify(value):
    require(os.geteuid() == 0, "verification requires administrator access")
    for kind, guid in (("Users", value["account_uuid"]), ("Groups", value["group_uuid"])):
        require(guid in ds(kind, "GeneratedUID").stdout, "account/group UUID does not match installation")
    for key, expected in (("IsHidden", "1"), ("UserShell", "/usr/bin/false"), ("Password", "*"),
                          ("UniqueID", str(value["uid"])), ("PrimaryGroupID", str(value["gid"]))):
        actual = ds("Users", key).stdout.strip().removeprefix("dsAttrTypeNative:")
        require(actual == key + ": " + expected, "unexpected account property: " + key)
    require("DisabledUser" in ds("Users", "AuthenticationAuthority").stdout, "authentication authority is not disabled")
    # Read stored policy without trying to authenticate the disabled account.
    policy = ds("Users", "dsAttrTypeNative:accountPolicyData").stdout
    require("FALSEPREDICATE" in policy and "local.macqueue.deny-login" in policy, "deny-login account policy is missing")
    denied = command(["/usr/bin/pwpolicy", "-u", ACCOUNT, "-authentication-allowed"], check=False)
    require(denied.returncode != 0 or "not allowed" in (denied.stdout + denied.stderr).lower(), "account policy did not deny authentication")
    for password in ("", "macqueue-invalid-login-probe"):
        require(command(["/usr/bin/dscl", ".", "-authonly", ACCOUNT, password], check=False).returncode != 0,
                "service account unexpectedly authenticated")
    membership = command(["/usr/bin/dsmemberutil", "checkmembership", "-U", ACCOUNT, "-G", "admin"]).stdout
    require("not a member" in membership, "worker must not be an administrator")
    require(command(["/bin/launchctl", "print", "gui/" + str(value["uid"])], check=False).returncode != 0, "worker unexpectedly has a GUI session")
    token = command(["/usr/sbin/sysadminctl", "-secureTokenStatus", ACCOUNT], check=False)
    require("DISABLED" in token.stdout + token.stderr, "worker must not have a FileVault secure token")
    service = command(["/bin/launchctl", "print", "system/" + LABEL]).stdout
    require("state = running" in service, "LaunchDaemon is not running")
    match = re.search(r"\bpid = (\d+)", service)
    require(match is not None, "LaunchDaemon has no PID")
    pid = int(match.group(1))
    process_uid = command(["/bin/ps", "-o", "uid=", "-p", str(pid)]).stdout.strip()
    require(process_uid == str(value["uid"]), "LaunchDaemon is not running as the dedicated UID")
    require((BASE / "state/PAUSED").is_file(), "initial installation must remain paused")
    for name in ("doctor", "smoke"):
        require(json.loads((BASE / "diagnostics" / (name + ".json")).read_text())["ok"], name + " did not pass")
    return {"ok": True, "account": ACCOUNT, "uid": value["uid"], "hidden": True, "authentication": "disabled",
            "gui_session": False, "secure_token": False, "admin": False, "daemon_pid": pid,
            "daemon_domain": "system", "worker_state": "paused", "sandbox": "passed", "cargo_smoke": "passed",
            "base": str(BASE), "rollback": "sudo /usr/bin/python3 /Library/Macqueue/rollback.py rollback"}


def recover(previous):
    require(os.geteuid() == 0, "recovery requires administrator authentication in Terminal")
    current = json.loads((BASE / "installation.json").read_text())
    require(current["installation_id"] == previous["installation_id"], "recovery plan does not match the partial installation")
    require(current["status"] in ("installing", "starting"), "recovery is only for an incomplete installation")
    require(not (BASE / "state/ACTIVE.json").exists(), "an active job needs operator review before recovery")
    # Prove rollback works in this authorization context before installing again.
    rollback(current, purge=True)
    updated = plan(SimpleNamespace(source=previous["source"], python=previous["python"], toolchain=previous["toolchain_source"]))
    apply(updated)


def rollback(value, purge=False):
    require(os.geteuid() == 0, "rollback requires administrator access")
    require(value["base"] == str(BASE) and value["plist"] == str(PLIST), "unexpected installation paths")
    require(not BASE.is_symlink(), "installation directory is a symlink")
    receipt = json.loads((BASE / "installation.json").read_text())
    require(receipt["installation_id"] == value["installation_id"], "installation ID mismatch")
    for kind, guid in (("Users", value["account_uuid"]), ("Groups", value["group_uuid"])):
        record = ds(kind, "GeneratedUID")
        require(record.returncode != 0 or guid in record.stdout, "refusing to remove an account/group with a different UUID")
    if PLIST.exists():
        require(not PLIST.is_symlink(), "plist is a symlink")
        if value.get("plist_sha256"):
            require(sha(PLIST) == value["plist_sha256"], "plist changed; review before rollback")
        command(["/bin/launchctl", "bootout", "system/" + LABEL], check=False)
        require(command(["/bin/launchctl", "print", "system/" + LABEL], check=False).returncode != 0, "service is still loaded")
        PLIST.unlink()
    # Never kill a user-supplied/reused PID. Refuse account removal while its processes remain.
    deadline = time.monotonic() + 50
    while command(["/usr/bin/pgrep", "-U", str(value["uid"])], check=False).returncode == 0:
        require(time.monotonic() < deadline, "worker processes remain; inspect them before retrying rollback")
        time.sleep(1)
    for kind in ("Users", "Groups"):
        if ds(kind).returncode == 0:
            command(["/usr/bin/dscl", ".", "-delete", "/" + kind + "/" + ACCOUNT])
    if purge:
        shutil.rmtree(BASE)
        print("Rollback complete: new account, group, daemon, and installation files removed.", flush=True)
    else:
        archive = BASE.with_name("Macqueue.removed-" + value["installation_id"])
        require(not archive.exists(), "rollback archive already exists")
        BASE.rename(archive)
        os.chown(archive, 0, 0)
        archive.chmod(0o700)
        print(json.dumps({"removed": [ACCOUNT, LABEL], "retained_root_only_archive": str(archive)}, indent=2), flush=True)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--source", default=str(REPO))
    p.add_argument("--python", required=True)
    p.add_argument("--toolchain", required=True)
    for action in ("apply", "recover"):
        p = sub.add_parser(action)
        p.add_argument("--plan", type=Path, required=True)
    sub.add_parser("verify")
    p = sub.add_parser("rollback")
    p.add_argument("--purge", action="store_true", help="delete only this installation's files instead of retaining an archive")
    args = parser.parse_args()
    if args.action == "plan":
        print(json.dumps(plan(args), indent=2))
    elif args.action == "apply":
        apply(json.loads(args.plan.read_text()))
    elif args.action == "recover":
        recover(json.loads(args.plan.read_text()))
    else:
        value = json.loads((BASE / "installation.json").read_text())
        if args.action == "verify":
            print(json.dumps(verify(value), indent=2))
        else:
            rollback(value, args.purge)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print("macqueue setup: " + str(exc), file=sys.stderr)
        sys.exit(1)
