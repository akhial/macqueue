#!/usr/bin/env python3
"""Run as the Mac operator after the root update; publish readiness without sharing worker credentials."""
import argparse
import json
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, required=True)
    args = parser.parse_args()
    report = args.stage / 'diagnostics/report.json'
    value = json.loads(report.read_text())
    assert value['enable'] == ['source-provision'] and value['sandbox'] is True, 'source provisioning probe did not pass'
    subprocess.run(['/usr/bin/ssh', '-o', 'BatchMode=yes', 'adel@devbox.komodo-spectrum.ts.net',
        'python3 /home/adel/code/macqueue/deploy/debian/confirm_selfserve.py --state /home/adel/code/macqueue/.state'],
        input=report.read_text(), text=True, check=True)


if __name__ == '__main__':
    main()
