# Structured job format

`macqueue plan` generates complete jobs. All fields are validated again on the Mac. Unknown keys and operations are rejected. This is not a generic command-execution API.

```json
{
  "version": 1,
  "project": "seedfinder",
  "label": "focused core test",
  "sources": {"candidate": "1111111111111111111111111111111111111111"},
  "timeout_seconds": 3600,
  "expires_in_seconds": 86400,
  "steps": [
    {
      "id": "test",
      "op": "exec",
      "command": {
        "argv": ["cargo", "test", "--locked", "--offline", "-p", "shpd-seedfinder-core", "--release", "module::test_name"],
        "cwd": "work/checkouts/candidate",
        "env": {"CARGO_TARGET_DIR": "${JOB}/work/targets/candidate", "RUSTFLAGS": ""},
        "stdin": "",
        "timeout_seconds": 1800,
        "stdout": "artifacts/logs/test.stdout",
        "stderr": "artifacts/logs/test.stderr"
      }
    }
  ]
}
```

Replace the illustrative SHA with a real 40-character commit SHA. Source names create `work/checkouts/NAME`. The Mac maps `project` to a local mirror; the caller cannot provide repository URLs or host filesystem paths.

`cwd` and output paths are relative to the job root. They cannot be absolute, contain `..` or empty components, or traverse symlinks. All outputs must have distinct, unused paths. `${JOB}` is the only path substitution, and `${WORKERS}` is supported in `compare` argv. There is no shell expansion, glob expansion, environment inheritance, or command substitution. The PGO merge operation expands its own narrowly bounded `.profraw` glob locally.

## Approved command shapes

Cargo accepts exactly these argv sequences (argument order matters):

```sh
cargo build --locked --offline --release \
  --target aarch64-apple-darwin \
  -p shpd-seedfinder-cli -p shpd-seedfinder-ffi \
  --bin seed-seeker --example match_benchmark

cargo fmt --all -- --check

cargo clippy --locked --offline --workspace \
  --exclude shpd-seedfinder-gtk --all-targets -- -D warnings

cargo test --locked --offline --workspace \
  --exclude shpd-seedfinder-gtk --release

cargo test --locked --offline -p shpd-seedfinder-core --release TEST_FILTER
```

The focused filter is optional, contains only letters, digits, `_`, `:`, `.`, `-`, and cannot begin with `-`. Cargo steps require an explicit job-owned target directory and explicit `RUSTFLAGS`. Ordinary builds use `"RUSTFLAGS":""`; optimization settings stay in the repository's profiles.

Only `CARGO_TARGET_DIR`, `RUSTFLAGS`, and `LLVM_PROFILE_FILE` are accepted as caller-provided environment keys. The Mac constructs HOME, TMPDIR, PATH, offline Cargo settings, pinned compiler/linker/SDK paths, and an isolated Cargo home locally. Repository code can run within the sandbox, but cannot override the worker's environment by submitting additional env keys.

Frozen executables accept:

```text
${JOB}/artifacts/frozen/VARIANT/seed-seeker --benchmark SEED_COUNT --workers WORKERS
${JOB}/artifacts/frozen/VARIANT/match_benchmark QUERY_JSON WORKERS
${JOB}/artifacts/frozen/VARIANT/equivalence
```

`equivalence` is available only with the local profiling capability and uses its default invocation. The query argument is a JSON object; its domain schema belongs to the benchmark. `match_benchmark` stdin contains JSON-lines seed requests, each with at most 1,024 unsigned 64-bit seeds. The generic exec operation sends stdin and closes it, preserving all output.

Set `"wrappers":["time","caffeinate"]` in a command to wrap it with `/usr/bin/time -l` and `caffeinate -i`, in the listed outer-to-inner order. Their output is captured with the command. The worker's timeout and process-group cleanup apply to wrappers too.

## Benchmark comparison

A `compare` step has `mode` (`process` or `jsonl`), two complete command objects under `commands.baseline` and `commands.candidate`, `worker_counts`, `warmups`, `samples`, `requests`, and `output`. Both commands must use frozen baseline/candidate binaries and include `${WORKERS}` as a separate argv element. Their `stdin` must be empty; the coordinator supplies requests.

Use `"worker_counts":[1,"performance","available"]`. Counts are resolved with `hw.perflevel0.physicalcpu` and `hw.activecpu`, deduplicated, and recorded. Missing requested hardware information fails the comparison rather than silently choosing a different count. Explicit integer counts are also supported.

`process` mode runs a fresh `seed-seeker` per warmup/sample. `jsonl` mode starts one `match_benchmark` process per variant per worker count, consumes each readiness record, and keeps the sessions alive through warmups and samples. Both processes may be alive but only one request is outstanding. After every request completes, execution moves to the other variant. Sample pairs alternate AB, BA, AB, BA. Requests are replayed identically to both variants, and warmup records remain marked separately.

For strict readiness validation, add:

```json
"ready": {"field": "event", "equals": "ready"}
```

Set the field/value to match your executable. Without it, the first complete JSON object is accepted and retained as the readiness record. Each request must produce **one** complete JSON object followed by a newline. Diagnostic text belongs on stderr. Extra unsolicited stdout records, incomplete JSON, a failed exit, a timeout, or an output limit fail the job.

Results preserve the executable's complete response: elapsed search time, tested counts, matches, recipes, witnesses, and any additional fields. `wall_ns` is separately measured by the runner and includes stdin/stdout protocol overhead; it is not substituted for the executable's search time. The runner does not invent a domain-specific correctness comparator or timing field. Each sample includes variant identity, worker count, phase, order, and request. Process-mode output is in the referenced stdout/stderr artifacts.

## Files and metadata

| Operation | Additional fields | Behavior |
| --- | --- | --- |
| `metadata` | none | Record toolchain, architecture, OS, hardware, worker source hashes, source commits/status/diffs, and frozen hashes. Automatically runs before and after successful jobs too. |
| `mkdir` | `path` | Create a directory under work or ordinary artifacts; cannot create frozen directories. |
| `write` | `path`, `text` | Create an artifact text file, at most 64 KiB. |
| `copy` | `source`, `destination` | Copy regular files/directories from this job into ordinary artifacts. Reject symlinks and overwrites. |
| `freeze` | `variant`, `target_dir`, `profile` | Copy both benchmark binaries from the target tree to a new frozen variant. Profiling also copies `equivalence`; preserve adjacent `.dSYM`, `.dwp`, `.pdb` symbols. Set binaries/directories 0555 and other files 0444. |

Example freeze step:

```json
{"id":"freeze-baseline","op":"freeze","variant":"baseline","target_dir":"work/targets/baseline","profile":"release"}
```

Metadata executes `rustc -Vv`, `cargo --version`, `uname -m`, `sw_vers`, the requested hardware `sysctl` values, `git rev-parse HEAD`, `git status --porcelain`, and `git diff --binary`. Git external diff/textconv hooks are disabled. SHA-256 is computed in the worker without invoking a shell. Each frozen directory and the archive have manifests. Git diff output includes binary patches for tracked files; `git status` also identifies untracked files but their contents are not automatically copied.

If a profiling build uses unpacked DWARF and has no adjacent `.dSYM`, the worker runs the locally resolved `dsymutil` against the job-owned executable before freezing, then copies the resulting standalone symbol bundle alongside it. This internal operation is fixed; agents cannot pass arbitrary dsymutil arguments.

Reserved worker records cannot be caller output destinations. Executed code can write work, the dedicated Cargo cache, and its explicitly selected output paths; metadata and frozen artifacts are not writable by sandboxed commands. File modes alone are not relied on to protect frozen files.

## Optional profiling

Add `profiling` to the Mac's local `capabilities`. This build shape is then allowed:

```sh
cargo build --locked --offline --profile profiling \
  --target aarch64-apple-darwin \
  -p shpd-seedfinder-cli -p shpd-seedfinder-ffi -p shpd-seedfinder-core \
  --bin seed-seeker --example match_benchmark --example equivalence
```

`exec` also accepts `xcrun xctrace list templates` and these export shapes:

```text
xcrun xctrace export --input ${JOB}/artifacts/profile.trace --output ${JOB}/artifacts/toc.xml --toc
xcrun xctrace export --input ${JOB}/artifacts/profile.trace --output ${JOB}/artifacts/data.xml --xpath XPATH
```

For recording, use a `profile` step with a complete approved binary command:

```json
{
  "id": "trace", "op": "profile", "tool": "xctrace",
  "duration_seconds": 10, "template": "Time Profiler",
  "output": "artifacts/profile.trace",
  "command": {
    "argv": ["${JOB}/artifacts/frozen/candidate/seed-seeker", "--benchmark", "10000000", "--workers", "1"],
    "cwd": "work/checkouts/candidate", "env": {}, "stdin": "",
    "timeout_seconds": 60,
    "stdout": "artifacts/logs/profile.stdout", "stderr": "artifacts/logs/profile.stderr"
  }
}
```

The worker constructs `xcrun xctrace record … --launch -- EXECUTABLE ARGS`. Allowed templates are `Time Profiler` and `CPU Profiler`; durations are 1–600 seconds. `tool: "sample"` instead starts the approved binary and samples **that newly created PID**, never an agent-supplied PID. Choose a workload that lasts through the sampling interval. Profile command stdin must be empty and wrappers are disallowed. For training/interactive stdin use ordinary exec or compare, not a profile step.

Instruments availability, Developer Tools permissions, and sandbox support vary by macOS/Xcode installation. Profiling fails closed if the local environment denies it; the worker does not grant permissions or relax isolation automatically. Validate it locally in the intended VM/account. Recording real traces is not part of the fixture integration test.

The `inspect` capability permits `llvm-objdump`, `nm`, and `size`, ending in a frozen executable path. Approved flags:

- `llvm-objdump`: `--disassemble`, `--demangle`, `--section-headers`, `--syms`, `--macho`, `--full-contents`.
- `nm`: `-n`, `-m`, `-C`, `-g`.
- `size`: `-m`, `-l`.

These commands require empty env/stdin. `llvm-objdump` resolves inside the configured Rust toolchain.

## Optional PGO

Add `pgo` to local capabilities and provision `llvm-tools-preview` in the exact Rust toolchain used to compile. Accepted nonempty `RUSTFLAGS` are exactly:

```text
-Cprofile-generate=${JOB}/work/pgo/raw
-Cprofile-use=${JOB}/work/pgo/merged.profdata
```

Generation also requires `LLVM_PROFILE_FILE=${JOB}/work/pgo/raw/%m-%p.profraw`. Supply the same LLVM variable when running instrumented training binaries. Let them exit cleanly so LLVM can flush profiles. The worker closes persistent session stdin and waits for successful termination at the end of comparison.

```json
{"id":"merge","op":"pgo_merge","raw_dir":"work/pgo/raw","output":"work/pgo/merged.profdata"}
```

This uses `RUST_TOOLCHAIN/lib/rustlib/aarch64-apple-darwin/bin/llvm-profdata merge -o …` over 1–4,096 job-owned `.profraw` files. It records the LLVM tool version. It never resolves a random system LLVM installation. Use separate target directories for ordinary, training, and PGO builds; the generated plan does this. Copy raw/merged profiles to ordinary artifacts if you want them uploaded; the generated PGO plan preserves the merged profile.
