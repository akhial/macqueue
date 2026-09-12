#!/usr/bin/env python3
"""Publish the Mac probe result and submit a confirmation through the agent's own CLI."""
import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    args = parser.parse_args()
    report = json.load(sys.stdin)
    assert report['sandbox'] is True and report['enable'] == ['source-provision'], 'source probe did not pass'
    check = report['checks']['source-provision']
    assert check['ok'] and check['offline_build'] and check['snapshot_write_denied'], 'incomplete source probe'
    state = args.state
    (state / 'selfserve-mac-report.json').write_text(json.dumps(report, indent=2) + '\n')
    status = {'server_updated': True, 'mac_updated': True, 'enabled': True,
              'verified_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'probe': check, 'instructions': 'Use macqueue provision --candidate FULL_SHA [--baseline FULL_SHA]; wait for its successful job result before submitting build/test jobs.'}
    log = state / 'selfserve-confirmation.log'
    print('Submitting a provisioning confirmation using the unprivileged agent CLI ...', flush=True)
    with log.open('w') as errors:
        result = subprocess.run(['macqueue', 'provision', '--candidate', check['commit'], '--no-wait'],
                                text=True, stdout=subprocess.PIPE, stderr=errors)
    if result.returncode == 0:
        job = json.loads(result.stdout)
        (state / 'selfserve-confirmation-job.json').write_text(json.dumps(job, indent=2) + '\n')
        status['confirmation_job'] = job['id']
        status['confirmation_status'] = job['status']
    else:
        status['confirmation_error'] = str(log)
    (state / 'selfserve-status.json').write_text(json.dumps(status, indent=2) + '\n')
    ready_path = state / 'worker-readiness.json'
    if ready_path.exists():
        ready = json.loads(ready_path.read_text())
        ready['capabilities'] = list(dict.fromkeys([*ready['capabilities'], 'source-provision']))
        ready['self_service_provisioning'] = True
        ready_path.write_text(json.dumps(ready, indent=2) + '\n')
    handoff = ('Self-service source provisioning is enabled and its Mac sandbox/offline-build probe passed. '
               'Use `macqueue provision --candidate FULL_SHA --baseline FULL_SHA`; it waits by default. '
               'After success, submit ordinary correctness/comparison jobs. No source administrator handoff is needed. '
               'See docs/self-service-provisioning.md and .state/selfserve-status.json. ')
    if status.get('confirmation_job'):
        handoff += 'An end-to-end confirmation is queued as ' + status['confirmation_job'] + '; use `macqueue wait ' + status['confirmation_job'] + '` to inspect its result. '
    (state / 'optimization-handoff.md').write_text(handoff + '\n')
    print(json.dumps(status, indent=2))
    if result.returncode:
        print('Worker enablement succeeded; inspect ' + str(log) + ' for the confirmation submission error.', file=sys.stderr)
        raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()
