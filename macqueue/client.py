import hashlib
import http.client
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from .common import digest, json_bytes, require
from .schema import MAX_JOB_BYTES


class RemoteError(RuntimeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(f"HTTP {status}: {message}")


class Client:
    def __init__(self, url, token, *, allow_http=False):
        self.url = urlsplit(url)
        require(self.url.scheme in ("http", "https") and self.url.hostname, "expected an HTTP(S) URL")
        require(not self.url.username and not self.url.password and self.url.path in ("", "/")
                and not self.url.query and not self.url.fragment, "URL must contain only scheme and authority")
        require(self.url.scheme == "https" or allow_http or self.url.hostname in ("localhost", "127.0.0.1", "::1"),
                "remote plaintext HTTP requires explicit allow_http (use only over a private encrypted network)")
        self.token = token

    def connection(self, timeout=30):
        cls = http.client.HTTPSConnection if self.url.scheme == "https" else http.client.HTTPConnection
        return cls(self.url.hostname, self.url.port, timeout=timeout)

    def request(self, method, path, data=None, *, lease=None, key=None):
        headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}
        if lease:
            headers["X-Job-Lease"] = lease
        if key:
            headers["Idempotency-Key"] = key
        connection = self.connection()
        try:
            connection.request(method, path, body=json_bytes(data) if data is not None else None, headers=headers)
            response = connection.getresponse()
            carries_job = (path == "/v1/worker/claim" or path == "/v1/jobs" and method == "POST"
                           or re.fullmatch(r"/v1/jobs/[a-f0-9]{32}(/cancel)?", path))
            maximum = MAX_JOB_BYTES + 1024 * 1024 if carries_job else 4 * 1024 * 1024
            raw = response.read(maximum + 1)
            require(len(raw) <= maximum, "server response exceeds size limit")
            result = json.loads(raw)
            if response.status >= 300:
                raise RemoteError(response.status, result.get("error", "request failed"))
            return result
        finally:
            connection.close()

    def upload(self, job_id, lease, path):
        connection = self.connection(timeout=30)
        try:
            with Path(path).open("rb") as stream:
                connection.request("PUT", f"/v1/worker/jobs/{job_id}/artifact", body=stream, headers={
                    "Authorization": "Bearer " + self.token, "X-Job-Lease": lease,
                    "X-Artifact-SHA256": digest(path), "Content-Length": str(Path(path).stat().st_size),
                    "Content-Type": "application/gzip"})
                response = connection.getresponse()
                result = json.loads(response.read(65536))
                if response.status >= 300:
                    raise RemoteError(response.status, result.get("error", "upload failed"))
                return result
        finally:
            connection.close()

    def download(self, job_id, destination):
        destination = Path(destination)
        connection = self.connection()
        created = False
        try:
            connection.request("GET", f"/v1/jobs/{job_id}/artifact", headers={"Authorization": "Bearer " + self.token})
            response = connection.getresponse()
            if response.status != 200:
                raise RemoteError(response.status, response.read(65536).decode(errors="replace"))
            expected = response.getheader("X-Artifact-SHA256")
            sha = hashlib.sha256()
            with destination.open("xb") as stream:
                created = True
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    require(total <= 2 * 1024**3, "download exceeds 2 GiB")
                    sha.update(chunk)
                    stream.write(chunk)
            require(sha.hexdigest() == expected, "artifact checksum mismatch")
            return {"path": str(destination), "sha256": expected, "bytes": total}
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise
        finally:
            connection.close()
