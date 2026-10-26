/** Private, read-only Val Town bridge for public Kalshi Trade API v2 data.
 * Do not add private portfolio or order-write routes. */
const BASE = "https://api.elections.kalshi.com/trade-api/v2";
function reply(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), { status, headers: {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store", "x-content-type-options": "nosniff"
  }});
}
const validTicker = (s: string) => /^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$/.test(s);
function allowed(path: string): boolean {
  if (["/events", "/events/multivariate", "/markets", "/series", "/exchange/status", "/historical/cutoff"].includes(path)) return true;
  const p = path.split("/").filter(Boolean);
  if (p.length === 2 && ["events", "markets", "series"].includes(p[0])) return validTicker(p[1]);
  if (p.length === 3 && p[0] === "historical" && p[1] === "markets") return validTicker(p[2]);
  return false;
}
function equalSecret(a: string, b: string): boolean {
  if (!a || !b || a.length !== b.length) return false;
  let diff = 0; for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}
export default async function handler(req: Request): Promise<Response> {
  if (req.method !== "GET") return reply({ error: "method_not_allowed" }, 405);
  const expected = (Deno.env.get("ATI_INGEST_SHARED_TOKEN") ?? "").trim();
  const supplied = (req.headers.get("authorization") ?? "").match(/^Bearer\s+(.+)$/i)?.[1]?.trim() ?? "";
  if (!equalSecret(expected, supplied)) return reply({ error: "unauthorized" }, 401);
  const incoming = new URL(req.url);
  const path = incoming.pathname.replace(/\/+$/, "") || "/";
  if (!allowed(path)) return reply({ error: "route_not_allowlisted" }, 404);
  if (incoming.search.length > 2048) return reply({ error: "query_too_large" }, 400);
  const params = new URLSearchParams();
  for (const [key, value] of incoming.searchParams.entries()) {
    if (!/^[A-Za-z0-9_]+$/.test(key) || value.length > 512) return reply({ error: "invalid_query_parameter" }, 400);
    params.append(key, value);
  }
  try {
    const response = await fetch(BASE + path + (params.size ? "?" + params.toString() : ""), {
      headers: { accept: "application/json", "user-agent": "kalshi-bot-val-town-bridge/1.0" },
      signal: AbortSignal.timeout(8000)
    });
    const body = await response.text();
    if (body.length > 2000000) return reply({ error: "upstream_response_too_large" }, 502);
    if (!response.ok) return reply({ error: "kalshi_upstream_error", upstream_status: response.status }, response.status);
    try { return reply(JSON.parse(body)); } catch { return reply({ error: "invalid_upstream_json" }, 502); }
  } catch (e) { return reply({ error: "upstream_unavailable", kind: e instanceof Error ? e.name : "network_error" }, 502); }
}