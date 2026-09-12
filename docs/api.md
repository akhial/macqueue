# HTTP API

All routes except `GET /healthz` require `Authorization: Bearer TOKEN`. Tokens are separate for submitters and workers. They are never accepted in URLs. The service is for a private network or authenticated HTTPS reverse proxy, not unauthenticated public hosting.

The server accepts JSON bodies of at most 1 MiB. Artifact bodies have a configurable limit, default 512 MiB. Requests must use `Content-Length`; chunked uploads are not supported. Errors are JSON: `{"error":"message"}`. Missing/bad authentication returns 401, unknown routes/jobs 404, validation or stale lease failures 400. Clients should retry transport/5xx failures, using the same submission key or lease. A 4xx failure needs attention rather than a blind retry.

## Submitter routes

| Method | Route | Purpose |
| --- | --- | --- |
| `POST` | `/v1/jobs` | Submit a [version-1 job](jobs.md); requires `Idempotency-Key` (1–128 letters, digits, `_`, `.`, `-`). Returns the job. |
| `GET` | `/v1/jobs` | Latest 100 job summaries. |
| `GET` | `/v1/jobs/ID` | Full job spec, status, result, timestamps, and artifact availability. Never includes the lease secret. |
| `POST` | `/v1/jobs/ID/cancel` | Cancel queued work or request cooperative cancellation of running work. |
| `GET` | `/v1/jobs/ID/logs?after=-1` | Up to 100 `{seq,text}` live-preview chunks after a sequence number. |
| `GET` | `/v1/jobs/ID/artifact` | Raw `.tar.gz` bytes with `X-Artifact-SHA256`. The server never extracts uploads. |

Example:

```sh
python3 -m macqueue submit benchmark.json --key experiment-001
```

`queued → running → succeeded | failed`. Other terminal states are `cancelled`, `expired` (queue deadline), and `lost` (worker lease expired). Running cancellation passes through `cancel_requested`. No terminal state is automatically requeued. Resubmitting the same key returns the original job, including terminal jobs; use a new key for an intentional rerun.

One shared submitter credential controls all jobs. This is a two-machine service, not a multi-tenant access-control system. Do not share that credential with unrelated tenants.

## Worker routes

| Method | Route | Body |
| --- | --- | --- |
| `POST` | `/v1/worker/claim` | `{"worker":"macbook-pro","projects":["seedfinder"]}`; long-polls up to 20 seconds; returns `null` or `{job,lease,lease_seconds}`. |
| `POST` | `/v1/worker/jobs/ID/heartbeat` | `{}`; returns `{cancel,lease_seconds}`. |
| `POST` | `/v1/worker/jobs/ID/logs` | `{"seq":0,"text":"..."}`; max 64 KiB text per chunk, 16 MiB per job. Duplicate identical sequences are accepted. |
| `PUT` | `/v1/worker/jobs/ID/artifact` | Raw archive, `Content-Length`, and `X-Artifact-SHA256`. A checksum mismatch or stale lease rejects the upload. |
| `POST` | `/v1/worker/jobs/ID/finish` | `{"status":"succeeded","result":{...}}`; allowed final states: succeeded, failed, cancelled. Success requires an uploaded artifact. |

All job-specific worker routes also require `X-Job-Lease: CLAIM_LEASE`. A lease is scoped to its job; the worker bearer token alone cannot mutate an active job. Completion is idempotent when the same lease and result are retried. Cancellation wins a race with successful completion.

SQLite transactions serialize claims. The service permits a single running job globally, matching a single benchmark Mac. Leases last 60 seconds and renew every 10 seconds. The worker uses an independent local watchdog with a conservative 40-second renewal deadline; it records renewal time before the HTTP request so a delayed reply cannot lengthen execution rights.

Artifact uploads are streamed to a temporary file, verified, fsynced, and atomically published while checking the active lease. Files from interrupted uploads are removed when the request unwinds. After abrupt VPS termination, an orphan `.upload` file can remain; remove such files only while the server is stopped. SQLite WAL recovery preserves queue state, but this protocol cannot guarantee exactly-once effects inside arbitrary built code. Lost jobs are deliberately not replayed.

