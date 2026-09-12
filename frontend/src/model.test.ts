import { describe, expect, it } from "vite-plus/test";
import { appendLogs, duration, elapsed, emptyLog, filterJobs } from "./model";
import type { Job } from "./model";

describe("job monitoring", () => {
  it("does not show two active steps when a finish preview was dropped", () => {
    const state = appendLogs(emptyLog(), [
      { seq: 0, text: JSON.stringify({ event: "step_started", step: "first" }) + "\n" },
      { seq: 1, text: JSON.stringify({ event: "step_started", step: "second" }) + "\n" },
    ]);
    expect(state.steps).toEqual({ first: "done", second: "running" });
  });
  it("distinguishes running, stopping and attention states", () => {
    const jobs = [
      "running",
      "cancel_requested",
      "failed",
      "lost",
      "expired",
      "succeeded",
      "queued",
      "cancelled",
    ].map((status) => ({ status, id: status, label: "Candidate" }) as Job);
    expect(filterJobs(jobs, "running", "").map((job) => job.status)).toEqual([
      "running",
      "cancel_requested",
    ]);
    expect(filterJobs(jobs, "attention", "candidate").map((job) => job.status)).toEqual([
      "failed",
      "lost",
      "expired",
    ]);
    expect(filterJobs(jobs, "all", "missing")).toHaveLength(0);
  });
  it("freezes elapsed time for terminal jobs; queued work has no runtime", () => {
    expect(elapsed({ started: 100, finished: 140 } as Job, 999)).toBe(40);
    expect(elapsed({ started: null } as Job, 999)).toBeNull();
    expect(duration(-10)).toBe("0s");
    expect(duration(3661)).toBe("1h 01m");
  });
  it("deduplicates cursor retries and retains step state when preview is trimmed", () => {
    const chunks = [
      { seq: 0, text: JSON.stringify({ event: "step_started", step: "build" }) + "\n" },
      { seq: 1, text: JSON.stringify({ event: "step_finished", step: "build" }) + "\n" },
    ];
    const state = appendLogs(emptyLog(), chunks);
    expect(appendLogs(state, chunks)).toEqual(state);
    const clipped = appendLogs(state, [{ seq: 2, text: "x".repeat(600_000) + "\n" }]);
    expect(clipped.clipped).toBe(true);
    expect(clipped.text.length).toBeLessThanOrEqual(512 * 1024);
    expect(clipped.steps.build).toBe("done");
  });
});
