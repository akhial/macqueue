#!/usr/bin/env python3
"""Pinned operator probe for unprivileged self-service source provisioning."""
import argparse
import json
import shutil
import sys
import threading
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from macqueue.common import Invalid, digest, json_bytes, require
from macqueue.doctor import doctor
from macqueue.plans import build_steps, command
from macqueue.policy import Policy
from macqueue.runner import Runner, pack_artifacts
from macqueue.sources import lookup
from macqueue.worker import load_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--commit', required=True)
    args = parser.parse_args()
    config = load_config('/Library/Macqueue/config/worker.json')
    config['capabilities'] = list(dict.fromkeys([*config['capabilities'], 'source-provision']))
    state = Path(config['state_dir'])
    require((state / 'PAUSED').exists() and not (state / 'ACTIVE.json').exists(), 'source probe requires paused idle execution')
    require(digest(args.input) == args.sha256, 'source probe package changed')
    root = state / ('source-probe-' + uuid.uuid4().hex)
    root.mkdir(mode=0o700)
    policy = Policy(config)
    report = {'sandbox': doctor(config)['ok'], 'root': str(root), 'enable': [], 'checks': {}}
    spec = {'version': 1, 'project': 'seedfinder', 'sources': {'candidate': args.commit},
            'steps': [{'id': 'provision', 'op': 'provision', 'sha256': args.sha256}], 'timeout_seconds': 1200}
    def download(sha, destination, check):
        require(sha == args.sha256, 'unexpected package')
        check()
        shutil.copyfile(args.input, destination)
    print('Checking unprivileged source import and offline dependency resolution ...', flush=True)
    provision = Runner(policy, root / 'provision', spec, threading.Event(), download_input=download)
    result = provision.run()
    snapshot = lookup(policy, 'seedfinder', args.commit)
    require(snapshot is not None, 'source package was not published')
    print('Building both benchmark executables from the imported package, offline ...', flush=True)
    build = {'version': 1, 'project': 'seedfinder', 'sources': {'candidate': args.commit},
             'steps': build_steps('candidate'), 'timeout_seconds': 1200}
    build['steps'].append({'id': 'smoke', 'op': 'exec', 'command': command([
        '${JOB}/artifacts/frozen/candidate/seed-seeker', '--benchmark', '3', '--workers', '1'], env={})})
    runner = Runner(policy, root / 'build', build, threading.Event())
    runner.run()
    metadata = json.loads((runner.root / 'artifacts/environment-after.json').read_text())
    require(metadata['source_packages'] == {'candidate': args.sha256}, 'build did not use the uploaded package')
    try:
        runner.internal(['/bin/chmod', 'u+w', str(snapshot / 'source.bundle')], label='deny-package-mutation')
    except Invalid:
        pass
    else:
        raise RuntimeError('job could make its source snapshot writable')
    pack_artifacts(provision.root, root / 'provision.tar.gz')
    pack_artifacts(runner.root, root / 'build.tar.gz')
    report['enable'] = ['source-provision']
    report['checks']['source-provision'] = {'ok': True, 'commit': args.commit, 'sha256': args.sha256,
        'offline_build': True, 'snapshot_write_denied': True, 'result': result['source_provisioning']}
    (root / 'report.json').write_bytes(json_bytes(report))
    print(json.dumps(report['checks'], indent=2), flush=True)
    print('ROLLOUT_REPORT=' + str(root / 'report.json'), flush=True)


if __name__ == '__main__':
    main()
