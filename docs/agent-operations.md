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

The project name is `seedfinder`. Jobs accept full 40-character **commit** SHAs
already provisioned into the Mac's local mirror. They do not include uncommitted
worktree edits. The optimization worktree is
`/home/adel/.t3/worktrees/shpd-seed-seeker/t3code-b11a8beb` and shares Git objects
with `/home/adel/code/shpd-seed-seeker`.

```sh
macqueue source-status
```

Use the agent's existing review/commit workflow to commit a candidate. Do not
reset, stash, or change other worktrees just to submit a job. To request provisioning
of a new candidate, export a standalone bundle containing baseline and candidate:

```sh
macqueue export-source --baseline FULL_BASELINE_SHA --candidate FULL_CANDIDATE_SHA \
  --output /home/adel/code/macqueue/.state/seedfinder-candidate.bundle
```

Existing output files are never overwritten; use a new filename for another
export. Notify the Mac operator of both full SHAs and the bundle path. For this
setup handoff, those values can also be recorded in
`/home/adel/code/macqueue/.state/candidate-request.json` as
`{"baseline":"...","candidate":"...","bundle":"/absolute/path.bundle"}`.
This file is an operator handoff, not an automatically consumed queue endpoint.
Wait for provisioning confirmation before submitting a new revision. No job
fetches source or dependencies, and no VPS-to-Mac SSH access is required.

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
For a missing source/dependency, request provisioning rather than adding network
access or shell commands to the job. For permission or sandbox failures, preserve
the artifact and report the failing command to the Mac operator.

Initial capabilities are Cargo, benchmarks, and executable inspection. Profiling
and PGO remain disabled until their tools and local policy are provisioned. Leave
service configuration, worker credentials, source-mirror updates and account
settings to the operator; submitter access does not require administrator actions.
