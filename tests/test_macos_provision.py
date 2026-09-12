import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "deploy/macos/provision.py"
spec = importlib.util.spec_from_file_location("macos_provision", SOURCE)
provision = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provision)


class ProvisionTests(unittest.TestCase):
    def test_endpoint_and_snapshot_reject_unintended_sources(self):
        provision.queue_url("http://100.102.112.115:8787")
        for url in ("http://100.attacker.example:8787", "http://100.1.2.3:8787", "http://100.64.0.1:8787/path",
                    "http://user@100.64.0.1:8787", "http://100.64.0.1:80"):
            with self.assertRaises((RuntimeError, ValueError)):
                provision.queue_url(url)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "normal").write_text("content")
            (root / "unexpected-link").symlink_to(root / "normal")
            with self.assertRaises(RuntimeError):
                provision.tree(root)

    def test_rollback_restores_previous_snapshot_and_preserves_new_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            backup = base / "provisioning" / ("a" * 32)
            backup.mkdir(parents=True)
            for name in ("state", "config", "mirrors", "cargo"):
                (base / name).mkdir()
            (base / "mirrors/seedfinder.git").mkdir()
            for name in ("mirror.git", "cargo"):
                (backup / name).mkdir()
                (backup / name / "old").write_text("original")
            (base / "cargo/new").write_text("downloaded during jobs")
            (base / "installation.json").write_text(json.dumps({"installation_id": "original", "uid": 450, "gid": 450}))
            for name in ("worker.json", "worker.token"):
                (backup / name).write_text("original " + name)
                (base / "config" / name).write_text("current " + name)
            receipt = {"installation_id": "original", "status": "active", "moved": ["mirror.git", "cargo"],
                       "config_sha256": provision.sha(base / "config/worker.json")}
            path = backup / "receipt.json"
            path.write_text(json.dumps(receipt))
            def restarted():
                self.assertTrue((base / "state/PAUSED").exists())
                self.assertEqual("original worker.token", (base / "config/worker.token").read_text())
            with patch.object(provision, "BASE", base), patch.object(provision.os, "geteuid", return_value=0), \
                    patch.object(provision.os, "chown"), patch.object(provision, "restart", side_effect=restarted):
                provision.rollback(path)
            self.assertEqual("original", (base / "cargo/old").read_text())
            self.assertEqual("downloaded during jobs", (backup / "removed-cargo/new").read_text())
            self.assertEqual("rolled-back", json.loads(path.read_text())["status"])
