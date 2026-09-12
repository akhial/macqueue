// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vite-plus/test";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import App from "./App";
import type { Detail, Job } from "./model";

const job: Job = {
  id: "a".repeat(32),
  label: "Baseline comparison",
  project: "seedfinder",
  status: "lost",
  created: 100,
  started: 110,
  finished: 170,
  expires: 86400,
  worker: "macbook-pro",
  lease_until: 170,
  steps: 1,
  has_artifact: false,
  artifact_sha256: null,
};
const detail: Detail = {
  ...job,
  spec: {
    label: job.label,
    project: job.project,
    sources: { candidate: "b".repeat(40) },
    steps: [{ id: "build", op: "exec" }],
  },
  result: { error: "worker lease expired" },
};
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  history.replaceState(null, "", "/");
});
describe("dashboard", () => {
  it("filters jobs and opens failure, logs and spec without write requests", async () => {
    const fetcher = vi.fn(async (input: string) =>
      Response.json(
        input.includes("/logs")
          ? [
              {
                seq: 0,
                text: JSON.stringify({ stream: "stderr", text: "build interrupted" }) + "\n",
              },
            ]
          : input.endsWith(job.id)
            ? detail
            : [job, { ...job, id: "c".repeat(32), status: "succeeded", label: "Correctness" }],
      ),
    );
    vi.stubGlobal("fetch", fetcher);
    render(<App />);
    await screen.findByRole("button", { name: "Open Baseline comparison" });
    fireEvent.click(screen.getByRole("button", { name: "LOST" }));
    expect(screen.queryByRole("button", { name: "Open Correctness" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Open Baseline comparison" }));
    await screen.findByText("worker lease expired");
    fireEvent.click(screen.getByRole("tab", { name: "LOGS" }));
    await screen.findByText(/build interrupted/);
    fireEvent.click(screen.getByRole("tab", { name: "SPEC" }));
    expect(screen.getByText(/"candidate":/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /cancel job/i })).toBeNull();
    expect(fetcher.mock.calls.every(([path]) => path.startsWith("/api/jobs"))).toBe(true);
  });
  it("retains the last queue snapshot on disconnect and recovers on refresh", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(Response.json([job]))
      .mockResolvedValueOnce(Response.json({ error: "QUEUE UNREACHABLE" }, { status: 502 }))
      .mockResolvedValue(Response.json([job]));
    vi.stubGlobal("fetch", fetcher);
    render(<App />);
    await screen.findByRole("button", { name: "Open Baseline comparison" });
    fireEvent.click(screen.getByRole("button", { name: "Refresh jobs" }));
    await screen.findByText("QUEUE UNREACHABLE");
    expect(screen.getByRole("button", { name: "Open Baseline comparison" })).toBeTruthy();
    expect(screen.getByText(/STALE/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Refresh jobs" }));
    await waitFor(() => expect(screen.queryByText("QUEUE UNREACHABLE")).toBeNull());
  });
});
