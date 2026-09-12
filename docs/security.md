# Execution boundary and operational limits

The worker authorizes operations locally, independently of the VPS. An agent supplies argv and typed job data, but can select only approved Cargo shapes, job-owned benchmark binaries, approved inspector options, and explicit artifact operations. Shells and interpreters are not accepted. The Python benchmark coordinator is part of the installed, reviewed worker package; no remote Python file is accepted. Its source-file hashes are included in environment artifacts. Keep that installation and the worker configuration separate from job checkouts and deploy reviewed versions.

Build scripts, procedural macros, tests, and benchmark binaries execute repository code. A matching command name does not make this code trustworthy. Use a dedicated non-administrator account for reviewed work. For unreviewed code, use a disposable macOS VM with the worker inside, no host home shares, no host credentials, and restricted network access. Reset the VM/cache between trust domains. The worker cannot verify that a machine is a VM or provision one for you.

## What is enforced

- The Mac has no listening job service and uses only outbound queue requests.
- Distinct worker/submitter tokens, full commit SHAs, local project mapping, and validation before execution.
- No inherited login environment, SSH keys, server tokens, Cargo credentials, or arbitrary command paths in the job environment.
- Jobs cannot request `RUSTC_WRAPPER`, arbitrary Rust flags, alternate Cargo configurations, repository URLs, or process IDs.
- Each command enters the macOS sandbox. Its allowed writes are the job work directory, selected command outputs, and dedicated Cargo cache. Frozen directories have an explicit write denial.
- The sandbox denies network operations. SDK/toolchain/system reads are allowed; private account files and control files are outside read roots. Additional read roots are local configuration, not request data.
- Process termination targets only process groups created by the runner; `sample` targets only the PID spawned for its own profile step.
- File operations reject path traversal, symlinks, special files, and overwrites. The server stores archives as opaque bytes and never extracts them.
- A local worker lock and server-side serialized claim prevent ordinary concurrent job execution. Deadlines, lease loss, cancellation, and output/disk limits stop active subprocesses.

The startup `doctor` performs real positive and negative sandbox probes; unsupported profiles or missing tool access fail startup. Apple's `sandbox-exec` interface is [deprecated](https://developer.apple.com/forums/thread/661939). This project uses it as an additional boundary and does not claim it is a substitute for disposable execution isolation. It permits process creation, process inspection, and Mach service lookup needed by developer tools; that is not a hardened general-purpose hostile-code sandbox.

## Limits to plan for

- A compromised OS or sandbox escape defeats a same-account worker. A program may also try to daemonize or leave its process group. Process-group cleanup is not a VM reset. Do not treat it as containment of every hostile process.
- A writable Cargo cache persists across jobs and can be poisoned. It must contain no secrets. Provision/reset it separately and do not mix unrelated trust domains.
- Job-owned target files and explicit output paths are mutable while that command runs. The worker freezes executable copies after the build finishes and records hashes. The local machine owner can still change file modes or files; “frozen” means immutable to the job policy and sandbox, not tamper-proof against the owner.
- One workload is coordinated at a time, but the worker does not pin cores, stop unrelated Mac applications, control thermal state, or prove that a malicious benchmark remains idle between requests. Record and control those conditions for meaningful performance measurements.
- Disk checks are periodic, not hard quotas. A fast writer can overshoot. Use a quota-limited volume or disposable VM disk for a hard storage boundary. CPU/RAM isolation also belongs to the execution environment.
- Only JSON syntax and seed request structure are known here; query validity, response semantics, match equivalence, and meaningful training data belong to the seedfinder project. Complete raw records are retained for analysis.
- Optional Instruments profiling may need local Developer Tools permissions and may be denied by the sandbox. The worker reports failures without widening permissions. No real Instruments trace has been validated by the fixture suite.
- No automatic code approval UI, repository patch upload, network provisioning, live interactive stdin API, source fetch, submodule/LFS downloader, VM manager, web dashboard, or MCP server is included. The CLI and HTTP API cover the requested asynchronous workflow.
- The server uses one submitter role and one worker role; credentials are not tenant-specific. Anyone with the submitter token can inspect/cancel all jobs. Protect both tokens and restrict API network access.
- TLS termination is external; the built-in server supports loopback/private HTTP. Set up HTTPS or Tailscale before moving credentials across machines.
- A worker crash or laptop sleep can leave partial results. Expired leases become `lost`, with no automatic replay. The Mac retains undelivered receipts and artifacts for operator recovery. A successful API receipt is not a distributed exactly-once guarantee for side effects in executed code.
- An unclean local exit leaves `ACTIVE.json` and blocks new execution after restart. Inspect/reset the environment before removing it. This prevents a restarted worker from silently overlapping an orphaned workload, without risking termination of reused process IDs.

Repository configuration and build scripts are intentionally not scanned as a substitute for isolation. Explicit argv validation constrains the API; the OS/VM boundary constrains the code those commands build and run.
