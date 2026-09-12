# Reversible Debian 13 queue

The VPS stores jobs, logs and artifacts. Cargo and benchmark execution happens on
the separately provisioned Mac. The installed service binds only to the selected
Tailscale IPv4 address on port 8787. It does not alter SSH, firewall, Tailscale
policy, shell configuration, or existing source repositories.

## Install

Stage this repository without `.git`, `.state`, or credentials, then run on Debian:

```sh
python3 deploy/debian/install.py plan --source "$PWD" \
  --host 100.102.112.115 --agent adel \
  --repository /home/adel/code/shpd-seed-seeker \
  --worktree /home/adel/.t3/worktrees/shpd-seed-seeker/t3code-b11a8beb \
  --output plan.json
sudo /usr/bin/python3 deploy/debian/install.py apply plan.json
sudo /usr/bin/python3 /opt/macqueue/install.py verify
sudo /usr/bin/python3 -I -B /opt/macqueue/smoke.py
```

Installation refuses existing resources. It creates a locked `macqueue` system
user/group with `/usr/sbin/nologin`, root-owned `/opt/macqueue` and `/etc/macqueue`,
private service state `/var/lib/macqueue`, a systemd unit, and
`/usr/local/bin/macqueue`. The existing agent account receives only the submitter
token at `~/.config/macqueue/submit.token` (0600). Both source credentials in
`/etc/macqueue` are root-only; systemd supplies private runtime credential copies
to the queue; its launcher makes 0600 copies in the private, temporary
`/run/macqueue` directory, removed when the service stops. The worker credential
is not given to the agent CLI. Agents running
as `adel` retain that account's existing sudo authority; these token roles do not
remove it.

No additional Debian packages are installed. The service uses Debian's Python
standard library. Its systemd restrictions deny access to home directories and
permit persistent writes only under its state directory. Queue artifacts retain
their existing 512 MiB per-upload limit; retention is manual with
`prune-server`. There is no total disk quota or automatic pruning.

## Agent commands on devbox

`macqueue` is on the existing PATH and defaults to `http://100.102.112.115:8787`
and the local submitter credential. HTTP is carried over the private Tailscale
transport. An explicitly overridden remote HTTP endpoint requires `--allow-http`.

```sh
macqueue list
macqueue source-status
macqueue plan correctness --project seedfinder --candidate FULL_COMMIT_SHA > correctness.json
macqueue validate correctness.json
macqueue submit correctness.json --key seedfinder-correctness-001
macqueue get JOB_ID
macqueue logs JOB_ID --follow
macqueue wait JOB_ID
macqueue download JOB_ID results.tar.gz
macqueue cancel JOB_ID
```

The correctness plan runs fmt, Clippy, and release tests with the GTK exclusion.
Add `--test-filter module_name::test_name` for a focused core test instead.

```sh
macqueue plan benchmark --project seedfinder \
  --baseline FULL_BASELINE_SHA --candidate FULL_CANDIDATE_SHA \
  --query query.json --seeds 123,456,789 \
  --seed-count 100000 --warmups 2 --samples 10 > benchmark.json
macqueue validate benchmark.json
macqueue submit benchmark.json --key seedfinder-benchmark-001
```

The generated job builds and freezes both binaries, then compares `seed-seeker`
and the persistent `match_benchmark` with alternating samples at 1, performance,
and available core counts. Supply the actual query JSON accepted by the project.
The benchmark's readiness object is `{"ready":true,...}`; if adding an explicit
`ready` condition, use `{"field":"ready","equals":true}`. Full response
records are retained for domain-specific comparison of matches and witnesses.
The VPS's Python benchmark harness is not authorized to run as an arbitrary Mac
job; use structured steps, or separately approve a specific harness hash.

## Provision source before build/test jobs

Use full lowercase 40-character commit SHAs. `source-status` shows the VPS main
repository and active worktree without refreshing their indexes; it does not
report Mac provisioning state. Commit a candidate using the normal review
workflow first; uncommitted worktree edits are excluded.

After one-time worker enablement of `source-provision`, run:

```sh
macqueue provision --candidate FULL_CANDIDATE_SHA --baseline FULL_BASELINE_SHA
```

`--baseline` is optional. The helper exports exact commits, vendors their locked
dependencies on the VPS, uploads the package, and waits for a Mac provisioning
job. Use its `succeeded` status and `result.source_provisioning` as confirmation
before submitting build/test/benchmark jobs, which remain offline. With
`--no-wait`, wait separately using `macqueue wait JOB_ID`. Routine committed source
and crates.io dependency changes need no Mac sudo or administrator handoff.

See [self-service provisioning](self-service-provisioning.md) for supported
dependencies, retries, and limits, and [agent operations](agent-operations.md) for
job submission. On the installed VPS, these guides are under
`/home/adel/code/macqueue/docs/`.

`export-source` is an operator fallback for initial setup or a separately reviewed
import; a standalone bundle export does not provision the Mac. Follow the
[operator provisioning procedure](macos-provisioning.md) only when that path is
requested. Repository Cargo configuration, Git/alternative-registry dependencies,
and toolchain or worker-policy changes require operator review.

## Pair the Mac

Copy `/etc/macqueue/worker.token` securely to the Mac's configured worker token
path without printing it. Set its URL to `http://100.102.112.115:8787` and
`allow_http` to `true`, preserving the dedicated account and file ownership.
The Mac's current placeholder URL/token are not paired automatically by this
installer. Provision sources and dependencies, run its doctor checks, and then
resume it locally. Keep it paused until these steps pass. No inbound Mac service
or GUI login is required.

## Operations and rollback

```sh
sudo systemctl status macqueue.service
sudo journalctl -u macqueue.service -n 50 --no-pager
sudo systemctl stop macqueue.service
sudo systemctl start macqueue.service
sudo /usr/bin/python3 /opt/macqueue/install.py verify
sudo /usr/bin/python3 /opt/macqueue/install.py rollback
```

Rollback stops/disables the service, verifies the recorded account identity and
installed external file hashes, archives this installation to a root-only
`/var/backups/macqueue-<installation-id>-<timestamp>.tar.gz`, then removes its
account, group, directories, CLI and unit. The archive includes credentials and
queue artifacts: keep it private, or delete that exact archive later. Use
`rollback --purge` to omit the archive. It refuses modified external files or
replaced identities; review those explicitly instead of deleting unrelated work.

Existing system journal/audit records and normal account-tool backup files may
remain. Rollback does not restore whole `/etc/passwd` backups, which could erase
unrelated account changes. Git bundles/results created later by agents remain
where agents saved them. Existing repositories, worktrees and global network
settings are never part of rollback.
