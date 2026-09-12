# Macqueue dashboard

Local, read-only job monitor. React + TypeScript; Vite+ (`vp`); pnpm; Bun.

## Run

From this directory:

```sh
vp install          # uses pinned pnpm
pnpm build          # vp build
pnpm start          # Bun, http://127.0.0.1:8790
```

The local submit credential is already configured on this Mac. Queue access needs
Tailscale connectivity to devbox. Stop with Ctrl+C. Nothing starts at login.

For development:

```sh
pnpm dev            # Bun API :8790 + vp dev :8791
```

Or run `pnpm api` and `vp dev` in separate terminals. Both bind to loopback.
`DASHBOARD_PORT` overrides the production port only.

## Views

- Latest 100 jobs from the existing API; counts cover that window.
- Active-first or newest-first order, status filters, label/ID/project/worker search.
- Refresh every five seconds; pause refresh or refresh manually.
- Last snapshot remains visible during disconnects, marked stale.
- Job details: sources, timings, step progress, errors, result, raw spec.
- Incremental log preview, search, follow toggle, copy. Live previews can omit
  events; sequential step progress uses later starts to confirm prior completion.
- Browser artifact downloads and copyable SHA-256. Downloaded bytes are not
  automatically verified; use the supplied hash when verifying locally.
- `/` focuses search; Escape closes details; selected job ID lives in the URL hash.

The API connection indicator describes the queue connection, not Mac availability.
The API has no independent worker heartbeat/status endpoint. Spec/result previews
are capped at 128 Ki characters; Copy retains full JSON. Log tails are capped at
512 Ki characters; artifacts contain full command logs. Log previews load at most
three API pages per refresh, then continue from the last sequence number.

## Local configuration

Bun reads `.env.local` if present. Example keys are in `.env.example`.
The default upstream is `http://100.102.112.115:8787` over Tailscale.

Credential path: `.local/submit.token`, regular file, mode 0600. It is read only by
Bun, never sent to browser code or stored in browser storage. `.local/`, `.env.local`,
credentials and build output are Git-ignored. Fonts are bundled locally.

For another checkout, copy the submit token from the existing VPS account through
SSH into that file, without printing it. Do not use the worker token. See
[agent operations](../docs/agent-operations.md) for credential location and roles.

The existing VPS has a submitter role, not a separate read-only role. The local
Bun gateway enforces read-only access: fixed GET routes for list/detail/log/artifact;
no forwarding of browser-supplied credentials, upstream URLs, writes or worker
routes. It rejects foreign hosts/origins and cross-site API requests. Queue state,
service settings, worker permissions and source provisioning are managed separately.

## Checks

```sh
vp check            # format, lint, type-check
vp test run         # gateway, state handling, dashboard interactions
vp build
```

Tests use fixtures and a fake upstream. They submit no jobs. UI was also checked
against the live API, on desktop and mobile layouts.

## Remove

Stop the process and remove `frontend/` (including its ignored `.local/` credential,
`node_modules/` and `dist/`). There is no daemon, account, port exposure or VPS
installation to undo. The queue and worker remain separate.

Toolchain references: [Vite+](https://viteplus.dev/guide/),
[Bun HTTP server](https://bun.sh/docs/runtime/http/server).
