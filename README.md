# Macqueue

A small pull-based job queue for an Apple Silicon Mac and a Linux VPS. Python 3.11+; no runtime packages, broker, or database server to install.

The VPS stores jobs in SQLite. The Mac long-polls the VPS, creates disposable Git checkouts, runs locally approved operations, and uploads logs, results, and artifacts. The Mac opens no listening port. Agents receive a submitter token; only the Mac receives a worker token.

Included:

- Offline release builds for `seed-seeker` and `match_benchmark`, targeting `aarch64-apple-darwin`.
- Separate frozen baseline/candidate executables with SHA-256 identities.
- Warmups and alternating AB/BA benchmark samples at 1, performance-core, and available-core counts. Persistent JSON-lines sessions keep one request active at a time.
- Formatting, Clippy, workspace tests, and focused core tests.
- Optional profiling, executable inspection, and PGO using tools bundled with the pinned Rust toolchain.
- Explicit argv, working directory, environment, stdin, timeouts, and output paths. No remote shell, Python snippets, arbitrary executables, fetch URLs, or PIDs.
- Durable queue state, idempotent submissions, cancellation, worker leases, queue expiry, bounded output, local pause, and artifact downloads with checksum verification.
- macOS sandbox checks before polling; a process group for each launched command.

## Deployment

Use a **dedicated non-administrator macOS account**. The worker refuses root and administrator accounts. For unreviewed agent code, install the worker and tools **inside a disposable macOS VM**, without host home-directory or credential shares. Builds and tests execute repository code; the command policy alone is not isolation.

The built-in Seatbelt profile denies network access and restricts writes to each job's work directory, its selected output paths, and a dedicated Cargo cache. Frozen executables are outside those write permissions. Worker credentials, source mirrors, toolchains, and control files remain outside writable directories. `sandbox-exec` is a deprecated macOS interface, so the worker checks it on startup and has no unsandboxed fallback. This is defense in depth, not a claim that a same-user runner can safely contain every hostile program. See [security and operational limits](docs/security.md).

### 1. VPS

For Debian 13, use the [reversible installer and configured agent CLI](docs/debian-setup.md).
It creates a separate systemd service, binds to the selected Tailscale address,
and includes an ownership-checked rollback command. The manual setup below is an alternative.

Copy this directory to `/opt/macqueue`. Run from that directory, or install with `python3 -m pip install .` in a virtual environment. Running `python3 -m macqueue` directly needs no package installation or network access.

Create a service account and its private directories (example for a Linux system with `useradd`), then create credentials as that account:

```sh
sudo useradd --system --home-dir /var/lib/macqueue --shell /usr/sbin/nologin macqueue
sudo install -d -o macqueue -g macqueue -m 700 /etc/macqueue /var/lib/macqueue
cd /opt/macqueue
sudo -u macqueue /usr/bin/python3 -m macqueue tokens /etc/macqueue
sudo -u macqueue /usr/bin/python3 -m macqueue server \
  --state /var/lib/macqueue \
  --host 127.0.0.1 --port 8787 \
  --submit-token-file /etc/macqueue/submit.token \
  --worker-token-file /etc/macqueue/worker.token
```

Skip account creation if it already exists. Keep `/opt/macqueue` administrator-owned and readable by the service account. [deploy/macqueue.service](deploy/macqueue.service) is a systemd unit with these paths. Stop the foreground server before installing the unit and starting the service:

```sh
sudo install -m 644 deploy/macqueue.service /etc/systemd/system/macqueue.service
sudo systemctl daemon-reload
sudo systemctl enable --now macqueue
```

Choose one transport:

- **Tailscale address:** change the service's `--host` to the VPS's actual Tailscale IPv4 address and add `--allow-http-private`. Set the Mac URL to `http://VPS_TAILSCALE_IP:8787` and `allow_http: true`. HTTP runs inside Tailscale's encrypted connection. Do not bind this configuration to a public interface.
- **HTTPS:** keep the API on loopback and put your existing HTTPS reverse proxy in front of it. [deploy/Caddyfile](deploy/Caddyfile) is an optional example. Use `https://YOUR_HOST` and omit `allow_http` on the Mac. Certificate verification is always enabled.

Permit Mac-to-VPS TCP access to the queue port and restrict agent access to the VPS API. Remove existing broad Tailscale rules if they permit VPS-to-Mac connections you do not want; a narrow additional rule does not override an existing allow. This project does not change your network policy.

### 2. Provision the Mac separately

In the dedicated account, install Python 3.11+, Xcode/Command Line Tools, and your **project's pinned** Rust toolchain with `rustfmt` and `clippy`. Complete Xcode first-run setup and license acceptance locally. These are provisioning operations, never agent-submitted jobs.

Resolve and record the installed toolchain directory:

```sh
rustc --print sysroot
rustc -Vv
```

Use that absolute directory in the worker config, ideally a versioned toolchain rather than a moving `stable` installation. The worker calls its Cargo/rustc binaries directly, bypassing repository rustup overrides. Add `llvm-tools-preview` to that same toolchain when enabling PGO or `llvm-objdump`. Rust documents why [PGO must use compatible LLVM tools](https://doc.rust-lang.org/rustc/profile-guided-optimization.html).

Provision an allowlisted local mirror and an isolated dependency cache. Substitute your real repository and revision:

```sh
mkdir -p /Users/macworker/mirrors /Users/macworker/macqueue-cargo
git clone --mirror YOUR_REPOSITORY_URL /Users/macworker/mirrors/seedfinder.git
git clone /Users/macworker/mirrors/seedfinder.git /Users/macworker/provision-seedfinder
git -C /Users/macworker/provision-seedfinder checkout FULL_COMMIT_SHA
cd /Users/macworker/provision-seedfinder
CARGO_HOME=/Users/macworker/macqueue-cargo cargo fetch --locked --target aarch64-apple-darwin
```

Populate dependencies for **both** baseline and candidate revisions and any host-target correctness dependencies. Re-run provisioning when dependencies change. Do not put Cargo credentials in this cache. Fetch authentication belongs to a separate provisioning environment; the job cannot fetch missing dependencies. Avoid sharing this mutable cache across trust boundaries; rebuild it between untrusted runs or reset the VM.

Mirror updates also happen outside jobs:

```sh
git -C /Users/macworker/mirrors/seedfinder.git fetch --prune origin
```

The worker accepts only exact full commit SHAs already in the mirror. It does not update submodules or download Git LFS objects. Provision repositories without those runtime dependencies, or extend the trusted provisioning process first.

### 3. Configure and start the Mac worker

For a hidden account that cannot log in to a GUI, use the [reversible system LaunchDaemon installer](docs/macos-setup.md). It creates a disabled service identity, copies the toolchain outside your home, runs local checks, and starts paused. The manual LaunchAgent option below is for accounts that are allowed a graphical login.

Copy this project to `/Users/macworker/macqueue`. Copy [examples/worker.json](examples/worker.json) to `/Users/macworker/.config/macqueue/worker.json` and replace the server address, account paths, project mirror, and toolchain path. Transfer **only `worker.token`** to the Mac's configured token path. Use a trusted existing transfer method; Mac SSH access is unnecessary.

```sh
chmod 600 /Users/macworker/.config/macqueue/worker.json
chmod 600 /Users/macworker/.config/macqueue/worker.token
mkdir -p /Users/macworker/macqueue-state
cd /Users/macworker/macqueue
python3 -m macqueue doctor --config /Users/macworker/.config/macqueue/worker.json
python3 -m macqueue worker --config /Users/macworker/.config/macqueue/worker.json
```

`doctor` verifies tool execution and actual denials of network access, control-file reads, writes outside the job, and writes to frozen files. The worker repeats these probes on startup. Missing SDK/toolchain access fails the job; there is no automatic permission expansion. Use narrowly scoped `read_roots` for extra installed build tools. `developer_dir` can pin a non-default Xcode installation.

For automatic startup, adjust the Python executable and paths in [deploy/local.macqueue.worker.plist](deploy/local.macqueue.worker.plist), copy it into the dedicated account's `~/Library/LaunchAgents/`, and load it **from that account's graphical login session**:

```sh
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/local.macqueue.worker.plist
```

Only one worker instance may own a state directory. The VPS also allows only one running job globally. The LaunchAgent runs while that account is logged in. Sleep makes the worker unavailable; `caffeinate` is an optional per-command wrapper and does not wake a sleeping Mac. Queued jobs wait until their expiry; running jobs can lose their lease during sleep.

### 4. Agents submit reviewable JSON

For the configured devbox CLI and source-provisioning handoff, read the
[agent operations guide](docs/agent-operations.md).

On the VPS, give agents the **submitter token**, not the worker token. The CLI is also a convenient agent tool interface:

```sh
export MACQUEUE_URL=http://127.0.0.1:8787
export MACQUEUE_TOKEN_FILE=/etc/macqueue/submit.token

python3 -m macqueue plan benchmark \
  --project seedfinder \
  --baseline FULL_BASELINE_SHA --candidate FULL_CANDIDATE_SHA \
  --query query.json --seeds 123,456,789 \
  --seed-count 100000 --warmups 2 --samples 10 > benchmark.json

python3 -m macqueue validate benchmark.json
python3 -m macqueue submit benchmark.json --key seedfinder-comparison-001
```

`query.json` must contain the real query object accepted by your `match_benchmark`; this project does not invent its domain schema. The plan generator prints all argv and paths for review. The Mac revalidates the complete job against its own policy before creating checkouts. Use the same idempotency key to retry an uncertain submission without creating a second job.

Use the returned job ID:

```sh
python3 -m macqueue get JOB_ID
python3 -m macqueue logs JOB_ID --follow
python3 -m macqueue wait JOB_ID
python3 -m macqueue download JOB_ID results.tar.gz
python3 -m macqueue cancel JOB_ID
```

`wait` exits nonzero unless the job succeeded. `download` refuses to overwrite an existing file and verifies the server's SHA-256. It saves an archive without extracting it. Logs are a bounded live preview; complete command output is in artifacts. Failed jobs also upload available artifacts. If upload fails, local files and a receipt remain on the Mac.

Correctness and focused test plans:

```sh
python3 -m macqueue plan correctness --project seedfinder --candidate FULL_SHA > correctness.json
python3 -m macqueue plan correctness --project seedfinder --candidate FULL_SHA \
  --test-filter module_name::test_name > focused.json
```

Optional PGO:

```sh
python3 -m macqueue plan pgo --project seedfinder --candidate FULL_SHA \
  --query query.json --seeds 123,456,789 > pgo.json
```

Enable `pgo` locally before submitting. The plan builds an ordinary baseline, trains a separate instrumented binary, merges raw profiles with the pinned Rust toolchain's `llvm-profdata`, and builds a PGO candidate in a separate target directory. Adjust the training seeds for the workload. `--profiling` on `plan benchmark` uses the repository's `profiling` profile and preserves symbols; enable `profiling` locally. See [the job format](docs/jobs.md) for `sample`, `xctrace`, inspection, wrappers, and artifact operations.

## Operations

```sh
python3 -m macqueue pause --config /Users/macworker/.config/macqueue/worker.json
python3 -m macqueue resume --config /Users/macworker/.config/macqueue/worker.json
python3 -m macqueue list
```

Pause stops new claims and lets the active job finish. Cancel by job ID to stop a running job. Graceful worker termination cancels its process groups. After a crash or lost lease, the server marks the job `lost` and **never automatically replays it**. Review its artifacts, then submit a new request explicitly. This avoids unexpected duplicate benchmark execution after laptop sleep or a network outage.

Job timeout defaults to four hours, queue expiry to one day; each is explicit in the job. Heartbeats renew a 60-second lease; a separate local watchdog cancels execution after 40 seconds without a confirmed renewal. Local disk monitoring, minimum free space, output caps, and upload caps are configurable. Disk monitoring is periodic, not a filesystem quota.

Clean up completed jobs explicitly:

```sh
python3 -m macqueue prune-server --state /var/lib/macqueue --days 30
# Stop the LaunchAgent/worker before local pruning; pause alone keeps its lock.
python3 -m macqueue prune-worker --config /Users/macworker/.config/macqueue/worker.json --days 7
```

Local pruning removes only jobs with a successfully delivered final receipt; undelivered results are retained for recovery. Server pruning removes terminal jobs and their artifacts. Back up SQLite with its online backup API or stop the server before copying the database; include artifact files. Rotate tokens by updating protected files and restarting the corresponding services.

An unclean worker exit leaves `STATE_DIR/ACTIVE.json`. The next worker refuses new jobs until you inspect and stop leftover execution (or reset the VM), then remove that marker. It does not kill recorded PIDs after a restart, since those IDs may have been reused. Artifact delivery failures are retained even when their failure status reached the VPS.

## Verification

```sh
python3 -m unittest discover -v
```

Portable tests cover authentication, leases, durable recovery, idempotency, cancellation, artifact transfers, command/path/env rejection, streaming IO, deadlines, and process groups. Apple Silicon tests create a tiny dependency-free Rust workspace and run actual sandboxed offline builds, correctness checks, frozen executable comparisons, and artifact packaging. They do not build your private seedfinder repository. Real project performance, domain-specific response validation, Instruments permissions, and VM behavior must be checked on your deployment.

The server's [HTTP API](docs/api.md) is intentionally small. There is no web UI or bundled MCP server; agents can use the CLI or HTTP directly.
