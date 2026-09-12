# Operating Macqueue from the optimization agent

For future revisions, use [self-service provisioning](self-service-provisioning.md):

```sh
macqueue provision --candidate FULL_CANDIDATE_SHA --baseline FULL_BASELINE_SHA
```

After its job succeeds, submit ordinary correctness/comparison jobs with those SHAs. No operator handoff is needed for committed source or crates.io dependency changes. The provisioning job result is authoritative for new imports; the readiness JSON is an operator snapshot. One-time enablement is tracked in `.state/selfserve-status.json`.

On `devbox`, the `macqueue` CLI is already on PATH. It uses the private queue at
`http://100.102.112.115:8787` and the submitter token in
`/home/adel/.config/macqueue/submit.token`. Do not print that token. The installed
CLI and Mac policy enforce the structured job interface described in
[jobs.md](jobs.md). Source code and documentation are cloned at
`/home/adel/code/macqueue`; changes to that clone do not update installed services.

The MQ-001/MQ-002/MQ-003 fixes and staged PGO/profiling rollout are documented in [rollout.md](rollout.md). Check `.state/rollout-status.json` as well as the readiness report: the server/CLI may be updated before the Mac administrator step finishes. Continue ordinary jobs during that interval; use new limits and optional operations only after the report marks the Mac updated.

After rollout, matching requests accept up to **1,048,576 seeds** as an explicit JSON array (`--seeds-file`) or a compact consecutive range (`--seed-range START:COUNT`). The worker expands ranges, so existing adapters need no changes. Ceilings are **32 MiB per job**, **64 MiB per JSONL response**, **256 MiB cumulative stdout/stderr per process**, with the existing **512 MiB artifact budget**. These are ceilings, not workload defaults. Pilot smaller inputs, account for warmups and both variants, and split jobs when necessary. Matching results occur in session stdout and result rows, so artifact usage includes both copies.

PGO plans support `--baseline-profile-sha256 HASH` for the checked-in `pgo/seed-seeker-aarch64-apple-darwin.profdata`. Use a full provisioned source SHA, verify the profile hash in that revision, and supply representative training plus held-out measurements. The generated plan trains CLI and matching with one worker, verifies LLVM versions, preserves raw/merged profiles and counters, emits missing-function diagnostics, and compares at 1/performance/available workers. Inspect those diagnostics before treating results as native deployment evidence. `--profiling` on a benchmark plan requires `profiling-build`; `sample` and `xctrace` have separate capabilities shown in the rollout report.

Read `/home/adel/code/macqueue/.state/worker-readiness.json` before first use. It is
an operator-written provisioning report, not a live heartbeat. `ready` means its
listed revisions and dependencies passed a real Mac verification job. The laptop
may subsequently sleep or be paused. `macqueue get JOB_ID` is authoritative for
job state. Queued work waits for the Mac until the job expires (24 hours by default).

## Revisions and source changes

The project name is `seedfinder`. Use full lowercase 40-character **commit** SHAs.
Build/test/benchmark jobs require those revisions in a validated source package
or the Mac's protected mirror; they do not include uncommitted worktree edits.
The optimization worktree is
`/home/adel/.t3/worktrees/shpd-seed-seeker/t3code-b11a8beb` and shares Git objects
with `/home/adel/code/shpd-seed-seeker`.

```sh
macqueue source-status
```

`source-status` reports the VPS repository and worktree state, not which revisions
are provisioned on the Mac. Use the agent's existing review/commit workflow to
commit a candidate. Do not reset, stash, or change other worktrees just to submit
a job. Provision the chosen revisions directly:

```sh
macqueue provision --candidate FULL_CANDIDATE_SHA --baseline FULL_BASELINE_SHA
```

Omit `--baseline` when only the candidate needs importing. The command exports
the committed source, vendors locked dependencies on the VPS, uploads the package,
and waits for the Mac provisioning job. A `succeeded` job with
`result.source_provisioning` is the confirmation to use before submitting ordinary
jobs with those SHAs. It proves source and offline dependency availability, not
correctness or performance. With `--no-wait`, use `macqueue wait JOB_ID` yourself.

Routine committed source and crates.io dependency changes need no operator
notification, `candidate-request.json`, worker credential transfer, or Mac sudo.
Existing readiness JSON does not enumerate future self-service imports. See
[self-service provisioning](self-service-provisioning.md) for retries and limits.

`export-source` remains a manual bundle export for operator-led initial setup or
an explicitly requested fallback; exporting a bundle alone does not provision it.
The [operator provisioning procedure](macos-provisioning.md) covers that path.
Repository Cargo configuration, Git/alternative-registry dependencies, and
toolchain or worker-policy changes still require operator review. Ordinary
build/test/benchmark commands remain offline.

## Benchmark jobs

Create the actual project query JSON. For a small protocol check, the existing
native `cheap` workload is:

```json
{"max_depth":1,"requirements":[{"kind":"ring"}],"auto_apply_trinket":false}
```

Generate a reviewable job, then validate and submit it:

```sh
macqueue plan benchmark --project seedfinder \
  --baseline FULL_BASELINE_SHA --candidate FULL_CANDIDATE_SHA \
  --query query.json --seeds 123,456,789 \
  --seed-count 100000 --warmups 2 --samples 10 > benchmark.json
macqueue validate benchmark.json
macqueue submit benchmark.json --key UNIQUE_COMPARISON_KEY
```

Reuse the same idempotency key only when retrying the same submission. A changed
job requires a new key. Keep seed inputs equal between variants. Three seeds are
useful for a smoke check; choose enough seeds and samples for actual measurements.
`--seed-count` controls the CLI benchmark and `--seeds` controls JSONL requests;
they are distinct workloads.

The plan builds both revisions offline for `aarch64-apple-darwin`, uses empty
`RUSTFLAGS`, freezes each variant, and benchmarks both executables. It resolves
worker counts to 1, the performance-core count, and the available-core count,
deduplicating equal values. AB/BA samples alternate, with one workload executing
at a time. Both persistent JSONL processes can exist while one is idle.

The JSONL adapter emits `{"ready":true,...}`. For an explicit readiness check,
add `"ready":{"field":"ready","equals":true}` to the JSONL compare step.
Requests are `{"seeds":[...]}`. Full responses, executable hashes, warmup/sample
labels, sample order and worker count are recorded. Compare correctness fields
(tested counts, matched seeds, recipes, witnesses) independently of timings.
Use the benchmark's internal elapsed search time; protocol wall time includes
extra overhead. Job success proves execution completed, not semantic equivalence
or a statistically meaningful speedup.

The VPS's `tooling/benchmarks/compare_native.py` cannot be uploaded as arbitrary
Python to the Mac. Use the structured compare steps. Any additional harness needs
separate approval of its exact file/hash and a corresponding local policy change.

## Correctness and results

```sh
macqueue plan correctness --project seedfinder --candidate FULL_SHA > checks.json
macqueue plan correctness --project seedfinder --candidate FULL_SHA \
  --test-filter module_name::test_name > focused.json
macqueue submit checks.json --key UNIQUE_CHECK_KEY
macqueue get JOB_ID
macqueue logs JOB_ID --follow
macqueue wait JOB_ID
macqueue download JOB_ID results.tar.gz
macqueue cancel JOB_ID
```

The full correctness plan runs fmt, Clippy with warnings denied, and workspace
release tests excluding GTK. The focused plan runs only core release tests with
the chosen filter. A filter matching zero tests is not evidence that a test passed.

`wait` exits nonzero for failure/cancellation/expiry/lost lease. `download` checks
SHA-256 and refuses existing destinations. Archives are not extracted automatically;
inspect/extract them into a new disposable directory. Artifacts include
`job.json`, `environment-before.json`, `environment-after.json` when execution
reaches completion, `worker-result.json`, logs, frozen binaries/manifests, and
`results/{seed-seeker,match_benchmark}.jsonl`. Failed jobs can also have artifacts.

For `lost`, inspect the reason and use a new submission key only when deliberately
retrying. The worker does not automatically replay jobs after a lease loss.
For a missing source/dependency, run `macqueue provision` with the required SHAs
and inspect its result. For permission or sandbox failures, preserve
the artifact and report the failing command to the Mac operator.

Initial capabilities are Cargo, benchmarks, and executable inspection. Profiling
and PGO remain disabled until their tools and local policy are provisioned. Leave
service configuration, worker credentials, protected-mirror updates and account
settings to the operator; submitter access does not require administrator actions.
