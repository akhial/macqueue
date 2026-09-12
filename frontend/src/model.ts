export const statuses = [
  "running",
  "queued",
  "succeeded",
  "failed",
  "lost",
  "cancel_requested",
  "cancelled",
  "expired",
] as const;
export type Status = (typeof statuses)[number];
export type Filter = Status | "all" | "attention";
export interface Job {
  id: string;
  status: Status;
  label?: string;
  project: string;
  steps: number;
  created: number;
  expires: number;
  started: number | null;
  finished: number | null;
  worker: string | null;
  lease_until: number | null;
  has_artifact: boolean;
  artifact_sha256: string | null;
}
export interface Step {
  id: string;
  op: string;
  command?: { argv: string[] };
}
export interface Detail extends Job {
  spec: {
    label?: string;
    project: string;
    sources: Record<string, string>;
    steps: Step[];
    timeout_seconds?: number;
    expires_in_seconds?: number;
  };
  result: Record<string, unknown> | null;
}
export interface LogChunk {
  seq: number;
  text: string;
}
export interface LogState {
  cursor: number;
  text: string;
  steps: Record<string, "running" | "done">;
  clipped: boolean;
}
export const emptyLog = (): LogState => ({ cursor: -1, text: "", steps: {}, clipped: false });
export const active = (status: Status) => status === "running" || status === "cancel_requested";
export const attention = (status: Status) => ["failed", "lost", "expired"].includes(status);
export const statusLabel = (status: Status) =>
  status === "cancel_requested" ? "STOPPING" : status.toUpperCase();
export function duration(seconds: number | null) {
  if (seconds === null) return "—";
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
}
export function elapsed(job: Job, now: number) {
  return job.started === null ? null : (job.finished ?? now) - job.started;
}
export function filterJobs(jobs: Job[], filter: Filter, search: string) {
  const query = search.trim().toLowerCase();
  return jobs.filter(
    (job) =>
      (filter === "all" ||
        (filter === "attention"
          ? attention(job.status)
          : filter === "running"
            ? active(job.status)
            : job.status === filter)) &&
      [job.label, job.id, job.project, job.worker, job.status].some((value) =>
        value?.toLowerCase().includes(query),
      ),
  );
}
export function appendLogs(previous: LogState, chunks: LogChunk[]): LogState {
  let text = previous.text;
  const steps = { ...previous.steps };
  let cursor = previous.cursor;
  for (const chunk of chunks) {
    if (chunk.seq <= cursor) continue;
    cursor = chunk.seq;
    for (const line of chunk.text.split("\n")) {
      if (!line) continue;
      try {
        const event = JSON.parse(line);
        const stamp =
          typeof event.at === "number"
            ? new Date(event.at * 1000).toLocaleTimeString("en-GB", { hour12: false })
            : "—";
        if (
          typeof event.step === "string" &&
          ["step_started", "step_finished"].includes(event.event)
        ) {
          // Steps run sequentially. A later start confirms earlier execution finished,
          // even when the bounded live-event queue dropped a finish event.
          if (event.event === "step_started") {
            for (const key of Object.keys(steps)) if (steps[key] === "running") steps[key] = "done";
          }
          steps[event.step] = event.event === "step_finished" ? "done" : "running";
        }
        const message = event.stream
          ? `[${event.stream}] ${String(event.text ?? "")}`
          : `${event.event ?? "event"} ${event.step ?? (event.argv ? event.argv.join(" ") : "")}`;
        text += `${stamp} ${message}${message.endsWith("\n") ? "" : "\n"}`;
      } catch {
        text += line + "\n";
      }
    }
  }
  const limit = 512 * 1024;
  const clipped = previous.clipped || text.length > limit;
  if (text.length > limit) {
    const start = text.indexOf("\n", text.length - limit);
    text = text.slice(start < 0 ? -limit : start + 1);
  }
  return { cursor, text, steps, clipped };
}
