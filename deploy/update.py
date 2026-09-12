#!/usr/bin/env python3
"""Apply a reviewed file manifest; keep an installation-bound rollback receipt."""
import argparse
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import time
import uuid
from pathlib import Path


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def current(path):
    require(not path.is_symlink(), 'symlink at ' + str(path))
    return digest(path.read_bytes()) if path.exists() else None


def save(path, value):
    data = json.dumps(value, indent=2).encode() + b'\n'
    replace(path, data, 0, 0, 0o600)


def replace(path, data, uid, gid, mode):
    temp = path.with_name(path.name + '.update-' + uuid.uuid4().hex)
    with temp.open('xb') as stream:
        stream.write(data)
    os.chown(temp, uid, gid)
    temp.chmod(mode)
    temp.replace(path)


class Update:
    def __init__(self):
        self.mac = platform.system() == 'Darwin'
        require(self.mac or platform.system() == 'Linux', 'unsupported platform')
        self.base = Path('/Library/Macqueue' if self.mac else '/opt/macqueue')
        self.app = self.base / 'app' if self.mac else self.base
        self.installation = json.loads((self.base / 'installation.json').read_text())
        self.paused = self.base / 'state/PAUSED'
        self.lock = None

    def target(self, name):
        require(bool(re.fullmatch(r'macqueue/[a-z_]+\.py', name)) or name in ('agent.py', 'rollout-probe.py', 'source-probe.py', 'config/worker.json'), 'invalid update target')
        if name == 'config/worker.json':
            require(self.mac, 'worker config is Mac-only')
            return self.base / name
        return self.app / name

    def stop(self):
        if self.mac:
            self.paused.touch(mode=0o600)
            # Allow an in-flight claim to observe PAUSED, then drain any current job.
            time.sleep(3)
            for _ in range(60):
                if not (self.base / 'state/ACTIVE.json').exists():
                    break
                print('Waiting for the active job; new claims are paused ...', flush=True)
                time.sleep(5)
            require(not (self.base / 'state/ACTIVE.json').exists(), 'active job still running; retry later, worker remains paused')
            domain = 'system/local.macqueue.worker'
            if subprocess.run(['/bin/launchctl', 'print', domain], capture_output=True).returncode == 0:
                subprocess.run(['/bin/launchctl', 'bootout', domain], check=True)
            self.lock = (self.base / 'state/worker.lock').open('a+')
            for _ in range(60):
                try:
                    fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(0.5)
            else:
                raise RuntimeError('worker did not stop; installation stays paused')
            require(not (self.base / 'state/ACTIVE.json').exists(), 'unconfirmed job cleanup; refusing update')
        else:
            subprocess.run(['/usr/bin/systemctl', 'stop', 'macqueue.service'], check=True)

    def start(self, paused):
        if self.mac:
            if self.lock:
                self.lock.close()
                self.lock = None
            log = self.base / 'logs/worker.stderr.log'
            offset = log.stat().st_size
            subprocess.run(['/bin/launchctl', 'bootstrap', 'system', '/Library/LaunchDaemons/local.macqueue.worker.plist'], check=True)
            for _ in range(120):
                with log.open() as stream:
                    stream.seek(offset)
                    if 'ready; paused=True' in stream.read():
                        break
                time.sleep(0.5)
            else:
                raise RuntimeError('updated worker did not become ready; remains paused')
            require(subprocess.run(['/bin/launchctl', 'print', 'gui/' + str(self.installation['uid'])], capture_output=True).returncode != 0, 'unexpected GUI session')
            if not paused:
                self.paused.unlink()
        else:
            subprocess.run(['/usr/bin/systemctl', 'start', 'macqueue.service'], check=True)
            subprocess.run(['/usr/bin/systemctl', 'is-active', '--quiet', 'macqueue.service'], check=True)

    def check_restore(self, path, receipt):
        # Preflight every file before changing any file. Handles interrupted apply too.
        for name, item in receipt['files'].items():
            require(current(self.target(name)) in (item['before'], item['after']), 'file changed since update: ' + name)
            if item['before'] is not None:
                require(current(path.parent / 'before' / name) == item['before'], 'backup changed: ' + name)

    def restore(self, path, receipt):
        self.check_restore(path, receipt)
        for name, item in receipt['files'].items():
            if item['before'] is None:
                self.target(name).unlink(missing_ok=True)
            else:
                replace(self.target(name), (path.parent / 'before' / name).read_bytes(), item['uid'], item['gid'], item['mode'])

    def apply(self, stage):
        plan = json.loads((stage / 'plan.json').read_text())
        require(plan['platform'] == ('macos' if self.mac else 'debian'), 'wrong deployment platform')
        require(plan['files'] and 'config/worker.json' not in plan['files'], 'config is changed only by successful local probes')
        payloads = {}
        for name, hashes in plan['files'].items():
            target = self.target(name)
            require(current(target) == hashes['before'], 'installed file differs from reviewed version: ' + name)
            source = stage / 'files' / name
            require(current(source) == hashes['after'], 'staged file hash changed: ' + name)
            payloads[name] = source.read_bytes()
            compile(payloads[name], name, 'exec')
        if self.mac:
            if plan['probe'].get('kind') == 'source-provision':
                require(current(stage / 'source-probe.tar.gz') == plan['probe']['sha256'], 'source probe package changed')
            for attribute, expected in (('IsHidden', 'IsHidden: 1'), ('UserShell', 'UserShell: /usr/bin/false'),
                                        ('AuthenticationAuthority', 'DisabledUser'), ('dsAttrTypeNative:accountPolicyData', 'FALSEPREDICATE')):
                result = subprocess.run(['/usr/bin/dscl', '.', '-read', '/Users/_macqueue', attribute],
                                        text=True, capture_output=True, check=True)
                require(expected in result.stdout, 'service login policy differs: ' + attribute)
        backup = self.base / 'updates' / uuid.uuid4().hex
        backup.mkdir(parents=True, mode=0o700)
        backup.parent.chmod(0o700)
        path = backup / 'receipt.json'
        receipt = {'installation_id': self.installation['installation_id'], 'status': 'prepared',
                   'was_paused': self.paused.exists() if self.mac else False, 'files': {}}
        names = [*payloads, *(['config/worker.json'] if self.mac else [])]
        for name in names:
            target = self.target(name)
            before = current(target)
            info = target.stat() if before is not None else None
            receipt['files'][name] = {'before': before, 'after': digest(payloads[name]) if name in payloads else before,
                                     'uid': info.st_uid if info else 0,
                                     'gid': info.st_gid if info else (self.installation['gid'] if self.mac else 0),
                                     'mode': stat.S_IMODE(info.st_mode) if info else (0o640 if self.mac else 0o644)}
            if before is not None:
                old = backup / 'before' / name
                old.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                old.write_bytes(target.read_bytes())
        shutil.copyfile(Path(__file__), backup / 'update.py')
        save(path, receipt)
        print('Rollback: sudo /usr/bin/python3 ' + str(backup / 'update.py') + ' rollback --receipt ' + str(path), flush=True)
        self.stop()
        try:
            for name, data in payloads.items():
                item = receipt['files'][name]
                replace(self.target(name), data, item['uid'], item['gid'], item['mode'])
            if self.mac:
                probe = plan['probe']
                source_probe = probe.get('kind') == 'source-provision'
                input_path = None
                if source_probe:
                    input_path = self.base / 'state' / ('probe-input-' + uuid.uuid4().hex + '.tar.gz')
                    shutil.copyfile(stage / 'source-probe.tar.gz', input_path)
                    os.chown(input_path, 0, self.installation['gid'])
                    input_path.chmod(0o640)
                    require(current(input_path) == probe['sha256'], 'copied source package changed')
                    probe_args = [str(self.app / 'source-probe.py'), '--input', str(input_path),
                                  '--sha256', probe['sha256'], '--commit', probe['commit']]
                else:
                    probe_args = [str(self.app / 'rollout-probe.py'), '--commit', probe['commit'], '--profile-sha256', probe['profile_sha256']]
                argv = ['/usr/bin/sudo', '-u', '_macqueue', '--', '/usr/bin/env', '-i',
                        'HOME=/Library/Macqueue/home', 'PATH=/usr/bin:/bin:/usr/sbin:/sbin',
                        self.installation['python'], '-I', '-B', *probe_args]
                child = subprocess.Popen(argv, text=True, stdout=subprocess.PIPE)
                report_path = None
                for line in child.stdout:
                    print(line, end='', flush=True)
                    if line.startswith('ROLLOUT_REPORT='):
                        report_path = Path(line.strip().split('=', 1)[1])
                code = child.wait()
                if input_path:
                    input_path.unlink()
                require(code == 0 and report_path is not None, 'local probes did not complete; use rollback receipt')
                require(report_path.resolve().is_relative_to(self.base / 'state'), 'unexpected probe report path')
                report = json.loads(report_path.read_text())
                expected = {'source-provision'} if source_probe else {'pgo', 'profiling-build', 'sample', 'xctrace'}
                require(report['sandbox'] is True and set(report['enable']) <= expected, 'invalid probe report')
                config_path = self.target('config/worker.json')
                config = json.loads(config_path.read_text())
                if not source_probe:
                    require(set(config.get('capabilities', [])) <= {'cargo', 'benchmark', 'inspect'}, 'optional capabilities were already configured; review this rollout')
                config['capabilities'] = list(dict.fromkeys([*config['capabilities'], *report['enable']]))
                config['max_output_bytes'] = 256 * 1024 * 1024
                data = json.dumps(config, indent=2).encode() + b'\n'
                receipt['files']['config/worker.json']['after'] = digest(data)
                receipt['probe_report'] = str(report_path)
                save(path, receipt)
                item = receipt['files']['config/worker.json']
                replace(config_path, data, item['uid'], item['gid'], item['mode'])
                save(backup / 'probe-report.json', report)
                # Share only sandboxed job diagnostics, never control credentials.
                diagnostics = stage / 'diagnostics'
                diagnostics.mkdir(mode=0o700)
                owner = stage.stat()
                os.chown(diagnostics, owner.st_uid, owner.st_gid)
                for source in [report_path, *sorted(report_path.parent.glob('*.tar.gz'))]:
                    require(not source.is_symlink() and source.is_file(), 'invalid diagnostic artifact')
                    dest = diagnostics / source.name
                    shutil.copyfile(source, dest)
                    os.chown(dest, owner.st_uid, owner.st_gid)
                    dest.chmod(0o600)
            receipt['status'] = 'installed'
            save(path, receipt)
            self.start(receipt['was_paused'])
        except BaseException:
            print('Update incomplete. Execution stays stopped or paused; use the printed rollback command.', flush=True)
            raise
        print(json.dumps({'ok': True, 'receipt': str(path), 'probe_report': receipt.get('probe_report'),
                          'paused': receipt['was_paused']}, indent=2))

    def rollback(self, path):
        path = path.resolve()
        require(path.parent.parent == self.base / 'updates' and path.name == 'receipt.json', 'unexpected receipt path')
        receipt = json.loads(path.read_text())
        require(receipt['installation_id'] == self.installation['installation_id'], 'installation changed')
        require(receipt['status'] != 'rolled-back', 'already rolled back')
        self.check_restore(path, receipt)
        self.stop()
        self.restore(path, receipt)
        self.start(receipt['was_paused'])
        receipt['status'] = 'rolled-back'
        save(path, receipt)
        print(json.dumps({'ok': True, 'rolled_back': True, 'retained_backup': str(path.parent)}))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('apply').add_argument('--stage', type=Path, required=True)
    sub.add_parser('rollback').add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    require(os.geteuid() == 0, 'requires administrator authentication')
    updater = Update()
    updater.apply(args.stage.resolve()) if args.action == 'apply' else updater.rollback(args.receipt)


if __name__ == '__main__':
    main()
