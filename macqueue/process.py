"""Bounded subprocess IO, cancellation, deadlines, and process-group cleanup."""
import json
import os
import selectors
import signal
import subprocess
import time
from collections import deque

from .common import Invalid, json_bytes, require


class Stopped(RuntimeError):
    pass


class CleanupError(RuntimeError):
    pass


class Process:
    def __init__(self, argv, cwd, env, stdout, stderr, *, sandbox, cancel, deadline,
                 event=lambda _: None, max_output=32 * 1024 * 1024):
        self.cancel, self.deadline, self.event = cancel, deadline, event
        self.max_output = max_output
        self.total = 0
        self.selector = selectors.DefaultSelector()
        self.pending = bytearray()
        self.lines = deque()
        self.partial = bytearray()
        self.keep_lines = False
        self.closed = False
        self.files = {}
        self.proc = None
        try:
            for kind, path in (("stdout", stdout), ("stderr", stderr)):
                path.parent.mkdir(parents=True, exist_ok=True)
                self.files[kind] = path.open("xb")
            actual = ["/usr/bin/sandbox-exec", "-p", sandbox, *argv] if sandbox is not None else argv
            self.proc = subprocess.Popen(actual, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                                         close_fds=True)
            for kind in ("stdout", "stderr"):
                stream = getattr(self.proc, kind)
                os.set_blocking(stream.fileno(), False)
                self.selector.register(stream, selectors.EVENT_READ, kind)
            os.set_blocking(self.proc.stdin.fileno(), False)
            self.started = time.monotonic()
        except BaseException:
            self.close()
            raise

    def check(self, deadline=None):
        if self.cancel.is_set():
            raise Stopped("job cancelled or worker lease lost")
        if time.monotonic() >= min(self.deadline, deadline or self.deadline):
            raise Stopped("job or command timed out")

    def pump(self, deadline=None):
        self.check(deadline)
        for key, _ in self.selector.select(0.1):
            if key.data == "stdin":
                try:
                    count = os.write(key.fileobj.fileno(), self.pending)
                except BrokenPipeError:
                    raise Invalid("process closed stdin before consuming request") from None
                del self.pending[:count]
                if not self.pending:
                    self.selector.unregister(key.fileobj)
                continue
            data = os.read(key.fileobj.fileno(), 65536)
            if not data:
                self.selector.unregister(key.fileobj)
                continue
            self.total += len(data)
            require(self.total <= self.max_output, "process output limit exceeded")
            self.files[key.data].write(data)
            self.files[key.data].flush()
            self.event({"stream": key.data, "text": data[:4096].decode(errors="replace"), "truncated": len(data) > 4096})
            if self.keep_lines and key.data == "stdout":
                self.partial.extend(data)
                require(len(self.partial) <= 1024 * 1024, "JSON-lines record exceeds 1 MiB")
                while b"\n" in self.partial:
                    line, _, rest = self.partial.partition(b"\n")
                    self.partial = bytearray(rest)
                    if line.strip():
                        self.lines.append(line)
                        require(len(self.lines) <= 64, "unsolicited JSON-lines output limit exceeded")

    def send(self, data, deadline=None):
        require(not self.pending, "previous stdin write is pending")
        self.pending.extend(data)
        if self.pending:
            self.selector.register(self.proc.stdin, selectors.EVENT_WRITE, "stdin")
            while self.pending:
                self.pump(deadline)

    def record(self, timeout):
        deadline = time.monotonic() + timeout
        while not self.lines:
            if not any(key.data == "stdout" for key in self.selector.get_map().values()):
                raise Invalid("process ended before a complete JSON-lines record")
            self.pump(deadline)
        value = json.loads(self.lines.popleft())
        require(isinstance(value, dict), "benchmark record must be a JSON object")
        return value

    def request(self, value, timeout):
        self.check()
        # One outstanding request across the pair, enforced by the benchmark coordinator.
        require(not self.lines, "unexpected benchmark output before request")
        deadline = time.monotonic() + timeout
        start = time.perf_counter_ns()
        self.send(json_bytes(value) + b"\n", deadline)
        response = self.record(max(0.001, deadline - time.monotonic()))
        return {"wall_ns": time.perf_counter_ns() - start, "response": response}

    def run(self, stdin=""):
        self.send(stdin.encode())
        self.proc.stdin.close()
        while self.selector.get_map() or self.proc.poll() is None:
            self.pump()
        code = self.proc.wait()
        result = {"exit_code": code, "wall_ns": int((time.monotonic() - self.started) * 1e9)}
        require(code == 0, f"command exited with status {code}")
        return result

    def finish_session(self):
        """EOF flushes PGO profiles and catches a failing benchmark shutdown."""
        self.proc.stdin.close()
        deadline = time.monotonic() + 10
        while self.selector.get_map() or self.proc.poll() is None:
            self.pump(deadline)
        require(self.proc.wait() == 0, "benchmark failed on shutdown")

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.proc is not None:
                # Kill this job's group even if the immediate parent already exited.
                try:
                    os.killpg(self.proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    self.proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
                # On Darwin an exiting orphan group can transiently return EPERM.
                # Give SIGTERM a short grace period even when the direct child is reaped.
                time.sleep(0.05)
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CleanupError(f"cannot confirm process-group cleanup for {self.proc.pid}: {exc}") from exc
        finally:
            if self.proc is not None:
                for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                    if stream:
                        stream.close()
            self.selector.close()
            for stream in self.files.values():
                stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
