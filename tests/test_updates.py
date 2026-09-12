import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('update', Path(__file__).resolve().parents[1] / 'deploy/update.py')
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.updater = update.Update.__new__(update.Update)
        self.updater.mac = False
        self.updater.base = self.updater.app = self.root / 'app'
        self.updater.app.mkdir()
        self.updater.installation = {'installation_id': 'test-install', 'gid': 0}
        self.updater.stop = lambda: None
        self.updater.start = lambda paused: None
        self.addCleanup(patch.stopall)
        patch.object(update.os, 'chown').start()

    def install(self):
        target = self.updater.app / 'agent.py'
        target.write_bytes(b'x = 1\n')
        target.chmod(0o640)
        stage = self.root / 'stage'
        (stage / 'files').mkdir(parents=True)
        (stage / 'files/agent.py').write_bytes(b'x = 2\n')
        (stage / 'plan.json').write_text(json.dumps({'platform': 'debian', 'files': {'agent.py': {
            'before': update.current(target), 'after': update.current(stage / 'files/agent.py')}}}))
        self.updater.apply(stage)
        return target, next((self.updater.base / 'updates').glob('*/receipt.json'))

    def test_apply_and_rollback_restore_bytes_and_permissions(self):
        target, receipt = self.install()
        self.assertEqual(b'x = 2\n', target.read_bytes())
        self.updater.rollback(receipt)
        self.assertEqual(b'x = 1\n', target.read_bytes())
        self.assertEqual(0o640, target.stat().st_mode & 0o777)
        self.assertEqual('rolled-back', json.loads(receipt.read_text())['status'])

    def test_conflicting_change_is_preserved(self):
        target, receipt = self.install()
        target.write_bytes(b'x = 3\n')
        with self.assertRaisesRegex(RuntimeError, 'file changed since update'):
            self.updater.rollback(receipt)
        self.assertEqual(b'x = 3\n', target.read_bytes())

    def test_interrupted_apply_can_restore_mixed_state(self):
        target, receipt = self.install()
        value = json.loads(receipt.read_text())
        value['status'] = 'prepared'
        receipt.write_text(json.dumps(value))
        target.write_bytes(b'x = 1\n')
        self.updater.rollback(receipt)
        self.assertEqual(b'x = 1\n', target.read_bytes())
