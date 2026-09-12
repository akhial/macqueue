import subprocess
import tempfile
from pathlib import Path

from .common import require
from .policy import Policy


def doctor(config):
    """Exercise the actual OS sandbox before accepting jobs. No remote code executes."""
    policy = Policy(config)
    state = Path(config["state_dir"])
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    results = {}
    with tempfile.TemporaryDirectory(prefix="doctor-", dir=state) as temporary:
        base = Path(temporary)
        root = base / "job"
        for path in (root / "work/home", root / "work/tmp", root / "artifacts/frozen"):
            path.mkdir(parents=True, exist_ok=True)
        secret = base / "private.txt"
        secret.write_text("sandbox probe")
        profile = policy.sandbox(root)
        env = policy.environment(root, {})
        def probe(label, argv, expected):
            result = subprocess.run(["/usr/bin/sandbox-exec", "-p", profile, *argv], cwd=root / "work", env=env,
                                    capture_output=True, timeout=20)
            require((result.returncode == 0) == expected, f"sandbox probe failed: {label}: {result.stderr.decode(errors='replace')[:1000]}")
            results[label] = "passed"
        probe("write_job", ["/usr/bin/touch", str(root / "work/allowed")], True)
        probe("deny_write_outside", ["/usr/bin/touch", str(base / "forbidden")], False)
        probe("deny_read_control", ["/bin/cat", str(secret)], False)
        probe("deny_write_frozen", ["/usr/bin/touch", str(root / "artifacts/frozen/forbidden")], False)
        probe("cargo", [str(policy.tools["cargo"]), "--version"], True)
        probe("rustc", [str(policy.tools["rustc"]), "-Vv"], True)
        # Curl uses a loopback HTTP listener, so a refused connection cannot produce a false pass.
        import http.server
        import threading
        class Quiet(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
            def log_message(self, *_):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            require(subprocess.run(["/usr/bin/curl", "-fsS", "--max-time", "3", url], capture_output=True).returncode == 0, "network probe listener failed")
            probe("deny_network", ["/usr/bin/curl", "-fsS", "--max-time", "3", url], False)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
    return {"ok": True, "checks": results, "capabilities": sorted(policy.capabilities)}
