#!/usr/bin/env python3
"""New-install-only Debian setup with an ownership-checked rollback receipt."""
import argparse
import grp
import hashlib
import ipaddress
import json
import os
import pwd
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request
import uuid
from pathlib import Path

APP = Path("/opt/macqueue")
CONFIG = Path("/etc/macqueue")
STATE = Path("/var/lib/macqueue")
UNIT = Path("/etc/systemd/system/macqueue.service")
LINK = Path("/etc/systemd/system/multi-user.target.wants/macqueue.service")
WRAPPER = Path("/usr/local/bin/macqueue")
ACCOUNT = "macqueue"


def require(value, message):
    if not value:
        raise RuntimeError(message)


def run(*argv, check=True):
    result = subprocess.run(list(map(str, argv)), text=True, capture_output=True, timeout=120)
    if check and result.returncode:
        raise RuntimeError(f"{argv[0]} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def files(source):
    paths = sorted((source / "macqueue").glob("*.py"))
    paths += [source / "deploy/debian" / name for name in ("install.py", "agent.py", "smoke.py", "serve.py")]
    paths += [source / "docs/debian-setup.md"]
    require(len(paths) > 4 and all(p.is_file() and not p.is_symlink() for p in paths), "missing or symlinked source")
    return {str(p.relative_to(source)): sha(p) for p in paths}


def exists(path):
    return path.exists() or path.is_symlink()


def identity(kind):
    try:
        return pwd.getpwnam(ACCOUNT) if kind == "user" else grp.getgrnam(ACCOUNT)
    except KeyError:
        return None


def preflight(value):
    require(sys.platform == "linux" and sys.version_info >= (3, 11), "requires Linux and Python 3.11+")
    release = Path("/etc/os-release").read_text().splitlines()
    require('ID=debian' in release and 'VERSION_ID="13"' in release, "this installer targets Debian 13")
    require(ipaddress.ip_address(value["host"]) in ipaddress.ip_network("100.64.0.0/10"), "bind only to a Tailscale IPv4 address")
    require(value["host"] in run("/usr/bin/tailscale", "ip", "-4").stdout.splitlines(), "address is not on this machine")
    require(1024 <= value["port"] <= 65535, "use an unprivileged port")
    owner = pwd.getpwnam(value["agent"])
    require(owner.pw_uid == value["agent_uid"] != 0 and owner.pw_gid == value["agent_gid"], "agent identity changed")
    require(value["agent_config"] == str(Path(owner.pw_dir) / ".config/macqueue"), "unexpected client path")
    require(Path(value["agent_config"]).parent.is_dir(), "agent .config directory must already exist")
    for path in (APP, CONFIG, STATE, UNIT, LINK, WRAPPER, Path("/run/macqueue"), Path(value["agent_config"])):
        require(not exists(path), f"refusing to replace existing path: {path}")
    require(identity("user") is None and identity("group") is None, "service identity already exists")
    require(value["uid"] not in {p.pw_uid for p in pwd.getpwall()} and
            value["uid"] not in {g.gr_gid for g in grp.getgrall()}, "planned UID/GID now occupied")
    require(run("systemctl", "show", "macqueue.service", "--property=LoadState", "--value").stdout.strip() == "not-found", "service already registered")
    require(files(Path(value["source"])) == value["source_hashes"], "source changed since planning")
    with socket.socket() as probe:
        probe.bind((value["host"], value["port"]))
    for path in (value["repository"], value["worktree"]):
        run("git", "-c", "safe.directory=" + path, "-C", path, "rev-parse", "--git-dir")


def plan(args):
    owner = pwd.getpwnam(args.agent)
    used = {p.pw_uid for p in pwd.getpwall()} | {g.gr_gid for g in grp.getgrall()}
    source = args.source.resolve()
    value = {"version": 1, "installation_id": uuid.uuid4().hex, "host": args.host, "port": args.port,
             "source": str(source), "source_hashes": files(source), "agent": args.agent,
             "agent_uid": owner.pw_uid, "agent_gid": owner.pw_gid,
             "agent_config": str(Path(owner.pw_dir) / ".config/macqueue"),
             "uid": next(i for i in range(999, 499, -1) if i not in used),
             "repository": str(args.repository.resolve()), "worktree": str(args.worktree.resolve()),
             "created_dirs": [], "external_hashes": {}, "status": "planned"}
    preflight(value)
    return value


def write(path, content, mode=0o600, uid=0, gid=0):
    data = content.encode() if isinstance(content, str) else content
    with Path(path).open("xb") as stream:
        stream.write(data)
    os.chmod(path, mode)
    os.chown(path, uid, gid)


def journal(value):
    temp = APP / "installation.json.new"
    temp.write_text(json.dumps(value, indent=2) + "\n")
    temp.chmod(0o600)
    temp.replace(APP / "installation.json")


def unit(value):
    return f"""[Unit]
Description=Macqueue private pull-worker queue
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
User=macqueue
Group=macqueue
WorkingDirectory=/opt/macqueue
LoadCredential=submit.token:/etc/macqueue/submit.token
LoadCredential=worker.token:/etc/macqueue/worker.token
RuntimeDirectory=macqueue
RuntimeDirectoryMode=0700
ExecStart=/usr/bin/python3 -I -B /opt/macqueue/serve.py server --state /var/lib/macqueue --host {value['host']} --port {value['port']} --allow-http-private --submit-token-file /run/macqueue/submit.token --worker-token-file /run/macqueue/worker.token
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
RestrictSUIDSGID=true
RestrictRealtime=true
LockPersonality=true
CapabilityBoundingSet=
AmbientCapabilities=
RestrictAddressFamilies=AF_UNIX AF_INET
ReadWritePaths=/var/lib/macqueue
TasksMax=128
MemoryMax=512M

[Install]
WantedBy=multi-user.target
"""


def apply(value):
    require(os.geteuid() == 0, "apply requires sudo")
    os.umask(0o077)
    preflight(value)
    APP.mkdir(mode=0o755)
    APP.chmod(0o755)
    value["status"] = "installing"
    journal(value)
    # Install recovery first. Interrupted/failed installs can use this receipt.
    write(APP / "install.py", Path(value["source"], "deploy/debian/install.py").read_bytes(), 0o644)
    try:
        run("groupadd", "--system", "--gid", value["uid"], ACCOUNT)
        run("useradd", "--system", "--uid", value["uid"], "--gid", value["uid"], "--no-user-group",
            "--no-create-home", "--no-log-init", "--home-dir", STATE, "--shell", "/usr/sbin/nologin",
            "--comment", "Macqueue " + value["installation_id"], ACCOUNT)
        for path, uid, gid, mode in ((CONFIG, 0, 0, 0o700), (STATE, value["uid"], value["uid"], 0o700),
                                    (Path(value["agent_config"]), value["agent_uid"], value["agent_gid"], 0o700)):
            path.mkdir(mode=mode)
            os.chown(path, uid, gid)
            value["created_dirs"].append(str(path))
            journal(value)
        source = Path(value["source"])
        (APP / "macqueue").mkdir(mode=0o755)
        (APP / "macqueue").chmod(0o755)
        for relative in value["source_hashes"]:
            if relative.startswith("macqueue/"):
                write(APP / relative, (source / relative).read_bytes(), 0o644)
        for name in ("agent.py", "smoke.py", "serve.py"):
            write(APP / name, (source / "deploy/debian" / name).read_bytes(), 0o644)
        write(APP / "run.py", "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parent))\nfrom macqueue.cli import main\nmain()\n", 0o644)
        write(APP / "AGENT.md", (source / "docs/debian-setup.md").read_bytes(), 0o644)
        for role in ("submit", "worker"):
            write(CONFIG / (role + ".token"), secrets.token_urlsafe(48) + "\n")
        token_path = Path(value["agent_config"]) / "submit.token"
        write(token_path, (CONFIG / "submit.token").read_bytes(), uid=value["agent_uid"], gid=value["agent_gid"])
        write(Path(value["agent_config"]) / "installation-id", value["installation_id"] + "\n",
              uid=value["agent_uid"], gid=value["agent_gid"])
        client = {"url": f"http://{value['host']}:{value['port']}", "token_file": str(token_path),
                  "repository": value["repository"], "worktree": value["worktree"]}
        write(APP / "client.json", json.dumps(client, indent=2) + "\n", 0o644)
        for path, data, mode in ((UNIT, unit(value), 0o644),
                                 (WRAPPER, '#!/bin/sh\nexec /usr/bin/python3 -I -B /opt/macqueue/agent.py "$@"\n', 0o755)):
            value["external_hashes"][str(path)] = hashlib.sha256(data.encode()).hexdigest()
            journal(value)
            write(path, data, mode)
        run("systemd-analyze", "verify", UNIT)
        run("systemctl", "daemon-reload")
        run("systemctl", "enable", "--now", "macqueue.service")
        result = verify(value)
        value["status"] = "installed"
        journal(value)
        return result
    except BaseException:
        value["status"] = "incomplete"
        journal(value)
        print("Setup incomplete; recover with sudo /usr/bin/python3 /opt/macqueue/install.py rollback", file=sys.stderr)
        raise


def verify(value):
    require(os.geteuid() == 0, "verify requires sudo")
    user = identity("user")
    require(user is not None and user.pw_uid == value["uid"] and user.pw_gid == value["uid"] and
            user.pw_gecos == "Macqueue " + value["installation_id"] and
            user.pw_shell == "/usr/sbin/nologin", "service account mismatch")
    require(run("passwd", "-S", ACCOUNT).stdout.split()[1] == "L", "service password must be locked")
    require(run("systemctl", "is-enabled", "macqueue.service").stdout.strip() == "enabled", "service not enabled")
    endpoint = f"http://{value['host']}:{value['port']}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(30):
        try:
            with opener.open(endpoint + "/healthz", timeout=2) as response:
                require(json.load(response)["ok"] is True, "health response failed")
            break
        except OSError:
            if attempt == 29:
                raise
            time.sleep(0.2)
    require(run("systemctl", "is-active", "macqueue.service").stdout.strip() == "active", "service not active")
    pid = int(run("systemctl", "show", "macqueue.service", "--property=MainPID", "--value").stdout)
    require(Path(f"/proc/{pid}").stat().st_uid == value["uid"], "daemon has wrong UID")
    listeners = run("ss", "-H", "-ltn", "sport", "=", str(value["port"])).stdout.splitlines()
    require(len(listeners) == 1 and listeners[0].split()[3] == f"{value['host']}:{value['port']}", "unexpected listener")
    for role in ("submit", "worker"):
        stat = (CONFIG / (role + ".token")).stat()
        require(stat.st_uid == 0 and stat.st_mode & 0o777 == 0o600, "credential permissions changed")
    return {"ok": True, "endpoint": endpoint, "account": ACCOUNT, "uid": value["uid"],
            "password": "locked", "shell": user.pw_shell, "pid": pid, "service": "enabled and active",
            "rollback": "sudo /usr/bin/python3 /opt/macqueue/install.py rollback"}


def rollback(value, purge=False):
    require(os.geteuid() == 0, "rollback requires sudo")
    require(APP.is_dir() and not APP.is_symlink() and APP.stat().st_uid == 0, "installation directory replaced")
    user, group = identity("user"), identity("group")
    if user:
        require(user.pw_uid == value["uid"] and user.pw_gid == value["uid"] and
                user.pw_gecos == "Macqueue " + value["installation_id"], "account identity changed; refusing deletion")
        require(not [p for p in pwd.getpwall() if p.pw_uid == value["uid"] and p.pw_name != ACCOUNT], "UID shared")
    if group:
        require(group.gr_gid == value["uid"] and not group.gr_mem and
                not [p for p in pwd.getpwall() if p.pw_gid == value["uid"] and p.pw_name != ACCOUNT], "group identity/membership changed")
    for name, digest in value["external_hashes"].items():
        path = Path(name)
        require(path in (UNIT, WRAPPER), "unexpected tracked file")
        if exists(path):
            require(path.is_file() and not path.is_symlink() and sha(path) == digest, f"changed file; preserve and review before rollback: {path}")
    require(not exists(UNIT) or str(UNIT) in value["external_hashes"], "untracked service appeared; refusing to stop it")
    fragment = run("systemctl", "show", "macqueue.service", "--property=FragmentPath", "--value").stdout.strip()
    require(fragment in ("", str(UNIT)), "a different service is loaded")
    if exists(LINK):
        require(LINK.is_symlink() and LINK.resolve() == UNIT, "service enable link changed")
    expected_dirs = {str(CONFIG), str(STATE), value["agent_config"]}
    for name in value["created_dirs"]:
        require(name in expected_dirs, "unexpected tracked directory")
        require(not Path(name).is_symlink(), f"directory replaced with symlink: {name}")
    agent_dir = Path(value["agent_config"])
    if str(agent_dir) in value["created_dirs"] and agent_dir.exists():
        marker = agent_dir / "installation-id"
        if marker.exists():
            require(not marker.is_symlink() and marker.read_text().strip() == value["installation_id"], "agent config replaced")
    run("systemctl", "disable", "--now", "macqueue.service", check=False)
    require(run("systemctl", "is-active", "macqueue.service", check=False).stdout.strip() != "active", "service still active")
    if user:
        require(run("pgrep", "-u", value["uid"], check=False).returncode == 1,
                "service account still has processes; stop those before rollback")
    paths = [APP, *map(Path, value["created_dirs"]), *map(Path, value["external_hashes"])]
    archive = None
    if not purge:
        archive = Path("/var/backups") / f"macqueue-{value['installation_id']}-{time.time_ns()}.tar.gz"
        with archive.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            with tarfile.open(fileobj=stream, mode="w:gz", dereference=False) as tar:
                for path in paths:
                    if exists(path):
                        tar.add(path, arcname=str(path).lstrip("/"))
    if user:
        run("userdel", ACCOUNT)
    if identity("group"):
        run("groupdel", ACCOUNT)
    if LINK.is_symlink():
        LINK.unlink()
    for path in paths[1:]:
        if path.is_dir():
            shutil.rmtree(path)
        elif exists(path):
            path.unlink()
    shutil.rmtree(APP)
    run("systemctl", "daemon-reload")
    run("systemctl", "reset-failed", "macqueue.service", check=False)
    return {"rolled_back": True, "archive": str(archive) if archive else None,
            "note": "removed only this installation; system journal and account-tool audit/backup records may remain"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--agent", required=True)
    p.add_argument("--repository", type=Path, required=True)
    p.add_argument("--worktree", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p = sub.add_parser("apply")
    p.add_argument("plan", type=Path)
    sub.add_parser("verify")
    p = sub.add_parser("rollback")
    p.add_argument("--purge", action="store_true", help="remove installation without keeping a private archive")
    args = parser.parse_args()
    try:
        if args.action == "plan":
            result = plan(args)
            with args.output.open("x") as stream:
                json.dump(result, stream, indent=2)
        elif args.action == "apply":
            result = apply(json.loads(args.plan.read_text()))
        else:
            value = json.loads((APP / "installation.json").read_text())
            result = verify(value) if args.action == "verify" else rollback(value, args.purge)
        print(json.dumps(result, indent=2))
    except (OSError, RuntimeError, ValueError, KeyError, StopIteration) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
