# Pair credentials and provision offline source snapshots

This procedure keeps the hidden `_macqueue` account and LaunchDaemon intact.
It fetches dependencies as the Mac operator into a fresh private staging directory,
then installs a reviewed snapshot with administrator authentication. Cargo fetch
does not build the project; all actual job execution stays in the sandboxed worker.

On the VPS, export exact committed revisions with `macqueue export-source`; see
[agent operations](agent-operations.md). The active worktree is never altered by
export. On the Mac, from this Macqueue repository:

```sh
umask 077
MACQUEUE_STAGE="$PWD/.state/mac-provision-$(date +%s)"
mkdir -p "$MACQUEUE_STAGE"
ssh adel@devbox.komodo-spectrum.ts.net \
  'sudo -n /bin/cat /etc/macqueue/worker.token' > "$MACQUEUE_STAGE/worker.token"
scp adel@devbox.komodo-spectrum.ts.net:/ABSOLUTE/PATH/TO/source.bundle \
  "$MACQUEUE_STAGE/source.bundle"
python3 deploy/macos/provision.py prepare \
  --stage "$MACQUEUE_STAGE" \
  --revision FULL_BASELINE_SHA --revision FULL_CANDIDATE_SHA \
  --toolchain /Users/adel/.rustup/toolchains/stable-aarch64-apple-darwin \
  --url http://100.102.112.115:8787 --resume
```

Substitute the operator's real pinned toolchain path; preparation records its Rust
version and installation requires it to match the copied worker toolchain.
Preparation fetches the locked dependencies for all targets so host tests and
Clippy also resolve offline. It verifies the lockfile is unchanged, checks offline
metadata resolution, rejects symlinks and Cargo credentials/configuration in the
cache, and pins hashes of the source mirror, cache, credential and provisioner.
Repository `.cargo` configuration requires separate review. No personal Cargo
cache, credentials or shell environment is copied to the worker.

Pause the worker and allow active work to finish before installing another snapshot:

```sh
sudo -u _macqueue touch /Library/Macqueue/state/PAUSED
sudo /usr/bin/python3 deploy/macos/provision.py apply --plan "$MACQUEUE_STAGE/plan.json"
```

The initial installation is already paused. Apply refuses an `ACTIVE.json` marker.
It backs up the previous worker config/token, mirror and cache under the root-only
`/Library/Macqueue/provisioning/<id>` directory. It installs the mirror as root-owned
and read-only to the worker, gives the cache and 0600 token to `_macqueue`, runs
doctor, and restarts while paused. `--resume` in the prepared plan then enables
claims. Omit it to leave the worker paused for further inspection.

Run the printed apply command in a locally authorized terminal. Never send the
administrator password in chat. If the automation tool cannot open the terminal,
run the prepared command yourself; no privacy setting or security bypass is needed.

Verify a real offline build/benchmark through the VPS API before reporting the
worker ready. Check artifacts and the resolved hardware counts. The provisioning
receipt alone proves installation and sandbox checks, not successful project builds.

## Undo just the provisioning

Apply prints the exact rollback command:

```sh
sudo /usr/bin/python3 /Library/Macqueue/provision.py rollback \
  --receipt /Library/Macqueue/provisioning/INSTALLATION_ID/receipt.json
```

This pauses new claims, refuses an active job or changed configuration, restores
the previous source mirror/cache/config/token, and restarts paused. The replaced
snapshot is retained privately beside the receipt, including cache updates from
jobs. Job records/artifacts remain in the normal worker state directory. The
account remains hidden and unable to log in.

The original whole-install rollback still removes the entire Macqueue installation,
including provisioning backups. Staging files under this checkout's ignored
`.state` directory are separate operator files; remove the exact staging directory
after verification if it is no longer needed. It contains a worker token and must
remain private. Do not commit `.state`, token files, private bundles or job artifacts.

## Updating installed worker code

The service runs a root-owned application snapshot, so pulling this repository
does not change the running worker. `deploy/macos/update_module.py` can install a
single reviewed Python module with required before/after SHA-256 values. It pauses
new claims, refuses an active job, saves the old module and a rollback receipt,
restarts the worker, and restores its previous pause state. Apply prints a
standalone rollback command stored under `/Library/Macqueue/updates/<id>`.

The root-owned mirror needs special handling: Git documents that cloning from
[another owner's repository requires `--no-local`](https://git-scm.com/docs/git-clone).
The worker uses that mode with a per-job Git configuration naming only the locally
allowlisted mirror as safe. That configuration is used only during cloning; it
does not change account or system Git settings, or give jobs write access to the
mirror. Ordinary job Git commands continue to use the empty global configuration.
