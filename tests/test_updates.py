import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

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

    def test_new_debian_module_is_readable_by_the_unprivileged_agent(self):
        (self.updater.app / 'macqueue').mkdir()
        stage = self.root / 'new-module-stage'
        (stage / 'files/macqueue').mkdir(parents=True)
        source = stage / 'files/macqueue/sources.py'
        source.write_text('pass\n')
        (stage / 'plan.json').write_text(json.dumps({'platform': 'debian', 'files': {
            'macqueue/sources.py': {'before': None, 'after': update.current(source)}}}))
        self.updater.apply(stage)
        target = self.updater.app / 'macqueue/sources.py'
        self.assertEqual(0o644, target.stat().st_mode & 0o777)
        receipt = next((self.updater.base / 'updates').glob('*/receipt.json'))
        self.updater.rollback(receipt)
        self.assertFalse(target.exists())

    def test_interrupted_apply_can_restore_mixed_state(self):
        target, receipt = self.install()
        value = json.loads(receipt.read_text())
        value['status'] = 'prepared'
        receipt.write_text(json.dumps(value))
        target.write_bytes(b'x = 1\n')
        self.updater.rollback(receipt)
        self.assertEqual(b'x = 1\n', target.read_bytes())

    def test_mac_source_update_preserves_existing_capabilities_and_rolls_back_new_files(self):
        self.updater.mac = True
        self.updater.app = self.updater.base / 'app'
        self.updater.app.mkdir()
        self.updater.installation.update(uid=450, gid=450, python='/test/python')
        state = self.updater.base / 'state'
        state.mkdir()
        self.updater.paused = state / 'PAUSED'
        config = self.updater.base / 'config/worker.json'
        config.parent.mkdir()
        previous = {'capabilities': ['cargo', 'benchmark', 'inspect', 'pgo', 'profiling-build'], 'token_file': '/private/worker.token'}
        config.write_text(json.dumps(previous))
        before = config.read_bytes()
        report = state / 'source-probe/report.json'
        report.parent.mkdir()
        report.write_text(json.dumps({'sandbox': True, 'enable': ['source-provision']}))
        stage = self.root / 'source-stage'
        (stage / 'files').mkdir(parents=True)
        (stage / 'files/source-probe.py').write_text('pass\n')
        (stage / 'source-probe.tar.gz').write_bytes(b'probe fixture')
        plan = {'platform': 'macos', 'files': {'source-probe.py': {'before': None, 'after': update.current(stage / 'files/source-probe.py')}},
                'probe': {'kind': 'source-provision', 'commit': 'a'*40, 'sha256': update.current(stage / 'source-probe.tar.gz')}}
        (stage / 'plan.json').write_text(json.dumps(plan))
        attrs = {'IsHidden': 'IsHidden: 1', 'UserShell': 'UserShell: /usr/bin/false',
                 'AuthenticationAuthority': 'DisabledUser', 'dsAttrTypeNative:accountPolicyData': 'FALSEPREDICATE'}
        with patch.object(update.subprocess, 'run', side_effect=lambda argv, **kw: SimpleNamespace(stdout=attrs[argv[-1]])), \
             patch.object(update.subprocess, 'Popen', return_value=SimpleNamespace(stdout=io.StringIO('ROLLOUT_REPORT=' + str(report) + '\n'), wait=lambda: 0)):
            self.updater.apply(stage)
        current = json.loads(config.read_text())
        self.assertEqual([*previous['capabilities'], 'source-provision'], current['capabilities'])
        self.assertEqual(previous['token_file'], current['token_file'])
        receipt = next((self.updater.base / 'updates').glob('*/receipt.json'))
        self.updater.rollback(receipt)
        self.assertEqual(before, config.read_bytes())
        self.assertFalse((self.updater.app / 'source-probe.py').exists())
