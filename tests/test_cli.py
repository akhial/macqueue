import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from macqueue.cli import main


class PlanTests(unittest.TestCase):
    def test_matching_inputs_are_reviewable_in_generated_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            query, seeds = Path(temp) / 'query.json', Path(temp) / 'seeds.json'
            query.write_text('{}')
            seeds.write_text('[123,456]')
            base = ['plan', 'benchmark', '--project', 'seedfinder', '--baseline', 'a'*40,
                    '--candidate', 'b'*40, '--query', str(query)]
            for args, expected in ((['--seed-range', '1000000:65536'], {'seed_range': {'start': 1000000, 'count': 65536}}),
                                   (['--seeds-file', str(seeds)], {'seeds': [123,456]})):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    main(base + args)
                self.assertEqual([expected], json.loads(output.getvalue())['steps'][-1]['requests'])
