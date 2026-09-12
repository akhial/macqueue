#!/usr/bin/env python3
"""Hash-pinned, operator-only probes. Run as the service account with its daemon stopped."""
import argparse
import json
import sys
import threading
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from macqueue.common import digest, json_bytes, require
from macqueue.doctor import doctor
from macqueue.plans import benchmark, command, pgo
from macqueue.policy import Policy
from macqueue.process import CleanupError
from macqueue.runner import Runner, pack_artifacts
from macqueue.worker import load_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--profile-sha256', required=True)
    args = parser.parse_args()
    config = load_config('/Library/Macqueue/config/worker.json')
    state = Path(config['state_dir'])
    require((state / 'PAUSED').exists() and not (state / 'ACTIVE.json').exists(), 'probes require a paused idle worker')
    config['capabilities'] = ['cargo', 'benchmark', 'inspect', 'pgo', 'profiling-build', 'sample', 'xctrace']
    config['max_output_bytes'] = 256 * 1024 * 1024
    root = state / ('rollout-' + uuid.uuid4().hex)
    root.mkdir(mode=0o700)
    report = {'sandbox': doctor(config)['ok'], 'root': str(root), 'checks': {}, 'enable': []}
    query = {'max_depth': 1, 'requirements': [{'kind': 'ring'}], 'auto_apply_trinket': False}

    def run(name, function):
        print('Checking ' + name + ' ...', flush=True)
        try:
            detail = function()
            report['checks'][name] = {'ok': True, **(detail or {})}
        except CleanupError:
            raise  # Do not resume production after unconfirmed process cleanup.
        except Exception as exc:
            report['checks'][name] = {'ok': False, 'error': str(exc)}
            job = root / ('profiling' if name == 'profiling-build' else name)
            if (job / 'artifacts').is_dir():
                pack_artifacts(job, root / (name + '-failed.tar.gz'))
        (root / 'report.json').write_bytes(json_bytes(report))
        print(json.dumps({name: report['checks'][name]}), flush=True)
        return report['checks'][name]['ok']

    def pgo_probe():
        spec = pgo('seedfinder', args.commit, query=query, seed_range={'start': 1000000, 'count': 4096},
                   seed_count=10, warmups=0, samples=1, baseline_profile_sha256=args.profile_sha256)
        spec['timeout_seconds'] = 1200
        runner = Runner(Policy(config), root / 'pgo', spec, threading.Event())
        runner.run()
        records = [json.loads(line) for line in (runner.root / 'artifacts/results/match_benchmark.jsonl').read_text().splitlines()]
        samples = [row for row in records if row['kind'] == 'sample']
        require(samples and all(row['response']['tested'] == 4096 for row in samples), 'matching tested counts differ')
        require({row['workers'] for row in samples} == {1, *runner.cores.values()}, 'core counts missing')
        reference = sorted(json.dumps(match, sort_keys=True) for match in samples[0]['response']['matches'])
        require(all(sorted(json.dumps(match, sort_keys=True) for match in row['response']['matches']) == reference for row in samples), 'PGO matching results differ')
        profile = runner.root / 'artifacts/pgo/merged.profdata'
        require(profile.stat().st_size > 0, 'merged profile is empty')
        pack_artifacts(runner.root, root / 'pgo.tar.gz')
        return {'merged_sha256': digest(profile), 'workers': sorted({row['workers'] for row in samples}),
                'matching_seeds': 4096, 'note': 'pipeline and correctness smoke; not representative training or speedup evidence'}

    if run('pgo', pgo_probe):
        report['enable'].append('pgo')

    spec = benchmark('seedfinder', args.commit, args.commit, query=query, seeds=[1], profiling=True, warmups=0, samples=1)
    spec['sources'] = {'candidate': args.commit}
    spec['steps'] = spec['steps'][2:4]
    spec['timeout_seconds'] = 1200
    profile_runner = Runner(Policy(config), root / 'profiling', spec, threading.Event())

    def symbols_probe():
        profile_runner.run()
        frozen = profile_runner.root / 'artifacts/frozen/candidate'
        require(all((frozen / (name + '.dSYM')).is_dir() for name in ('seed-seeker', 'match_benchmark', 'equivalence')), 'missing standalone symbols')
        for tool, flags in (('llvm-objdump', ['--section-headers']), ('nm', ['-g']), ('size', [])):
            profile_runner.execute(command([tool, *flags, '${JOB}/artifacts/frozen/candidate/seed-seeker'], env={}, log='probe-' + tool))

    if run('profiling-build', symbols_probe):
        report['enable'].append('profiling-build')
        for tool in ('sample', 'xctrace'):
            def probe(tool=tool):
                cmd = command(['${JOB}/artifacts/frozen/candidate/seed-seeker', '--benchmark', '10000000', '--workers', '1'],
                              env={}, timeout=45, log='probe-' + tool)
                output = 'artifacts/profile-' + tool + ('.trace' if tool == 'xctrace' else '.txt')
                if tool == 'xctrace':
                    profile_runner.execute(command(['xcrun', 'xctrace', 'list', 'templates'], env={}, timeout=30, log='probe-templates'))
                profile_runner.profile({'tool': tool, 'command': cmd, 'duration_seconds': 3, 'output': output})
                require(profile_runner.path(output, exists=True).stat().st_size > 0, 'empty profiler output')
                if tool == 'sample':
                    require('Call graph:' in profile_runner.path(output).read_text(errors='replace'), 'sample did not produce a call graph')
                if tool == 'xctrace':
                    profile_runner.execute(command(['xcrun', 'xctrace', 'export', '--input', '${JOB}/' + output,
                        '--output', '${JOB}/artifacts/toc.xml', '--toc'], env={}, timeout=30, log='probe-export'),
                        writable=[profile_runner.path('artifacts/toc.xml')])
                    require('<trace-toc' in profile_runner.path('artifacts/toc.xml').read_text(), 'missing exported trace TOC')
                return {'output': output}
            if run(tool, probe):
                report['enable'].append(tool)
        pack_artifacts(profile_runner.root, root / 'profiling.tar.gz')
    (root / 'report.json').write_bytes(json_bytes(report))
    print('ROLLOUT_REPORT=' + str(root / 'report.json'), flush=True)


if __name__ == '__main__':
    main()
