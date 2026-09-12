import hashlib
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from macqueue.client import Client, RemoteError
from macqueue.common import Invalid
from macqueue.plans import correctness
from macqueue.server import Server
from macqueue.store import Store


def job():
    return correctness("seedfinder", "a" * 40)


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)

    def test_atomic_claim_and_idempotent_submit(self):
        first = self.store.submit(job(), "request-one")
        self.assertEqual(first["id"], self.store.submit(job(), "request-one")["id"])
        changed = job()
        changed["label"] = "other"
        with self.assertRaises(Invalid):
            self.store.submit(changed, "request-one")
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda i: self.store.claim(f"worker-{i}", ["seedfinder"]), range(8)))
        self.assertEqual(1, sum(c is not None for c in claims))
        self.assertEqual(self.store.get(first["id"])["status"], "running")

    def test_restart_preserves_queue_and_expired_lease_is_never_replayed(self):
        submitted = self.store.submit(job(), "first")
        reopened = Store(self.temp.name)
        claimed = reopened.claim("mac", ["seedfinder"])
        with reopened.connect() as db:
            db.execute("UPDATE jobs SET lease_until=0")
        db.close()
        self.assertEqual("lost", reopened.get(submitted["id"])["status"])
        self.assertIsNone(reopened.claim("mac", ["seedfinder"]))
        with self.assertRaises(Invalid):
            reopened.heartbeat(submitted["id"], claimed["lease"])
        with self.assertRaises(Invalid):
            reopened.finish(submitted["id"], claimed["lease"], "succeeded", {})

    def test_expiry_and_cancellation_are_durable(self):
        submitted = self.store.submit(job(), "first")
        self.assertEqual("cancelled", self.store.cancel(submitted["id"])["status"])
        second = self.store.submit(job(), "second")
        claim = self.store.claim("mac", ["seedfinder"])
        self.assertEqual("cancel_requested", self.store.cancel(second["id"])["status"])
        self.assertTrue(self.store.heartbeat(second["id"], claim["lease"])["cancel"])
        self.assertEqual("cancelled", self.store.finish(second["id"], claim["lease"], "failed", {"stopped": True})["status"])
        third = self.store.submit(job(), "third")
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET expires=0 WHERE id=?", (third["id"],))
        db.close()
        self.assertEqual("expired", self.store.get(third["id"])["status"])

    def test_fenced_logs_and_completion(self):
        submitted = self.store.submit(job(), "first")
        claim = self.store.claim("mac", ["seedfinder"])
        job_id, lease = submitted["id"], claim["lease"]
        with self.assertRaises(Invalid):
            self.store.append_log(job_id, "wrong", 0, "text")
        self.store.append_log(job_id, lease, 0, "text")
        self.store.append_log(job_id, lease, 0, "text")
        self.assertEqual([{ "seq": 0, "text": "text"}], self.store.logs(job_id, -1))
        with self.assertRaises(Invalid):
            self.store.append_log(job_id, lease, 0, "changed")
        with self.assertRaises(Invalid):
            self.store.finish(job_id, lease, "succeeded", {})
        self.store.finish(job_id, lease, "failed", {"error": "build"})
        self.store.finish(job_id, lease, "failed", {"error": "build"})
        with self.assertRaises(Invalid):
            self.store.finish(job_id, lease, "failed", {"error": "different"})


class HTTPTests(unittest.TestCase):
    def test_compact_million_seed_request_survives_submit_and_claim(self):
        from macqueue.plans import benchmark
        spec = benchmark('seedfinder', 'a'*40, 'b'*40, query={}, seed_range={'start': 0, 'count': 1048576})
        self.submit.request('POST', '/v1/jobs', spec, key='large-range')
        claim = self.worker.request('POST', '/v1/worker/claim', {'worker': 'mac', 'projects': ['seedfinder']})
        self.assertEqual(spec, claim['job']['spec'])

    def test_explicit_million_seed_job_survives_large_http_responses(self):
        from macqueue.plans import benchmark
        spec = benchmark('seedfinder', 'a'*40, 'b'*40, query={}, seeds=[2**64-1] * 1048576)
        submitted = self.submit.request('POST', '/v1/jobs', spec, key='large-explicit')
        self.assertEqual(spec, submitted['spec'])
        claimed = self.worker.request('POST', '/v1/worker/claim', {'worker': 'mac', 'projects': ['seedfinder']})
        self.assertEqual(spec, claimed['job']['spec'])
        read = self.submit.request('GET', '/v1/jobs/' + submitted['id'])
        self.assertEqual(spec, read['spec'])


    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.server = Server(("127.0.0.1", 0), self.store, "s" * 48, "w" * 48, max_artifact=1024)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.submit = Client(self.url, "s" * 48)
        self.worker = Client(self.url, "w" * 48)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_auth_scopes(self):
        for client, method, endpoint in ((self.worker, "GET", "/v1/jobs"),
                                         (self.submit, "POST", "/v1/worker/claim"),
                                         (Client(self.url, "bad"), "GET", "/v1/jobs")):
            with self.subTest(endpoint=endpoint), self.assertRaises(RemoteError) as caught:
                client.request(method, endpoint, {})
            self.assertEqual(401, caught.exception.status)

    def test_submit_claim_artifact_complete_download(self):
        submitted = self.submit.request("POST", "/v1/jobs", job(), key="one")
        claim = self.worker.request("POST", "/v1/worker/claim", {"worker": "mac", "projects": ["seedfinder"]})
        job_id, lease = submitted["id"], claim["lease"]
        archive = Path(self.temp.name) / "test.tar.gz"
        archive.write_bytes(b"opaque artifact bytes")
        receipt = self.worker.upload(job_id, lease, archive)
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), receipt["sha256"])
        completed = self.worker.request("POST", f"/v1/worker/jobs/{job_id}/finish", {"status": "succeeded", "result": {}}, lease=lease)
        self.assertEqual("succeeded", completed["status"])
        destination = Path(self.temp.name) / "download.tar.gz"
        self.submit.download(job_id, destination)
        self.assertEqual(archive.read_bytes(), destination.read_bytes())
        with self.assertRaises(FileExistsError):
            self.submit.download(job_id, destination)
        self.assertEqual(archive.read_bytes(), destination.read_bytes())

    def test_upload_limit_and_bad_schema(self):
        invalid = job()
        invalid["steps"][0]["command"]["cwd"] = "../../outside"
        with self.assertRaises(RemoteError) as caught:
            self.submit.request("POST", "/v1/jobs", invalid, key="bad")
        self.assertEqual(400, caught.exception.status)
        submitted = self.submit.request("POST", "/v1/jobs", job(), key="one")
        claim = self.worker.request("POST", "/v1/worker/claim", {"worker": "mac", "projects": ["seedfinder"]})
        archive = Path(self.temp.name) / "big"
        archive.write_bytes(b"x" * 1025)
        with self.assertRaises(RemoteError):
            self.worker.upload(submitted["id"], claim["lease"], archive)
        self.assertFalse(self.store.get(submitted["id"])["has_artifact"])

    def test_remote_http_requires_opt_in(self):
        with self.assertRaises(Invalid):
            Client("http://100.64.0.1:8787", "token")
        with self.assertRaises(Invalid):
            Client("https://username:password@example.com", "token")
