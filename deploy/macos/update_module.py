#!/usr/bin/env python3
"""Install one hash-pinned worker module and retain its reversible update receipt."""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

BASE = Path("/Library/Macqueue")


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def restart():
    log = BASE / "logs/worker.stderr.log"
    offset = log.stat().st_size
    subprocess.run(["/bin/launchctl", "kickstart", "-k", "system/local.macqueue.worker"], check=True)
    for _ in range(120):
        with log.open() as stream:
            stream.seek(offset)
            if "ready; paused=True" in stream.read():
                return
        time.sleep(0.5)
    raise RuntimeError("worker did not restart paused; inspect its log")


def replace(source, target, gid):
    temp = target.with_suffix(".py.new")
    require(not temp.exists() and not temp.is_symlink(), "temporary update file already exists")
    shutil.copyfile(source, temp)
    os.chown(temp, 0, gid)
    temp.chmod(0o640)
    temp.replace(target)


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)
    s = sub.add_parser("apply")
    s.add_argument("--source", type=Path, required=True)
    s.add_argument("--before", required=True)
    s.add_argument("--after", required=True)
    s = sub.add_parser("rollback")
    s.add_argument("--receipt", type=Path, required=True)
    args = p.parse_args()
    require(os.geteuid() == 0, "requires macOS administrator authentication")
    installation = json.loads((BASE / "installation.json").read_text())
    if args.action == "apply":
        name = args.source.name
        require(re.fullmatch(r"[a-z_]+\.py", name), "invalid module filename")
        target = BASE / "app/macqueue" / name
        require(target.is_file() and not target.is_symlink(), "installed module missing or symlinked")
        require(not args.source.is_symlink() and sha(args.source) == args.after, "update source hash changed")
        if sha(target) == args.after:
            print(json.dumps({"already_installed": True, "module": name}))
            return
        require(sha(target) == args.before, "installed module differs from reviewed version")
        receipt = {"installation_id": installation["installation_id"], "module": name,
                   "before": args.before, "after": args.after, "status": "prepared",
                   "was_paused": (BASE / "state/PAUSED").exists()}
        backup = BASE / "updates" / uuid.uuid4().hex
        backup.mkdir(parents=True, mode=0o700)
        backup.parent.chmod(0o700)
        shutil.copy2(target, backup / "before.py")
        shutil.copy2(Path(__file__), backup / "update.py")
        path = backup / "receipt.json"
        path.write_text(json.dumps(receipt, indent=2) + "\n")
    else:
        path = args.receipt.resolve()
        require(path.parent.parent == BASE / "updates" and path.name == "receipt.json", "unexpected receipt path")
        receipt = json.loads(path.read_text())
        require(receipt["installation_id"] == installation["installation_id"], "installation changed")
        require(re.fullmatch(r"[a-z_]+\.py", receipt["module"]), "invalid receipt module")
        require(receipt["status"] == "installed", "update not installed or already rolled back")
        target = BASE / "app/macqueue" / receipt["module"]
        require(not target.is_symlink() and sha(target) == receipt["after"], "module changed since update")
        require(sha(path.parent / "before.py") == receipt["before"], "backup changed")
    (BASE / "state/PAUSED").touch(mode=0o600)
    require(not (BASE / "state/ACTIVE.json").exists(), "paused new claims; wait for the active job before retrying")
    replace(args.source if args.action == "apply" else path.parent / "before.py", target, installation["gid"])
    receipt["status"] = "installed" if args.action == "apply" else "rolled-back"
    path.write_text(json.dumps(receipt, indent=2) + "\n")
    restart()
    if not receipt["was_paused"]:
        (BASE / "state/PAUSED").unlink()
    print(json.dumps({"ok": True, "module": receipt["module"], "status": receipt["status"],
                      "paused": receipt["was_paused"],
                      "rollback": "sudo /usr/bin/python3 " + str(path.parent / "update.py") + " rollback --receipt " + str(path)}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(str(exc))
