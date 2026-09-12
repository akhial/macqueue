import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("macos_install", Path(__file__).resolve().parents[1] / "deploy/macos/install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class MacOSInstallerTests(unittest.TestCase):
    def test_service_runs_as_nonlogin_account_in_system_domain(self):
        plist = installer.daemon({"python": "/trusted/python3"})
        self.assertEqual("_macqueue", plist["UserName"])
        self.assertEqual("_macqueue", plist["GroupName"])
        self.assertFalse(plist["SessionCreate"])
        self.assertNotIn("LimitLoadToSessionType", plist)
        self.assertNotIn("Sockets", plist)
        self.assertIn("-I", plist["ProgramArguments"])
        self.assertTrue(plist["RunAtLoad"])

    def test_rollback_refuses_a_different_installation_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "installation.json").write_text(json.dumps({"installation_id": "somebody-else"}))
            value = {"base": str(root), "plist": str(installer.PLIST), "installation_id": "ours"}
            with patch.object(installer, "BASE", root), patch.object(installer.os, "geteuid", return_value=0), \
                 patch.object(installer, "command") as commands, patch.object(installer, "ds") as directory_service:
                with self.assertRaisesRegex(RuntimeError, "installation ID mismatch"):
                    installer.rollback(value, purge=True)
                commands.assert_not_called()
                directory_service.assert_not_called()
            self.assertTrue((root / "installation.json").exists())

    def test_recovery_refuses_completed_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "installation.json").write_text(json.dumps({"installation_id": "ours", "status": "installed-paused"}))
            with patch.object(installer, "BASE", root), patch.object(installer.os, "geteuid", return_value=0), \
                 patch.object(installer, "rollback") as rollback:
                with self.assertRaisesRegex(RuntimeError, "incomplete installation"):
                    installer.recover({"installation_id": "ours"})
                rollback.assert_not_called()

    def test_recovery_refuses_active_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "state").mkdir()
            (root / "state/ACTIVE.json").write_text("{}")
            (root / "installation.json").write_text(json.dumps({"installation_id": "ours", "status": "starting"}))
            with patch.object(installer, "BASE", root), patch.object(installer.os, "geteuid", return_value=0), \
                 patch.object(installer, "rollback") as rollback:
                with self.assertRaisesRegex(RuntimeError, "active job"):
                    installer.recover({"installation_id": "ours"})
                rollback.assert_not_called()

    def test_root_operations_refuse_nonroot_execution(self):
        with patch.object(installer.os, "geteuid", return_value=501):
            for action in (installer.apply, installer.rollback, installer.recover):
                with self.subTest(action=action.__name__), self.assertRaisesRegex(RuntimeError, "administrator"):
                    action({})

