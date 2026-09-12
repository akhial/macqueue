import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from macqueue.common import Invalid
from macqueue.process import Process, Stopped


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cancel = threading.Event()

    def process(self, code, timeout=5, limit=1024**2, **kwargs):
        return Process([sys.executable, "-u", "-c", code], self.root, {"PATH": "/usr/bin:/bin"},
                       self.root / "stdout", self.root / "stderr", sandbox=None, cancel=self.cancel,
                       deadline=time.monotonic() + timeout, max_output=limit, **kwargs)

    def test_large_matching_request_and_response(self):
        code = "import sys,json; print('{}');\nfor line in sys.stdin:\n seeds=json.loads(line)['seeds']; print(json.dumps({'tested':len(seeds),'first':seeds[0],'last':seeds[-1],'matches':'x'*1100000}))"
        seeds = list(range(2**64 - 8192, 2**64))
        with self.process(code, limit=4*1024**2) as process:
            process.keep_lines = True
            process.record(2)
            response = process.request({'seeds': seeds}, 3)['response']
            self.assertEqual((8192, seeds[0], seeds[-1]), (response['tested'], response['first'], response['last']))
            self.assertEqual(1100000, len(response['matches']))
            process.finish_session()

    def test_record_limit_applies_to_each_complete_record(self):
        with self.process("print('{\"a\":1}\\n{\"b\":2}')", max_record_bytes=7) as process:
            process.keep_lines = True
            self.assertEqual({'a': 1}, process.record(2))
            self.assertEqual({'b': 2}, process.record(2))
            process.finish_session()

    def test_oversized_record_is_rejected_below_total_output_limit(self):
        with self.process("print('{\"a\":12}')", max_record_bytes=7) as process:
            process.keep_lines = True
            with self.assertRaisesRegex(Invalid, 'record exceeds'):
                process.record(2)

    def check_trailing_output(self, trailing, message):
        code = "import sys; print('{}'); sys.stdin.readline(); print('{}'); sys.stdin.read(); " + trailing
        with self.process(code) as process:
            process.keep_lines = True
            process.record(2)
            process.request({'seeds': [1]}, 2)
            with self.assertRaisesRegex(Invalid, message):
                process.finish_session()

    def test_extra_complete_record_after_eof_is_rejected(self):
        self.check_trailing_output("print('{}')", 'unexpected benchmark output on shutdown')

    def test_unterminated_output_after_eof_is_rejected(self):
        self.check_trailing_output("sys.stdout.write('{')", 'incomplete benchmark output on shutdown')

    def test_stdin_and_both_output_streams(self):
        with self.process("import sys; print(sys.stdin.read()); print('error', file=sys.stderr)") as process:
            result = process.run("input\n")
        self.assertEqual(0, result["exit_code"])
        self.assertIn("input", (self.root / "stdout").read_text())
        self.assertIn("error", (self.root / "stderr").read_text())

    def test_persistent_jsonl_and_eof_flush(self):
        code = "import sys,json; print(json.dumps({'event':'ready'}));\nfor line in sys.stdin: print(json.dumps({'result':json.loads(line)}))\nprint('flushed',file=sys.stderr)"
        with self.process(code) as process:
            process.keep_lines = True
            self.assertEqual({"event": "ready"}, process.record(2))
            for seeds in ([1, 2], [3]):
                self.assertEqual({"seeds": seeds}, process.request({"seeds": seeds}, 2)["response"]["result"])
            process.finish_session()
        self.assertIn("flushed", (self.root / "stderr").read_text())

    def test_timeout_kills_descendants_after_parent_exit(self):
        code = "import subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); print(p.pid,flush=True)"
        with self.process(code, timeout=0.5) as process:
            with self.assertRaises(Stopped):
                process.run()
            group = process.proc.pid
        # The grandchild's inherited stdout kept the command open; cleanup targets its group.
        for _ in range(30):
            try:
                os.killpg(group, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("job process group survived cleanup")

    def test_cancel_and_output_limit(self):
        with self.process("import time; time.sleep(30)") as process:
            self.cancel.set()
            with self.assertRaises(Stopped):
                process.run()
        (self.root / "stdout").unlink()
        (self.root / "stderr").unlink()
        self.cancel.clear()
        with self.process("print('x'*10000)", limit=1024) as process:
            with self.assertRaises(Invalid):
                process.run()
