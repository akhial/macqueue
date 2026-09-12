import { lstat, readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { createReadOnlyProxy, jsonError, parseDashboardOrigin } from "./proxy.ts";

const root = resolve(import.meta.dir, "..");
const port = Number(process.env.DASHBOARD_PORT ?? 8790);
if (!Number.isInteger(port) || port < 1024 || port > 65535)
  throw new Error("Invalid dashboard port");
const tokenPath = resolve(root, process.env.MACQUEUE_TOKEN_FILE ?? ".local/submit.token");
let token = "";
try {
  const info = await lstat(tokenPath);
  if (!info.isFile() || info.isSymbolicLink() || (info.mode & 0o077) !== 0)
    throw new Error("Token must be a regular owner-only file");
  token = (await readFile(tokenPath, "utf8")).trim();
  if (token.length < 32) throw new Error("Invalid queue token");
} catch (error) {
  if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  console.warn("Token missing. See frontend/README.md.");
}
const origins = [
  `http://127.0.0.1:${port}`,
  `http://localhost:${port}`,
  ...(process.env.DASHBOARD_ORIGIN ? [parseDashboardOrigin(process.env.DASHBOARD_ORIGIN)] : []),
  ...(process.env.NODE_ENV === "development"
    ? ["http://127.0.0.1:8791", "http://localhost:8791"]
    : []),
];
const hosts = new Set(origins.map((origin) => new URL(origin).host));
const proxy = createReadOnlyProxy({
  upstream: process.env.MACQUEUE_URL ?? "http://100.102.112.115:8787",
  token,
  origins,
});
const security = {
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
  "Content-Security-Policy":
    "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
};
const server = Bun.serve({
  hostname: "127.0.0.1",
  port,
  idleTimeout: 120,
  async fetch(request) {
    const url = new URL(request.url);
    // Serve terminates TLS and preserves Host on its HTTP connection to loopback.
    // Keep exact host/origin checks; do not trust arbitrary forwarded headers.
    if (
      !hosts.has(url.host) ||
      (request.headers.get("host") && !hosts.has(request.headers.get("host")!))
    )
      return jsonError("LOCAL HOST REQUIRED", 403);
    if (url.pathname.startsWith("/api/")) return proxy(request);
    if (request.method !== "GET") return jsonError("READ ONLY", 405);
    const path = url.pathname === "/" ? "index.html" : url.pathname.slice(1);
    if (!/^(?:index\.html|favicon\.svg|assets\/[a-zA-Z0-9_.-]+)$/.test(path))
      return jsonError("NOT FOUND", 404);
    const file = Bun.file(resolve(root, "dist", path));
    if (!(await file.exists())) return jsonError("BUILD REQUIRED: pnpm build", 404);
    return new Response(file, {
      headers: {
        ...security,
        "Cache-Control": path.startsWith("assets/")
          ? "public, max-age=31536000, immutable"
          : "no-cache",
      },
    });
  },
});
console.log(`Macqueue dashboard: ${server.url.toString()}`);
