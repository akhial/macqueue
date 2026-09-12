# Hidden macOS service account

`deploy/macos/install.py` installs a system LaunchDaemon as `_macqueue`. It does not require a graphical login session.

The new account has an unused UID/GID between 450 and 499, `IsHidden=1`, `/usr/bin/false` as its shell, no password hash, a disabled authentication authority, and a per-account `FALSEPREDICATE` authentication policy. Installation verifies that authentication is denied, the account is not an administrator, no GUI domain exists, and no FileVault secure token was granted. Hiding is separate from disabling login; [Apple documents the IsHidden attribute](https://support.apple.com/en-us/102099). The authentication policy uses the local `pwpolicy(8)` interface and Apple's [always-false predicate](https://developer.apple.com/library/archive/documentation/Cocoa/Conceptual/Predicates/Articles/pSyntax.html).

The LaunchDaemon is in the system launchd domain with `UserName`/`GroupName` set to `_macqueue` and `SessionCreate=false`. The worker process runs as that non-administrator user. It has no Finder, Dock, user LaunchAgent, or GUI session. Cargo and the benchmark executables run headlessly. Instruments operations that require GUI permissions remain disabled in this initial setup.

## What is installed

- `/Library/LaunchDaemons/local.macqueue.worker.plist` — root-owned service definition.
- `/Library/Macqueue/app/` — root-owned snapshot of the reviewed worker package.
- `/Library/Macqueue/toolchain/rust/` — independent copy of the existing Rust toolchain; the original is untouched.
- `/Library/Macqueue/config/worker.json` — root-owned configuration, readable by the service group.
- `/Library/Macqueue/config/worker.token` — a newly generated worker credential, mode 0600, readable only by the worker and root.
- `/Library/Macqueue/home/`, `state/`, `cargo/`, `logs/` — dedicated writable locations.
- `/Library/Macqueue/mirrors/seedfinder.git` — initially empty, root-owned repository mirror.
- `/Library/Macqueue/diagnostics/` — results from real sandbox, Cargo, benchmark, and account checks.
- `/Library/Macqueue/installation.json` and `rollback.py` — installation receipt and rollback tool.

The daemon uses the existing Homebrew Python executable by its resolved versioned path. Homebrew and your personal home permissions are unchanged. The installed worker starts **paused** with a reserved `.invalid` server address. It makes no queue requests until configured and resumed. The installer tests a generated dependency-free Rust fixture rather than a private project, and cleans up the fixture after verification.

There are no changes to existing accounts, loginwindow preferences, FileVault settings, SSH configuration, network policy, or the original Python/Rust installations. A FileVault-encrypted Mac still requires its normal disk-unlock process after restart; the service account is not an unlock user.

## Install

Generate a reviewable plan as your normal user, using the real installed Python and Rust paths:

```sh
python3 deploy/macos/install.py plan \
  --python /ABSOLUTE/PATH/TO/python3 \
  --toolchain /ABSOLUTE/PATH/TO/RUST_TOOLCHAIN > macos-install-plan.json
sudo /usr/bin/python3 deploy/macos/install.py apply --plan macos-install-plan.json
```

The installer is compatible with Apple's Python 3.9; the worker itself requires Python 3.11+. The plan pins source hashes, UUIDs, and the selected UID/GID. The installer rechecks that no account, directory, or service already exists and refuses to overwrite them. A failure attempts rollback of newly created resources and reports any OS denial. Administrator authentication belongs in your local terminal, never in chat.

macOS can deny Directory Services policy/deletion operations from an automation helper even after administrator authentication. Run the installer in a locally authorized Terminal session. Do not disable SIP or directly edit the directory-service database. If a partial installation remains, `recover --plan ORIGINAL_PLAN.json` verifies its installation ID, rolls it back first, regenerates a current plan, and retries setup. Recovery refuses to remove a completed installation or one with an active job.

## Verify and operate

```sh
sudo /usr/bin/python3 /Library/Macqueue/rollback.py verify
sudo launchctl print system/local.macqueue.worker
sudo cat /Library/Macqueue/diagnostics/installation.json
sudo tail -n 40 /Library/Macqueue/logs/worker.stderr.log
```

Before accepting work, provision the real source mirror and dedicated offline Cargo cache, replace `server_url` in `config/worker.json`, and securely give the VPS the generated worker token (or replace it with the VPS worker token). Preserve token ownership `_macqueue:_macqueue` and mode 0600. Do not put a submitter credential on the Mac.

Use the [reversible provisioning procedure](macos-provisioning.md) to stage exact
revisions and locked dependencies, pair the VPS credential, and retain the previous
mirror/cache/configuration for rollback.

Restart after changing configuration:

```sh
sudo launchctl kickstart -k system/local.macqueue.worker
```

Use that command only while the worker is paused and has no active job. For an active job, cancel it through the queue and wait for completion before restarting. The service's process restart guard deliberately blocks new work after an unclean active-job exit.

Resume or pause by removing/creating the dedicated pause file:

```sh
# Resume only after source, cache, server, and authentication are ready.
sudo rm /Library/Macqueue/state/PAUSED
# Pause new claims; existing work finishes.
sudo -u _macqueue /usr/bin/touch /Library/Macqueue/state/PAUSED
```

## Roll back

This stops and unloads the service, waits for its processes to exit, removes only the matching new account/group, and retains all installation data in a root-only archive:

```sh
sudo /usr/bin/python3 /Library/Macqueue/rollback.py rollback
```

To return to the pre-installation state without retaining the newly created files:

```sh
sudo /usr/bin/python3 /Library/Macqueue/rollback.py rollback --purge
```

Rollback checks the installation ID, account/group UUIDs, and service-file hash before removing anything. It refuses to delete an account that has been replaced or still has running processes. It does not send signals to old recorded PIDs. No original files need restoring because installation never overwrites them. macOS may retain ordinary audit/log records of account creation, execution, and removal.
