import json
import tempfile
import time
import unittest
from pathlib import Path

from macqueue.common import Invalid
from macqueue.worker import Worker, prune_worker


class WorkerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        token = self.base / "worker.token"
        token.write_text("w" * 48)
        token.chmod(0o600)
        self.config = {"state_dir": str(self.base / "state"), "token_file": str(token),
                       "server_url": "http://127.0.0.1:1", "worker_id": "fixture", "cargo_home": str(self.base / "cargo"),
                       "rust_toolchain": str(self.base / "toolchain"), "projects": {"seedfinder": str(self.base / "source")}}

    def test_unclean_exit_blocks_new_jobs_without_contacting_server(self):
        worker = Worker(self.config)
        marker = Path(self.config["state_dir"]) / "ACTIVE.json"
        marker.write_text('{"job_id":"unfinished"}')
        with self.assertRaisesRegex(Invalid, "previous worker exited"):
            worker.run(once=True)
        self.assertTrue(marker.exists())
        self.assertTrue(worker.lock.closed)

    def test_prune_preserves_failed_artifact_delivery_and_unfinished_receipts(self):
        Worker(self.config)
        state = Path(self.config["state_dir"])
        for job_id, outcome in (("a" * 32, {"delivered": True, "result": {}}),
                                ("b" * 32, {"delivered": True, "result": {"artifact_error": "network"}}),
                                ("c" * 32, {"delivered": False, "result": {}})):
            root = state / "jobs" / job_id
            (root / "artifacts/frozen").mkdir(parents=True)
            (root / "artifacts/frozen/binary").write_text("binary")
            (root / "artifacts/frozen").chmod(0o555)
            receipt = state / (job_id + ".result.json")
            receipt.write_text(json.dumps(outcome))
            import os
            old = time.time() - 10 * 86400
            os.utime(receipt, (old, old))
        self.assertEqual(1, prune_worker(self.config, 7))
        self.assertFalse((state / "jobs" / ("a" * 32)).exists())
        self.assertTrue((state / "jobs" / ("b" * 32)).exists())
        self.assertTrue((state / "jobs" / ("c" * 32)).exists())

