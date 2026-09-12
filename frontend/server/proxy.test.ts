import { describe, expect, it, vi } from "vite-plus/test";
import { createReadOnlyProxy } from "./proxy.ts";

const origin = "http://127.0.0.1:8790";
const id = "a".repeat(32);
function setup(response = () => Response.json([])) {
  const fetcher = vi.fn(async () => response());
  const proxy = createReadOnlyProxy({
    upstream: "http://queue.internal:8787",
    token: "private-test-token",
    origins: [origin],
    fetcher: fetcher as unknown as typeof fetch,
  });
  return { proxy, fetcher };
}
describe("read-only local gateway", () => {
  it.each(["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])(
    "rejects %s without contacting queue",
    async (method) => {
      const { proxy, fetcher } = setup();
      expect((await proxy(new Request(`${origin}/api/jobs`, { method }))).status).toBe(405);
      expect(fetcher).not.toHaveBeenCalled();
    },
  );
  it.each([
    `/api/jobs/${id}/cancel`,
    "/api/worker/claim",
    "/api/inputs/" + "a".repeat(64),
    "/api/jobs?url=http://other.test",
    `/api/jobs/${id}/logs?after=-2`,
    `/api/jobs/${id}/logs?after=1&after=2`,
  ])("rejects non-read route %s", async (path) => {
    const { proxy, fetcher } = setup();
    expect((await proxy(new Request(origin + path))).status).toBeGreaterThanOrEqual(400);
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("rejects foreign origins and host rebinding", async () => {
    const { proxy, fetcher } = setup();
    for (const request of [
      new Request(`${origin}/api/jobs`, { headers: { Origin: "https://evil.test" } }),
      new Request("http://evil.test:8790/api/jobs"),
      new Request(`${origin}/api/jobs`, { headers: { "Sec-Fetch-Site": "cross-site" } }),
    ])
      expect((await proxy(request)).status).toBe(403);
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("forwards only configured credentials, route and cursor", async () => {
    const { proxy, fetcher } = setup();
    const response = await proxy(
      new Request(`${origin}/api/jobs/${id}/logs?after=7`, {
        headers: { Authorization: "Bearer browser-token", Cookie: "secret" },
      }),
    );
    expect(response.status).toBe(200);
    const [url, init] = fetcher.mock.calls[0] as unknown as [URL, RequestInit];
    expect(url.toString()).toBe(`http://queue.internal:8787/v1/jobs/${id}/logs?after=7`);
    expect(init.method).toBe("GET");
    expect(init.headers).toEqual({ Authorization: "Bearer private-test-token" });
    expect(init.redirect).toBe("error");
    expect(await response.text()).not.toContain("private-test-token");
  });
  it("streams artifacts with identity and a fixed download filename", async () => {
    const { proxy } = setup(
      () =>
        new Response("archive", {
          headers: { "X-Artifact-SHA256": "abc123", "Set-Cookie": "private" },
        }),
    );
    const response = await proxy(new Request(`${origin}/api/jobs/${id}/artifact`));
    expect(await response.text()).toBe("archive");
    expect(response.headers.get("X-Artifact-SHA256")).toBe("abc123");
    expect(response.headers.get("Content-Disposition")).toContain(id + ".tar.gz");
    expect(response.headers.has("Set-Cookie")).toBe(false);
  });
  it("does not expose upstream errors or credentials", async () => {
    const { proxy } = setup(() => new Response("private-test-token", { status: 401 }));
    const response = await proxy(new Request(`${origin}/api/jobs`));
    expect(response.status).toBe(401);
    expect(await response.json()).toEqual({ error: "QUEUE AUTH FAILED" });
  });
  it("bounds JSON response size", async () => {
    const { proxy } = setup(() => new Response(" ".repeat(4 * 1024 ** 2 + 1)));
    const response = await proxy(new Request(`${origin}/api/jobs`));
    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({ error: "QUEUE RESPONSE TOO LARGE" });
  });
});
