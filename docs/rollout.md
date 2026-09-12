# Reversible updates and capability checks

`deploy/update.py` applies a staged, reviewed `plan.json` containing before/after SHA-256 values for every changed file, with replacement files under `files/`. Targets are limited to Macqueue modules and its entry points. It verifies every old/new hash and Python syntax before stopping a service. It saves previous bytes, ownership, permissions, installation identity, and pause state under the installation's private `updates/UUID/` directory. The printed rollback command restores them and refuses to overwrite later unrelated changes. It also handles a partially applied update. Account/login settings, credentials, mirrors, firewall rules and OS privacy permissions remain as installed. Job probes may populate the existing dedicated Cargo cache. The Mac configuration records the approved 256 MiB process-output budget and successful optional capabilities; its previous contents are backed up.

On Debian the updater stops/restarts `macqueue.service`. Queue data, submissions and artifacts remain in place. On macOS it pauses new claims, waits up to five minutes for an active job, stops the LaunchDaemon, and takes the worker's exclusive state lock. If the job has not drained, it leaves new claims paused and asks the operator to retry. It never kills a running workload to install an update.

The Mac rollout plan also pins `rollout-probe.py`, a fixed operator harness run as `_macqueue`. While the daemon is stopped, it uses the existing sandbox and provisioned offline cache to check:

- Sandbox boundaries and the pinned toolchain.
- PGO: hash-verified checked-in baseline, instrumented CLI and matching training, matching-LLVM merge and counter report, optimized rebuild, identical 4,096-seed results at 1/performance/available workers, and frozen/profile artifacts.
- Profiling builds: all three executables with standalone dSYM bundles, plus `llvm-objdump`, `nm`, and `size` inspection.
- `sample` of a newly launched job-owned process, then Instruments template discovery, a short launch trace, and TOC export as independent checks.

Only successful optional checks add their respective `pgo`, `profiling-build`, `sample`, or `xctrace` capability. Permission failures remain in the report and leave the corresponding profiler disabled. A sandbox failure or unconfirmed process cleanup prevents the rollout from resuming. The service remains hidden, has disabled authentication, and gets no GUI session. A normal restart runs the sandbox doctor again before unpausing.

The fixed PGO corpus is a pipeline/correctness smoke test, not representative training or speedup evidence. The optimization agent should design training and held-out workloads after reviewing the report. Missing-function diagnostics are preserved; profile readability alone does not prove hot-function coverage.

Reports and archives live under `/Library/Macqueue/state/rollout-UUID/`; the root-owned update directory also keeps a report copy. Rollback retains these diagnostic artifacts. They are inside the installation and are included in the whole-installation rollback archive. The updater prints the exact update rollback command before making changes. The Mac root step must be run in an administrator's terminal, for example the prepared `.state/finish-rollout.command`; it needs no GUI login for `_macqueue`.

## Current issue fixes

- MQ-001: installed top-level help includes `source-status` and `export-source`.
- MQ-002: matching comparisons accept 1,048,576 explicit seeds or compact ranges up to 1,048,576 seeds. Ranges are expanded by the worker, so both adapters receive ordinary arrays. Jobs allow up to 32 MiB; JSONL records allow up to 64 MiB within the 256 MiB total process budget. Job-carrying API responses accommodate the full job plus queue metadata. Control-message limits remain smaller.
- MQ-003: EOF drain rejects both queued extra records and unterminated trailing bytes, including after the last sample. Tests reproduce both cases.

The VPS agent's untracked `ISSUES.md` belongs to that agent and is preserved during deployment. Consult the live rollout report before treating optional capabilities as available.
