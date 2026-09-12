import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .common import Invalid, integer, json_bytes, keys, name, require
from .schema import MAX_INPUT_BYTES, MAX_JOB_BYTES, validate_job
from .store import Store

MAX_ARTIFACT = 512 * 1024 * 1024


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address, store, submit_token, worker_token, max_artifact=MAX_ARTIFACT):
        require(submit_token != worker_token, "submitter and worker tokens must differ")
        super().__init__(address, Handler)
        self.store = store
        self.tokens = {"submit": submit_token, "worker": worker_token}
        self.max_artifact = max_artifact
        self.changed = threading.Condition()
        self.input_lock = threading.Lock()
        self.input_reserved = 0

    def wake(self):
        with self.changed:
            self.changed.notify_all()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "macqueue"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, fmt, *args):
        # No request headers, job contents, or tokens in HTTP access logs.
        pass

    def reply(self, status, value):
        body = json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def length(self, limit):
        require(not self.headers.get("Transfer-Encoding"), "chunked uploads are not supported")
        raw = self.headers.get("Content-Length", "0")
        require(raw.isdecimal(), "invalid Content-Length")
        count = int(raw)
        require(count <= limit, "request exceeds size limit")
        return count

    def body(self, maximum=1024 * 1024):
        count = self.length(maximum)
        body = self.rfile.read(count)
        require(len(body) == count, "truncated request body")
        return json.loads(body, parse_constant=lambda _: (_ for _ in ()).throw(Invalid("invalid JSON number"))) if body else {}

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def dispatch(self, method):
        try:
            url = urlsplit(self.path)
            path = url.path
            if method == "GET" and path == "/healthz":
                self.reply(200, {"ok": True})
                return
            role = "worker" if path.startswith("/v1/worker/") else "submit"
            expected = "Bearer " + self.server.tokens[role]
            if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected.encode()):
                self.reply(401, {"error": "unauthorized"})
                return
            store = self.server.store
            package = re.fullmatch(r"/v1/(worker/)?inputs/([a-f0-9]{64})", path)
            if package:
                worker, sha = package.groups()
                directory = store.directory / "inputs"
                directory.mkdir(mode=0o700, exist_ok=True)
                destination = directory / (sha + ".tar.gz")
                if not worker and method == "PUT":
                    count = self.length(MAX_INPUT_BYTES)
                    require(count > 0, "empty source package")
                    with self.server.input_lock:
                        files = list(directory.iterdir())
                        require(len(files) < 1000, "source upload count budget exceeded")
                        require(sum(p.stat().st_size for p in files) + self.server.input_reserved + count <= 16 * 1024**3, "source upload storage budget exceeded")
                        self.server.input_reserved += count
                    temp = directory / (sha + "." + secrets.token_hex(8) + ".upload")
                    actual = hashlib.sha256()
                    try:
                        with temp.open("xb") as stream:
                            remaining = count
                            while remaining:
                                data = self.rfile.read(min(1024 * 1024, remaining))
                                require(data, "truncated source package")
                                stream.write(data)
                                actual.update(data)
                                remaining -= len(data)
                            stream.flush()
                            os.fsync(stream.fileno())
                        require(actual.hexdigest() == sha, "source package checksum mismatch")
                        # Publication is immutable and idempotent by content hash.
                        try:
                            os.link(temp, destination)
                        except FileExistsError:
                            pass
                        self.reply(200, {"sha256": sha, "bytes": count})
                    finally:
                        temp.unlink(missing_ok=True)
                        with self.server.input_lock:
                            self.server.input_reserved -= count
                    return
                if worker and method == "GET":
                    job_id = self.headers.get("X-Job-ID", "")
                    with store.transaction() as db:
                        row = store.owned(db, job_id, self.headers.get("X-Job-Lease", ""))
                        spec = json.loads(row["spec"])
                        require(len(spec["steps"]) == 1 and spec["steps"][0]["op"] == "provision"
                                and spec["steps"][0]["sha256"] == sha, "input does not belong to this provisioning lease")
                    require(destination.is_file(), "source package has not been uploaded")
                    with destination.open("rb") as stream:
                        self.send_response(200)
                        self.send_header("Content-Length", str(os.fstat(stream.fileno()).st_size))
                        self.send_header("X-Input-SHA256", sha)
                        self.send_header("Connection", "close")
                        self.end_headers()
                        while data := stream.read(1024 * 1024):
                            self.wfile.write(data)
                    self.close_connection = True
                    return
                raise KeyError("route not found")
            if path == "/v1/jobs":
                if method == "POST":
                    spec = validate_job(self.body(MAX_JOB_BYTES))
                    key = self.headers.get("Idempotency-Key", "")
                    require(re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", key), "provide an Idempotency-Key")
                    self.reply(201, store.submit(spec, key))
                    self.server.wake()
                    return
                if method == "GET":
                    self.reply(200, store.list())
                    return
            if path == "/v1/worker/claim" and method == "POST":
                body = self.body()
                keys(body, ("worker", "projects"))
                name(body["worker"])
                require(isinstance(body["projects"], list) and 1 <= len(body["projects"]) <= 100, "invalid projects")
                for project in body["projects"]:
                    name(project)
                deadline = time.monotonic() + 20
                with self.server.changed:
                    while True:
                        claimed = store.claim(body["worker"], body["projects"])
                        if claimed or time.monotonic() >= deadline:
                            self.reply(200, claimed)
                            return
                        self.server.changed.wait(timeout=min(1, deadline - time.monotonic()))
            match = re.fullmatch(r"/v1/(worker/)?jobs/([a-f0-9]{32})(?:/(heartbeat|logs|finish|cancel|artifact))?", path)
            if not match:
                raise KeyError("route not found")
            worker, job_id, action = match.groups()
            lease = self.headers.get("X-Job-Lease", "")
            if worker:
                if action == "heartbeat" and method == "POST":
                    self.reply(200, store.heartbeat(job_id, lease))
                    return
                if action == "logs" and method == "POST":
                    body = self.body()
                    keys(body, ("seq", "text"))
                    integer(body["seq"], 0, 1000000, "log sequence")
                    require(isinstance(body["text"], str) and len(body["text"].encode()) <= 65536, "log chunk exceeds 64 KiB")
                    store.append_log(job_id, lease, body["seq"], body["text"])
                    self.reply(200, {"ok": True})
                    return
                if action == "finish" and method == "POST":
                    body = self.body()
                    keys(body, ("status", "result"))
                    self.reply(200, store.finish(job_id, lease, body["status"], body["result"]))
                    self.server.wake()
                    return
                if action == "artifact" and method == "PUT":
                    store.heartbeat(job_id, lease)
                    count = self.length(self.server.max_artifact)
                    require(count > 0, "empty artifact")
                    filename = job_id + ".tar.gz"
                    destination = store.directory / filename
                    temp = store.directory / (job_id + "." + secrets.token_hex(8) + ".upload")
                    sha = hashlib.sha256()
                    try:
                        with temp.open("xb") as stream:
                            remaining = count
                            while remaining:
                                chunk = self.rfile.read(min(1024 * 1024, remaining))
                                require(bool(chunk), "truncated artifact")
                                stream.write(chunk)
                                sha.update(chunk)
                                remaining -= len(chunk)
                            stream.flush()
                            os.fsync(stream.fileno())
                        require(self.headers.get("X-Artifact-SHA256") == sha.hexdigest(), "artifact checksum mismatch")
                        # Recheck lease before publishing; stale uploads never replace current results.
                        with store.transaction() as db:
                            store.owned(db, job_id, lease)
                            temp.replace(destination)
                            db.execute("UPDATE jobs SET artifact=?,artifact_sha256=? WHERE id=?",
                                       (filename, sha.hexdigest(), job_id))
                        self.reply(200, {"sha256": sha.hexdigest(), "bytes": count})
                    finally:
                        temp.unlink(missing_ok=True)
                    return
            else:
                if action is None and method == "GET":
                    self.reply(200, store.get(job_id))
                    return
                if action == "cancel" and method == "POST":
                    self.reply(200, store.cancel(job_id))
                    self.server.wake()
                    return
                if action == "logs" and method == "GET":
                    after = int(parse_qs(url.query).get("after", ["-1"])[0])
                    self.reply(200, store.logs(job_id, after))
                    return
                if action == "artifact" and method == "GET":
                    path, sha = store.artifact(job_id)
                    with path.open("rb") as stream:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/gzip")
                        self.send_header("Content-Length", str(os.fstat(stream.fileno()).st_size))
                        self.send_header("X-Artifact-SHA256", sha)
                        self.send_header("Content-Disposition", f'attachment; filename="{job_id}.tar.gz"')
                        self.send_header("Connection", "close")
                        self.end_headers()
                        while chunk := stream.read(1024 * 1024):
                            self.wfile.write(chunk)
                        self.close_connection = True
                    return
            raise KeyError("route not found")
        except KeyError as exc:
            self.reply(404, {"error": str(exc)})
        except (Invalid, ValueError, TypeError) as exc:
            self.reply(400, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            self.reply(500, {"error": "internal server error"})
