# Provision revisions without an administrator handoff

After the one-time worker update enables `source-provision`, run on `devbox`:

```sh
macqueue provision --candidate FULL_CANDIDATE_SHA --baseline FULL_BASELINE_SHA
```

`--baseline` is optional when only the candidate needs importing. The command exports exact committed revisions from the configured repository, vendors their locked dependencies on the VPS, uploads a checksummed package, submits a provisioning job, and waits for its result. Success means the revisions and their offline dependency graph are available for Mac jobs. It does not assert correctness or performance.

Then submit ordinary jobs with those SHAs:

```sh
macqueue plan correctness --project seedfinder --candidate FULL_CANDIDATE_SHA > correctness.json
macqueue submit correctness.json
```

Routine revisions and crates.io dependency changes need no worker-config edits, `candidate-request.json`, worker credential transfer, or macOS `sudo`. Worker, toolchain and permission changes remain operator tasks.

The prepared one-time enablement command installs the worker update, proves source import and an offline build as the hidden service account, then publishes `.state/selfserve-status.json` on the VPS automatically. It also submits a confirmation through the agent's unprivileged CLI. The agent can inspect that job directly; no relay through another assistant is required.

## Status and retries

The command prints its job ID after submission and waits by default. Use `--no-wait` to return immediately, then use ordinary `get`, `wait`, `logs`, `download`, or `cancel` commands. Interrupting the waiting CLI does not cancel an already submitted job. Queue cancellation/expiry rules apply; an offline Mac can pick up queued work when it returns.

Use `--output /PATH/source.tar.gz` to retain the exact package; the path must not exist. Uploads are immutable by SHA-256. `--key` provides submission idempotency for an identical job/package. Success is recorded in `result.source_provisioning`: package hash, revisions, Cargo.lock hashes, and verification time. Artifacts contain `provision.json` and offline metadata logs. This job result is the provisioning confirmation; operator-written readiness JSON does not enumerate future self-service imports automatically.

Failed validation never publishes a snapshot. Inspect the result/artifacts, fix the committed source or dependencies, and retry. If delivery failed after publication, a retry verifies and reuses the valid snapshot. Cancellation after publication may leave a valid cached package; revoke it if desired.

## Execution and isolation

The VPS helper runs fixed Git export/checkout and `cargo vendor --locked --versioned-dirs` commands in disposable directories. A separate cache lives at `/home/adel/.config/macqueue/provision-cargo`. It does not run builds/build scripts or change the agent's active checkout, index, refs, or uncommitted files. Dependency downloads happen on the VPS, outside timed Mac work.

The Mac makes outbound queue requests only. A provisioning lease can download only its referenced package. The worker verifies its hash, safely extracts regular files, verifies exact project/revision and lockfile identities, and runs sandboxed `cargo metadata --locked --offline` with generated vendor configuration. Repository Cargo configuration and Git/alternative-registry dependencies require operator review; crates.io and workspace dependencies are supported.

Validated snapshots live at `/Library/Macqueue/state/sources/PROJECT/PACKAGE_SHA/`. Later jobs select them by commit SHA, clone fresh disposable checkouts, and use per-checkout Cargo homes pointing at the selected vendor directory. Environment artifacts record `source_packages` per variant. Original protected mirrors remain usable for previously provisioned revisions.

This API cannot modify the worker program, configuration, credentials, toolchain, login policy or protected mirror. Downloaded code executes through the existing approved commands and sandbox. The service account remains unprivileged, hidden, and unable to authenticate or start a GUI session.

## Limits and rollback

Packages allow 512 MiB compressed, 4 GiB expanded, at most 100,000 members, and bounded extension headers. Extraction rejects traversal, duplicate members, links, sparse/special files, and unexpected paths. The Mac cache allows 16 GiB and 64 snapshots. VPS inputs allow 16 GiB and 1,000 files; `prune-server` retention removes old unreferenced inputs.

Remove an unused worker-managed package through the queue:

```sh
macqueue revoke-source --candidate FULL_SHA --sha256 PACKAGE_SHA256
```

Revocation runs between workloads and removes only that Mac snapshot. It preserves original repositories, the protected mirror, job artifacts, and the uploaded VPS package. Run `provision` again to restore it. If several packages contain a revision, revoke each to remove all uploaded copies. Revisions also present in the protected mirror remain available through it.

The feature installation has a rollback receipt restoring previous modules/configuration. It retains source/job artifacts for review; with `source-provision` disabled, the worker stops selecting uploaded snapshots.
