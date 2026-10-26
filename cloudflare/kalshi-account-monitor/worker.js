const API_BASE = "https://external-api.kalshi.com/trade-api/v2";

function corsHeaders(origin, env) {
  const allowed = (env.APP_ORIGIN || "").trim();
  const headers = {
    "Vary": "Origin",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Cache-Control": "no-store",
    "Content-Type": "application/json; charset=utf-8",
  };
  if (allowed && origin === allowed) headers["Access-Control-Allow-Origin"] = allowed;
  return headers;
}

function json(data, status, origin, env) {
  return new Response(JSON.stringify(data), { status, headers: corsHeaders(origin, env) });
}

function pemBytes(pem) {
  const body = pem.replace(/-----BEGIN PRIVATE KEY-----/g, "")
    .replace(/-----END PRIVATE KEY-----/g, "").replace(/\s+/g, "");
  const binary = atob(body);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

async function importSigningKey(pem) {
  return crypto.subtle.importKey(
    "pkcs8", pemBytes(pem), { name: "RSA-PSS", hash: "SHA-256" }, false, ["sign"]
  );
}

async function kalshiGet(path, env, key) {
  const timestamp = String(Date.now());
  const signedPath = path.split("?")[0]; // Sign the path without query parameters.
  const message = timestamp + "GET" + "/trade-api/v2" + signedPath;
  const signature = await crypto.subtle.sign(
    { name: "RSA-PSS", saltLength: 32 }, key, new TextEncoder().encode(message)
  );
  const b64 = btoa(String.fromCharCode(...new Uint8Array(signature)));
  const base = (env.KALSHI_API_BASE_URL || API_BASE).replace(/\/+$/, "");
  const response = await fetch(base + path, {
    method: "GET",
    headers: {
      "KALSHI-ACCESS-KEY": env.KALSHI_API_KEY_ID,
      "KALSHI-ACCESS-TIMESTAMP": timestamp,
      "KALSHI-ACCESS-SIGNATURE": b64,
      "Accept": "application/json",
    },
  });
  if (!response.ok) {
    const err = new Error("Kalshi returned HTTP " + response.status + " for " + path);
    err.status = response.status;
    throw err;
  }
  return response.json();
}

async function safeGet(path, env, key) {
  try { return { ok: true, data: await kalshiGet(path, env, key) }; }
  catch (e) { return { ok: false, error: e.message, status: e.status || 502 }; }
}

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin") || "";
    const headers = corsHeaders(origin, env);
    if (request.method === "OPTIONS") {
      if (!env.APP_ORIGIN || origin !== env.APP_ORIGIN) return new Response(null, { status: 403, headers });
      return new Response(null, { status: 204, headers });
    }
    if (!env.APP_ORIGIN || origin !== env.APP_ORIGIN) return json({ error: "Origin not allowed" }, 403, origin, env);
    if (request.method !== "GET") return json({ error: "GET only" }, 405, origin, env);
    const url = new URL(request.url);
    if (url.pathname === "/health") return json({ ok: true, service: "kalshi-account-monitor" }, 200, origin, env);
    if (url.pathname !== "/api/account") return json({ error: "Not found" }, 404, origin, env);
    if (!env.KALSHI_API_KEY_ID || !env.KALSHI_PRIVATE_KEY) {
      return json({ error: "Configure KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY as Worker secrets." }, 503, origin, env);
    }
    try {
      const key = await importSigningKey(env.KALSHI_PRIVATE_KEY);
      const paths = [
        [ "balance", "/portfolio/balance" ],
        [ "positions", "/portfolio/positions?limit=100" ],
        [ "fills", "/portfolio/fills?limit=100" ],
        [ "settlements", "/portfolio/settlements?limit=100" ],
      ];
      const results = await Promise.all(paths.map(async ([name, path]) => [name, await safeGet(path, env, key)]));
      const payload = Object.fromEntries(results);
      const failed = results.filter(([, result]) => !result.ok);
      return json({
        ok: failed.length === 0,
        fetchedAt: new Date().toISOString(),
        balance: payload.balance,
        positions: payload.positions,
        fills: payload.fills,
        settlements: payload.settlements,
        partialErrors: failed.map(([name, result]) => ({ section: name, status: result.status, error: result.error })),
      }, 200, origin, env);
    } catch (e) {
      return json({ error: "Could not read Kalshi account data.", detail: e.message }, 502, origin, env);
    }
  },
};
