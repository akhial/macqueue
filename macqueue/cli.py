import argparse
import json
import logging
import os
import secrets
import sys
import time
import uuid
from pathlib import Path

from .client import Client, RemoteError
from .common import Invalid, private_write, read_secret, require
from .plans import benchmark, correctness, pgo
from .schema import MAX_JOB_BYTES, validate_job
from .store import Store, TERMINAL


def print_json(value):
    print(json.dumps(value, indent=2, allow_nan=False))


def parser(*, extra_commands=()):
    root = argparse.ArgumentParser(prog="macqueue", description="Pull-based Cargo and benchmark jobs for Apple Silicon")
    sub = root.add_subparsers(dest="action", required=True)
    tokens = sub.add_parser("tokens", help="create separate 0600 submitter and worker tokens")
    tokens.add_argument("directory", type=Path)
    server = sub.add_parser("server", help="run the VPS queue API")
    server.add_argument("--state", type=Path, required=True)
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8787)
    server.add_argument("--submit-token-file", type=Path, required=True)
    server.add_argument("--worker-token-file", type=Path, required=True)
    server.add_argument("--allow-http-private", action="store_true")
    server.add_argument("--max-artifact-mib", type=int, default=512)
    for action in ("worker", "doctor", "pause", "resume", "prune-worker"):
        cmd = sub.add_parser(action)
        cmd.add_argument("--config", type=Path, required=True)
        if action == "worker":
            cmd.add_argument("--once", action="store_true")
        if action == "prune-worker":
            cmd.add_argument("--days", type=int, default=7)
    prune = sub.add_parser("prune-server")
    prune.add_argument("--state", type=Path, required=True)
    prune.add_argument("--days", type=int, default=30)
    validate = sub.add_parser("validate")
    validate.add_argument("file", type=Path)
    plan = sub.add_parser("plan", help="print a complete editable JSON job")
    plan.add_argument("kind", choices=["benchmark", "correctness", "pgo"])
    plan.add_argument("--project", required=True)
    plan.add_argument("--candidate", required=True)
    plan.add_argument("--baseline")
    plan.add_argument("--query", type=Path, help="path to the project's actual query JSON")
    seeds = plan.add_mutually_exclusive_group()
    seeds.add_argument("--seeds", help="comma-separated matching seed IDs (default: 123,456,789)")
    seeds.add_argument("--seeds-file", type=Path, help="JSON array of matching seed IDs (up to 1048576)")
    seeds.add_argument("--seed-range", metavar="START:COUNT",
                       help="consecutive matching seed IDs; up to 1048576 seeds")
    plan.add_argument("--baseline-profile-sha256", help="PGO only: compare the pinned checked-in target profile against fresh training")
    plan.add_argument("--seed-count", type=int, default=100000)
    plan.add_argument("--warmups", type=int, default=2)
    plan.add_argument("--samples", type=int, default=10)
    plan.add_argument("--test-filter")
    plan.add_argument("--profiling", action="store_true")
    for action in ("submit", "get", "list", "cancel", "logs", "download", "wait"):
        cmd = sub.add_parser(action)
        cmd.add_argument("--url", default=os.environ.get("MACQUEUE_URL", "http://127.0.0.1:8787"))
        cmd.add_argument("--token-file", type=Path, default=os.environ.get("MACQUEUE_TOKEN_FILE"))
        cmd.add_argument("--allow-http", action="store_true")
        if action == "submit":
            cmd.add_argument("file", type=Path)
            cmd.add_argument("--key", default=None, help="reuse this key to retry submission without duplicating the job")
        elif action != "list":
            cmd.add_argument("job_id")
        if action == "download":
            cmd.add_argument("destination", type=Path)
        if action == "logs":
            cmd.add_argument("--after", type=int, default=-1)
            cmd.add_argument("--follow", action="store_true")
    for name, help_text in extra_commands:
        sub.add_parser(name, help=help_text)
    return root


def main(argv=None, *, extra_commands=()):
    os.umask(0o077)
    args = parser(extra_commands=extra_commands).parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        action = args.action
        if action == "tokens":
            args.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            require(not any((args.directory / f"{role}.token").exists() for role in ("submit", "worker")), "token file already exists")
            for role in ("submit", "worker"):
                private_write(args.directory / f"{role}.token", (secrets.token_urlsafe(48) + "\n").encode())
            print_json({"directory": str(args.directory), "files": ["submit.token", "worker.token"]})
        elif action == "server":
            from .server import Server
            require(args.host in ("127.0.0.1", "localhost", "::1") or args.allow_http_private,
                    "binding non-loopback HTTP requires --allow-http-private; bind only to your private network address")
            require(1 <= args.max_artifact_mib <= 2048, "artifact limit must be 1..2048 MiB")
            server = Server((args.host, args.port), Store(args.state), read_secret(args.submit_token_file),
                            read_secret(args.worker_token_file), args.max_artifact_mib * 1024**2)
            logging.info("Queue listening on %s:%s", *server.server_address)
            try:
                server.serve_forever()
            finally:
                server.server_close()
        elif action in ("worker", "doctor", "pause", "resume", "prune-worker"):
            from .worker import Worker, load_config, prune_worker
            config = load_config(args.config, check_account=action in ("worker", "doctor"))
            state = Path(config["state_dir"])
            if action == "worker":
                Worker(config).run(once=args.once)
            elif action == "doctor":
                from .doctor import doctor
                print_json(doctor(config))
            elif action == "prune-worker":
                require(args.days >= 1, "retention must be at least one day")
                print_json({"pruned": prune_worker(config, args.days)})
            elif action == "pause":
                state.mkdir(parents=True, exist_ok=True, mode=0o700)
                (state / "PAUSED").touch(mode=0o600)
                print_json({"paused": True, "active_job": "continues; use cancel to stop it"})
            elif action == "resume":
                (state / "PAUSED").unlink(missing_ok=True)
                print_json({"paused": False})
        elif action == "prune-server":
            require(args.days >= 1, "retention must be at least one day")
            print_json({"pruned": Store(args.state).prune(args.days)})
        elif action == "validate":
            validate_job(json.loads(args.file.read_text()))
            print_json({"valid": True, "note": "the Mac also enforces its local command policy"})
        elif action == "plan":
            if args.kind == "correctness":
                value = correctness(args.project, args.candidate, args.test_filter)
            else:
                require(args.query is not None, "provide --query with a valid query JSON for your project")
                if args.seed_range is not None:
                    start, count = map(int, args.seed_range.split(":"))
                    matching = {"seed_range": {"start": start, "count": count}}
                elif args.seeds_file is not None:
                    with args.seeds_file.open("rb") as stream:
                        raw = stream.read(MAX_JOB_BYTES + 1)
                    require(len(raw) <= MAX_JOB_BYTES, "seeds file exceeds 32 MiB")
                    matching = {"seeds": json.loads(raw)}
                else:
                    matching = {"seeds": [int(s) for s in (args.seeds or "123,456,789").split(",")]}
                kwargs = dict(query=json.loads(args.query.read_text()), **matching,
                              seed_count=args.seed_count, warmups=args.warmups, samples=args.samples)
                if args.kind == "benchmark":
                    require(args.baseline_profile_sha256 is None, "--baseline-profile-sha256 requires a PGO plan")
                    require(args.baseline is not None, "benchmark requires --baseline")
                    value = benchmark(args.project, args.baseline, args.candidate, profiling=args.profiling, **kwargs)
                else:
                    require(not args.profiling, "--profiling requires a benchmark plan")
                    value = pgo(args.project, args.candidate, baseline_profile_sha256=args.baseline_profile_sha256, **kwargs)
            print_json(value)
        else:
            require(args.token_file is not None, "provide --token-file or MACQUEUE_TOKEN_FILE")
            client = Client(args.url, read_secret(args.token_file), allow_http=args.allow_http)
            if hasattr(args, "job_id"):
                require(len(args.job_id) == 32 and all(c in "0123456789abcdef" for c in args.job_id), "invalid job ID")
            if action == "submit":
                spec = validate_job(json.loads(args.file.read_text()))
                key = args.key or uuid.uuid4().hex
                print(f"Idempotency-Key: {key}", file=sys.stderr)
                print_json(client.request("POST", "/v1/jobs", spec, key=key))
            elif action == "list":
                print_json(client.request("GET", "/v1/jobs"))
            elif action in ("get", "cancel"):
                print_json(client.request("GET" if action == "get" else "POST", f"/v1/jobs/{args.job_id}" + ("/cancel" if action == "cancel" else "")))
            elif action == "download":
                print_json(client.download(args.job_id, args.destination))
            elif action == "wait":
                while True:
                    job = client.request("GET", f"/v1/jobs/{args.job_id}")
                    if job["status"] in TERMINAL:
                        print_json(job)
                        if job["status"] != "succeeded":
                            raise SystemExit(1)
                        break
                    time.sleep(2)
            elif action == "logs":
                after = args.after
                while True:
                    records = client.request("GET", f"/v1/jobs/{args.job_id}/logs?after={after}")
                    for record in records:
                        print(record["text"], end="", flush=True)
                        after = record["seq"]
                    if not args.follow:
                        break
                    if client.request("GET", f"/v1/jobs/{args.job_id}")["status"] in TERMINAL and len(records) < 100:
                        break
                    time.sleep(1)
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (Invalid, ValueError, OSError, RemoteError) as exc:
        print(f"macqueue: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
