// Read-only Kalshi market-data passthrough for the phone app. Kalshi's REST API
// refuses browser requests from other sites (CORS), so the app reads public
// market lists and price candles through this Worker. Public endpoints only,
// no credentials, strict input checks, short cache.
const KALSHI = "https://api.elections.kalshi.com/trade-api/v2";
const SERIES_RE = /^KX[A-Z0-9]{2,40}$/;
const TICKER_RE = /^KX[A-Z0-9_.-]{2,100}$/;
const PERIODS = new Set([1, 60, 1440]);
const MAX_SPAN = { 1: 2 * 86400, 60: 31 * 86400, 1440: 366 * 86400 };

function reply(body, status = 200, maxAge = 0) {
  return new Response(typeof body === "string" ? body : JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "access-control-allow-origin": "*",
      "cache-control": maxAge ? `public, max-age=${maxAge}` : "no-store",
    },
  });
}

function int(v) {
  return /^\d{1,12}$/.test(v || "") ? Number(v) : null;
}

/** Returns the upstream URL for an allowed request, or {error}. */
export function upstreamFor(url) {
  const q = url.searchParams;
  if (url.pathname === "/api/markets") {
    const series = q.get("series_ticker") || "";
    const status = q.get("status") || "open";
    const limit = Math.min(int(q.get("limit")) || 20, 100);
    if (!SERIES_RE.test(series)) return { error: "series_ticker must look like KXBTC15M" };
    if (!["open", "closed", "settled"].includes(status)) return { error: "status must be open, closed or settled" };
    return { url: `${KALSHI}/markets?series_ticker=${series}&status=${status}&limit=${limit}`, maxAge: 5 };
  }
  if (url.pathname === "/api/ladder") {
    const series = q.get("series_ticker") || "";
    if (!SERIES_RE.test(series)) return { error: "series_ticker must look like KXBTCD" };
    return { url: `${KALSHI}/markets?series_ticker=${series}&status=open&limit=1000`, maxAge: 15, ladder: true };
  }
  if (url.pathname === "/api/market") {
    const ticker = q.get("ticker") || "";
    if (!TICKER_RE.test(ticker)) return { error: "ticker is not a Kalshi market ticker" };
    return { url: `${KALSHI}/markets/${ticker}`, maxAge: 5 };
  }
  if (url.pathname === "/api/candles") {
    const series = q.get("series_ticker") || "", ticker = q.get("ticker") || "";
    const start = int(q.get("start_ts")), end = int(q.get("end_ts")), period = int(q.get("period_interval")) || 1;
    if (!SERIES_RE.test(series) || !TICKER_RE.test(ticker)) return { error: "series_ticker and ticker are required" };
    if (!PERIODS.has(period)) return { error: "period_interval must be 1, 60 or 1440" };
    if (start == null || end == null || end <= start || end - start > MAX_SPAN[period]) return { error: "bad start_ts/end_ts range" };
    return { url: `${KALSHI}/series/${series}/markets/${ticker}/candlesticks?start_ts=${start}&end_ts=${end}&period_interval=${period}`, maxAge: 10 };
  }
  return null;
}

const LADDER_FIELDS = ["ticker", "open_time", "close_time", "floor_strike", "cap_strike", "strike_type",
  "yes_bid_dollars", "yes_ask_dollars", "no_ask_dollars", "last_price_dollars"];

/** Trims a ladder series (hundreds of strikes per close) to what a phone
 * charts: the next few close times, each with the in-play strikes nearest 50%. */
export function ladderOf(data, now = Date.now(), closes = 3, perClose = 25) {
  const groups = new Map();
  for (const m of (data && data.markets) || []) {
    const close = Date.parse(m.close_time);
    if (!(close > now)) continue;
    const bid = parseFloat(m.yes_bid_dollars), ask = parseFloat(m.yes_ask_dollars);
    if (!(ask > 0.02 && bid < 0.98)) continue; // settled-in-all-but-name strikes
    const mid = bid >= 0 && ask > 0 ? (bid + ask) / 2 : parseFloat(m.last_price_dollars);
    if (!groups.has(m.close_time)) groups.set(m.close_time, []);
    groups.get(m.close_time).push({ m, d: Math.abs((isFinite(mid) ? mid : 0.5) - 0.5) });
  }
  return {
    closes: [...groups.entries()]
      .sort((a, b) => Date.parse(a[0]) - Date.parse(b[0]))
      .slice(0, closes)
      .map(([close_time, list]) => ({
        close_time,
        markets: list.sort((a, b) => a.d - b.d).slice(0, perClose)
          .map(({ m }) => Object.fromEntries(LADDER_FIELDS.map(k => [k, m[k] ?? null])))
          .sort((a, b) => (a.floor_strike ?? a.cap_strike ?? 0) - (b.floor_strike ?? b.cap_strike ?? 0)),
      })),
  };
}

/** Handles /api/* requests; returns null for other paths.
 * sign(path) -> auth headers (optional); cache: a Cache API store shared by all
 * phones for a few seconds (optional). One retry after a 429. */
export async function handleRest(request, fetchImpl = fetch, sign = null, cache = null, wait = ms => new Promise(r => setTimeout(r, ms))) {
  const url = new URL(request.url);
  if (!url.pathname.startsWith("/api/")) return null;
  if (request.method === "OPTIONS") return reply("", 204);
  if (request.method !== "GET") return reply({ error: "read-only: GET only" }, 405);
  const target = upstreamFor(url);
  if (!target) return reply({ error: "Use /api/markets, /api/ladder, /api/market or /api/candles" }, 404);
  if (target.error) return reply({ error: target.error }, 400);
  if (cache) {
    try { const hit = await cache.match(target.url); if (hit) return hit; } catch {}
  }
  let res;
  for (let attempt = 0; attempt < 2; attempt++) {
    const headers = { accept: "application/json", ...(sign ? await sign(new URL(target.url).pathname) : {}) };
    try {
      res = await fetchImpl(target.url, { headers });
    } catch {
      return reply({ error: "Kalshi unreachable; retry shortly" }, 502);
    }
    if (res.status !== 429) break;
    await wait(400 + attempt * 600);
  }
  let text = await res.text();
  if (target.ladder && res.ok) {
    try { text = JSON.stringify(ladderOf(JSON.parse(text))); } catch { return reply({ error: "Bad ladder data from Kalshi" }, 502); }
  }
  const out = reply(text, res.ok ? 200 : res.status, res.ok ? target.maxAge : 0);
  if (cache && res.ok) {
    try { await cache.put(target.url, out.clone()); } catch {}
  }
  return out;
}
