#!/usr/bin/env python3
"""Exercise the installed API using a synthetic project, without executing Mac jobs."""
import io
import json
import os
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from macqueue.client import Client, RemoteError
from macqueue.common import read_secret
from macqueue.plans import correctness


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run with sudo; this check reads the private worker credential.")
    config = json.loads(Path("/opt/macqueue/client.json").read_text())
    submit = Client(config["url"], read_secret(Path("/etc/macqueue/submit.token")), allow_http=True)
    worker = Client(config["url"], read_secret(Path("/etc/macqueue/worker.token")), allow_http=True)
    for client, method, path, body in (
            (Client(config["url"], "invalid", allow_http=True), "GET", "/v1/jobs", None),
            (worker, "GET", "/v1/jobs", None),
            (submit, "POST", "/v1/worker/claim", {"worker": "setup", "projects": ["setup"]})):
        try:
            client.request(method, path, body)
        except RemoteError as exc:
            if exc.status != 401:
                raise
        else:
            raise RuntimeError("authentication boundary failed")
    project = "setup-" + uuid.uuid4().hex
    spec = correctness(project, "a" * 40)
    spec["label"] = "VPS API setup check; synthetic worker; no commands executed"
    key = uuid.uuid4().hex
    submitted = submit.request("POST", "/v1/jobs", spec, key=key)
    job_id = submitted["id"]
    if submit.request("POST", "/v1/jobs", spec, key=key)["id"] != job_id:
        raise RuntimeError("idempotency failed")
    claim = worker.request("POST", "/v1/worker/claim", {"worker": "vps-setup", "projects": [project]})
    if not claim or claim["job"]["id"] != job_id:
        raise RuntimeError("synthetic claim failed")
    lease = claim["lease"]
    base = f"/v1/worker/jobs/{job_id}"
    if worker.request("POST", base + "/heartbeat", {}, lease=lease)["cancel"]:
        raise RuntimeError("unexpected cancellation")
    worker.request("POST", base + "/logs", {"seq": 0, "text": "VPS artifact transport check passed.\n"}, lease=lease)
    with tempfile.TemporaryDirectory(prefix="macqueue-smoke-") as temp:
        archive = Path(temp) / "artifact.tar.gz"
        payload = b'{"check":"VPS artifact transport","mac_execution":false}\n'
        with tarfile.open(archive, "w:gz") as tar:
            info = tarfile.TarInfo("setup.json")
            info.size = len(payload)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(payload))
        receipt = worker.upload(job_id, lease, archive)
        finished = worker.request("POST", base + "/finish", {"status": "succeeded", "result": {"synthetic": True}}, lease=lease)
        if finished["status"] != "succeeded":
            raise RuntimeError("completion failed")
        destination = Path(temp) / "download.tar.gz"
        submit.download(job_id, destination)
        if archive.read_bytes() != destination.read_bytes():
            raise RuntimeError("artifact roundtrip failed")
    cancelled = submit.request("POST", "/v1/jobs", spec, key=uuid.uuid4().hex)
    if submit.request("POST", f"/v1/jobs/{cancelled['id']}/cancel", {})["status"] != "cancelled":
        raise RuntimeError("queued cancellation failed")
    if submit.request("GET", f"/v1/jobs/{job_id}/logs")[0]["seq"] != 0:
        raise RuntimeError("log roundtrip failed")
    print(json.dumps({"ok": True, "authentication_and_roles": "passed", "idempotency": "passed",
                      "claim_heartbeat_logs_finish": "passed", "artifact_sha256": receipt["sha256"],
                      "succeeded_job": job_id, "cancelled_job": cancelled["id"], "mac_execution": False}, indent=2))


if __name__ == "__main__":
    main()
