export interface ProxyOptions {
  upstream: string;
  token: string;
  origins: string[];
  fetcher?: typeof fetch;
}

export function jsonError(message: string, status: number) {
  return Response.json({ error: message }, { status, headers: { "Cache-Control": "no-store" } });
}

/** Fixed GET routes only. Never accept an upstream URL or credential from the browser. */
export function createReadOnlyProxy(options: ProxyOptions) {
  const upstream = new URL(options.upstream);
  if (
    !["http:", "https:"].includes(upstream.protocol) ||
    upstream.username ||
    upstream.password ||
    upstream.pathname !== "/" ||
    upstream.search ||
    upstream.hash
  ) {
    throw new Error("MACQUEUE_URL must be an HTTP(S) origin");
  }
  const origins = new Set(options.origins);
  const hosts = new Set(options.origins.map((origin) => new URL(origin).host));
  const fetcher = options.fetcher ?? fetch;
  return async (request: Request): Promise<Response> => {
    const url = new URL(request.url);
    if (
      !hosts.has(url.host) ||
      (request.headers.get("host") && !hosts.has(request.headers.get("host")!))
    )
      return jsonError("LOCAL HOST REQUIRED", 403);
    if (request.method !== "GET") return jsonError("READ ONLY", 405);
    const origin = request.headers.get("origin");
    if ((origin && !origins.has(origin)) || request.headers.get("sec-fetch-site") === "cross-site")
      return jsonError("LOCAL ORIGIN REQUIRED", 403);
    const match = /^\/api\/jobs(?:\/([a-f0-9]{32})(?:\/(logs|artifact))?)?$/.exec(url.pathname);
    if (!match) return jsonError("NOT FOUND", 404);
    if (match[2] === "logs") {
      const params = [...url.searchParams];
      if (
        params.length > 1 ||
        (params.length && (params[0][0] !== "after" || !/^(?:-1|\d{1,7})$/.test(params[0][1])))
      )
        return jsonError("INVALID LOG CURSOR", 400);
    } else if (url.search) return jsonError("QUERY NOT ALLOWED", 400);
    if (!options.token) return jsonError("TOKEN NOT CONFIGURED", 503);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15_000);
    try {
      const response = await fetcher(
        new URL(url.pathname.replace("/api/", "/v1/") + url.search, upstream),
        {
          method: "GET",
          headers: { Authorization: `Bearer ${options.token}` },
          redirect: "error",
          signal: AbortSignal.any([request.signal, controller.signal]),
        },
      );
      if (!response.ok) {
        await response.body?.cancel();
        return jsonError(
          response.status === 401
            ? "QUEUE AUTH FAILED"
            : response.status === 404
              ? "NOT FOUND"
              : "QUEUE REQUEST FAILED",
          response.status,
        );
      }
      if (match[2] === "artifact") {
        clearTimeout(timer);
        const headers = new Headers({
          "Content-Type": "application/gzip",
          "Cache-Control": "no-store",
          "Content-Disposition": `attachment; filename="${match[1]}.tar.gz"`,
          "X-Content-Type-Options": "nosniff",
        });
        for (const key of ["Content-Length", "X-Artifact-SHA256"]) {
          const value = response.headers.get(key);
          if (value) headers.set(key, value);
        }
        return new Response(response.body, { headers });
      }
      const maximum =
        match[2] === "logs" ? 8 * 1024 ** 2 : match[1] ? 33 * 1024 ** 2 : 4 * 1024 ** 2;
      const reader = response.body?.getReader();
      if (!reader) return jsonError("EMPTY QUEUE RESPONSE", 502);
      const chunks: Uint8Array[] = [];
      let total = 0;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        total += value.byteLength;
        if (total > maximum) {
          await reader.cancel();
          return jsonError("QUEUE RESPONSE TOO LARGE", 502);
        }
        chunks.push(value);
      }
      const body = new Uint8Array(total);
      let offset = 0;
      for (const chunk of chunks) {
        body.set(chunk, offset);
        offset += chunk.length;
      }
      JSON.parse(new TextDecoder().decode(body));
      return new Response(body, {
        headers: {
          "Content-Type": "application/json",
          "Cache-Control": "no-store",
          "X-Content-Type-Options": "nosniff",
        },
      });
    } catch {
      return jsonError("QUEUE UNREACHABLE", 502);
    } finally {
      clearTimeout(timer);
    }
  };
}
