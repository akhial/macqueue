#!/usr/bin/env python3
"""Installed agent entry point; source inspection/export never changes source refs."""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

SOURCE_COMMANDS = (("source-status", "show repository/worktree revisions and uncommitted changes"),
                   ("export-source", "export exact committed revisions to a standalone Git bundle"),
                   ("provision", "vendor and provision exact source revisions on the Mac; no administrator handoff"),
                   ("revoke-source", "remove a worker-managed source package; original repositories are preserved"))


def git(path, *args):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
    for key in list(env):
        if key.startswith("GIT_") and key not in ("GIT_OPTIONAL_LOCKS", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL"):
            del env[key]
    result = subprocess.run(["/usr/bin/git", "-C", str(path), *args], env=env,
                            text=True, capture_output=True, check=True)
    return result.stdout.rstrip("\n")


def source_status(config):
    return [{"path": config[key], "commit": git(config[key], "rev-parse", "HEAD"),
             "branch": git(config[key], "branch", "--show-current"),
             "changes": git(config[key], "status", "--porcelain").splitlines()}
            for key in ("repository", "worktree")]


def export_source(config, baseline, candidate, output):
    for revision in (baseline, candidate):
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("source revisions must be full lowercase 40-character commit SHAs")
        if git(config["repository"], "cat-file", "-t", revision) != "commit":
            raise ValueError("revision is not a commit")
    output = Path(output).absolute()
    status = source_status(config)
    common = Path(git(config["repository"], "rev-parse", "--path-format=absolute", "--git-common-dir"))
    # Temporary refs belong only to this temporary bare repository. No fetch,
    # commit, checkout, index refresh or ref creation in the agent's repositories.
    with tempfile.TemporaryDirectory(prefix=".macqueue-export-", dir=output.parent) as temp:
        temp = Path(temp)
        bare = temp / "export.git"
        git(temp, "init", "--bare", str(bare))
        (bare / "objects/info/alternates").write_text(str(common / "objects") + "\n")
        for name, revision in (("baseline", baseline), ("candidate", candidate)):
            git(bare, "update-ref", "refs/heads/" + name, revision)
        bundle = temp / "source.bundle"
        git(bare, "bundle", "create", str(bundle), "refs/heads/baseline", "refs/heads/candidate")
        empty = temp / "verify.git"
        git(temp, "init", "--bare", str(empty))
        git(empty, "bundle", "verify", str(bundle))
        bundle.chmod(0o600)
        os.link(bundle, output)  # Atomic publication; refuses to overwrite.
    return {"bundle": str(output), "baseline": baseline, "candidate": candidate,
            "uncommitted_changes_included": False, "sources": status}


def main():
    os.umask(0o077)
    config = json.loads((Path(__file__).resolve().parent / "client.json").read_text())
    argv = sys.argv[1:]
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    if argv and argv[0] in ('provision', 'revoke-source'):
        from macqueue.client import Client, RemoteError
        from macqueue.common import read_secret, require
        from macqueue.schema import validate_job
        from macqueue.source_build import build_package
        parser = argparse.ArgumentParser(prog='macqueue ' + argv[0])
        parser.add_argument('--project', default='seedfinder')
        parser.add_argument('--candidate', required=True)
        parser.add_argument('--baseline')
        parser.add_argument('--no-wait', action='store_true', help='return the job ID immediately after submission')
        parser.add_argument('--key', help='stable submission key for this package')
        if argv[0] == 'provision':
            parser.add_argument('--output', type=Path, help='retain the package here; refuses overwrite')
        else:
            parser.add_argument('--sha256', required=True)
        args = parser.parse_args(argv[1:])
        try:
            require(args.project == 'seedfinder', 'this installed source helper is configured for seedfinder')
            sources = {'candidate': args.candidate}
            if args.baseline:
                sources['baseline'] = args.baseline
            spec = {'version': 1, 'project': args.project, 'sources': sources, 'timeout_seconds': 1200,
                    'label': 'Source/dependency ' + argv[0], 'steps': [{'id': argv[0], 'op': argv[0], 'sha256': '0' * 64}]}
            validate_job(spec)
            client = Client(config['url'], read_secret(config['token_file']), allow_http=True)
            if argv[0] == 'provision':
                with tempfile.TemporaryDirectory(prefix='macqueue-provision-') as directory:
                    root = Path(directory)
                    bundle = root / 'source.bundle'
                    export_source(config, args.baseline or args.candidate, args.candidate, bundle)
                    destination = args.output.absolute() if args.output else root / 'source.tar.gz'
                    cache = Path(config['token_file']).parent / 'provision-cargo'
                    print('Preparing exact commits and vendored offline dependencies ...', file=sys.stderr, flush=True)
                    build_package(bundle, args.project, list(sources.values()), destination, cache)
                    uploaded = client.upload_input(destination)
                    spec['steps'][0]['sha256'] = uploaded['sha256']
            else:
                spec['steps'][0]['sha256'] = args.sha256
            validate_job(spec)
            job = client.request('POST', '/v1/jobs', spec, key=args.key or uuid.uuid4().hex)
            print('Provisioning job: ' + job['id'], file=sys.stderr, flush=True)
            if not args.no_wait:
                from macqueue.store import TERMINAL
                while job['status'] not in TERMINAL:
                    time.sleep(2)
                    job = client.request('GET', '/v1/jobs/' + job['id'])
            print(json.dumps(job, indent=2))
            if not args.no_wait and job['status'] != 'succeeded':
                raise SystemExit(1)
        except (ValueError, OSError, RemoteError, subprocess.SubprocessError) as exc:
            parser.exit(2, str(exc) + '\n')
        return
    if argv and argv[0] in ("source-status", "export-source"):
        parser = argparse.ArgumentParser(prog="macqueue " + argv[0])
        if argv[0] == "export-source":
            parser.add_argument("--baseline", required=True)
            parser.add_argument("--candidate", required=True)
            parser.add_argument("--output", type=Path, required=True)
        args = parser.parse_args(argv[1:])
        try:
            result = source_status(config) if argv[0] == "source-status" else export_source(
                config, args.baseline, args.candidate, args.output)
            print(json.dumps(result, indent=2))
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            parser.exit(2, str(exc) + "\n")
        return
    # Only the configured Tailscale endpoint opts in to HTTP automatically.
    if argv and argv[0] in ("submit", "get", "list", "cancel", "logs", "download", "wait"):
        explicit_url = any(arg == "--url" or arg.startswith("--url=") for arg in argv)
        os.environ.setdefault("MACQUEUE_URL", config["url"])
        os.environ.setdefault("MACQUEUE_TOKEN_FILE", config["token_file"])
        if not explicit_url and os.environ["MACQUEUE_URL"] == config["url"]:
            argv.append("--allow-http")
    from macqueue.cli import main as cli
    cli(argv, extra_commands=SOURCE_COMMANDS)


if __name__ == "__main__":
    main()
