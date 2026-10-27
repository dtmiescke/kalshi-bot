import { DurableObject } from "cloudflare:workers";

const UPSTREAM = "https://external-api-ws.kalshi.com/trade-api/ws/v2";
const SIGN_PATH = "/trade-api/ws/v2";
const ALLOWED_CHANNELS = new Set(["ticker", "orderbook_delta", "trade"]);
const MAX_CLIENTS = 20;
const MAX_MESSAGE_BYTES = 16 * 1024;
const MAX_MESSAGES_PER_WINDOW = 20;
const MESSAGE_WINDOW_MS = 10_000;
const TICKER_RE = /^KX[A-Z0-9_-]{2,100}$/;

function b64(bytes) {
  let s = "";
  for (const b of new Uint8Array(bytes)) s += String.fromCharCode(b);
  return btoa(s);
}

function pemBytes(pem) {
  const clean = pem.replace(/-----BEGIN PRIVATE KEY-----/g, "")
    .replace(/-----END PRIVATE KEY-----/g, "").replace(/\s+/g, "");
  const raw = atob(clean);
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}

async function authHeaders(env) {
  if (!env.KALSHI_DATA_KEY_ID || !env.KALSHI_DATA_PRIVATE_KEY) {
    throw new Error("Kalshi feed credentials are not configured");
  }
  const timestamp = String(Date.now());
  const key = await crypto.subtle.importKey(
    "pkcs8", pemBytes(env.KALSHI_DATA_PRIVATE_KEY),
    { name: "RSA-PSS", hash: "SHA-256" }, false, ["sign"]
  );
  const signature = await crypto.subtle.sign(
    { name: "RSA-PSS", saltLength: 32 }, key,
    new TextEncoder().encode(timestamp + "GET" + SIGN_PATH)
  );
  return {
    "KALSHI-ACCESS-KEY": env.KALSHI_DATA_KEY_ID,
    "KALSHI-ACCESS-TIMESTAMP": timestamp,
    "KALSHI-ACCESS-SIGNATURE": b64(signature),
    "Upgrade": "websocket"
  };
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status, headers: { "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store", "access-control-allow-origin": "*" }
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/health") {
      return json({ ok: true, service: "kalshi-readonly-feed", now: Date.now(),
        credentialsConfigured: Boolean(env.KALSHI_DATA_KEY_ID && env.KALSHI_DATA_PRIVATE_KEY) });
    }
    if (url.pathname !== "/ws") return json({ error: "Use /health or /ws" }, 404);
    if (request.headers.get("Upgrade")?.toLowerCase() !== "websocket") {
      return json({ error: "WebSocket upgrade required" }, 426);
    }
    // Read-only market data relay. No order, portfolio, or account channels are allowed.
    const id = env.KALSHI_FEED.idFromName("shared-readonly-market-feed");
    return env.KALSHI_FEED.get(id).fetch(request);
  }
};

export class KalshiFeed extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.env = env;
    this.clients = new Set();
    this.upstream = null;
    this.upstreamReady = false;
    this.subscribedMarkets = new Set();
    this.lastMessageAt = 0;
    this.connecting = null;
    this.clientRate = new WeakMap();
  }

  async fetch(request) {
    if (request.headers.get("Upgrade")?.toLowerCase() !== "websocket") {
      return new Response("WebSocket upgrade required", { status: 426 });
    }
    if (this.clients.size >= MAX_CLIENTS) return new Response("Feed capacity reached", { status: 503, headers: { "retry-after": "10" } });
    const pair = new WebSocketPair();
    const client = pair[0];
    const server = pair[1];
    server.accept();
    this.clients.add(server);
    server.addEventListener("message", event => this.onClientMessage(server, event.data));
    server.addEventListener("close", () => this.clients.delete(server));
    server.addEventListener("error", () => this.clients.delete(server));
    try {
      await this.ensureUpstream();
      server.send(JSON.stringify({ type: "relay_status", status: "connected", at: Date.now() }));
    } catch (error) {
      const detail = error instanceof Error
        ? (error.message.match(/HTTP \\d{3}/)?.[0] || error.name || "connection error")
        : "connection error";
      console.error("Kalshi upstream connection failed:", detail);
      server.send(JSON.stringify({ type: "relay_status", status: "error",
        message: "Upstream market-data connection failed (" + detail + "); retry shortly", at: Date.now() }));
    }
    return new Response(null, { status: 101, webSocket: client });
  }

  async ensureUpstream() {
    if (this.upstreamReady && this.upstream) return;
    if (this.connecting) return this.connecting;
    this.connecting = (async () => {
      const headers = await authHeaders(this.env);
      const response = await fetch(UPSTREAM, { headers });
      if (!response.webSocket) throw new Error("Kalshi WebSocket handshake failed: " + response.status);
      const ws = response.webSocket;
      ws.accept();
      this.upstream = ws;
      this.upstreamReady = true;
      ws.addEventListener("message", event => {
        this.lastMessageAt = Date.now();
        let parsed;
        try { parsed = JSON.parse(event.data); } catch { return; }
        // Only broadcast read-only market data and subscription acknowledgements.
        const type = parsed?.type;
        if (type && !ALLOWED_CHANNELS.has(type) && !["subscribed", "unsubscribed", "error"].includes(type)) return;
        const payload = JSON.stringify(parsed);
        for (const client of this.clients) {
          try { client.send(payload); } catch { this.clients.delete(client); }
        }
      });
      const close = () => { this.upstreamReady = false; this.upstream = null; this.connecting = null; };
      ws.addEventListener("close", close);
      ws.addEventListener("error", close);
    })();
    try { await this.connecting; } finally { if (!this.upstreamReady) this.connecting = null; }
  }

  async onClientMessage(client, raw) {
    const rawText = typeof raw === "string" ? raw : "";
    if (new TextEncoder().encode(rawText).byteLength > MAX_MESSAGE_BYTES) {
      try { client.send(JSON.stringify({ type: "relay_error", message: "Message too large" })); } catch {}
      return;
    }
    const now = Date.now();
    const rate = this.clientRate.get(client) || { start: now, count: 0 };
    if (now - rate.start >= MESSAGE_WINDOW_MS) { rate.start = now; rate.count = 0; }
    rate.count += 1;
    this.clientRate.set(client, rate);
    if (rate.count > MAX_MESSAGES_PER_WINDOW) {
      try { client.send(JSON.stringify({ type: "relay_error", message: "Rate limit exceeded; slow down" })); } catch {}
      return;
    }
    let msg;
    try { msg = JSON.parse(String(raw)); } catch {
      client.send(JSON.stringify({ type: "relay_error", message: "Invalid JSON" })); return;
    }
    const cmd = msg?.cmd;
    const params = msg?.params || {};
    if (!["subscribe", "unsubscribe", "update_subscription"].includes(cmd)) {
      client.send(JSON.stringify({ type: "relay_error", message: "Command not allowed" })); return;
    }
    if (cmd === "unsubscribe") {
      const channels = params.channels;
      const tickers = params.market_tickers ?? (params.market_ticker ? [params.market_ticker] : []);
      if ((channels !== undefined && (!Array.isArray(channels) || channels.length > 3 || channels.some(ch => !ALLOWED_CHANNELS.has(ch)))) ||
          !Array.isArray(tickers) || tickers.length > 250 ||
          tickers.some(t => typeof t !== "string" || !TICKER_RE.test(t))) {
        client.send(JSON.stringify({ type: "relay_error", message: "Invalid unsubscribe parameters" })); return;
      }
    }
    if (cmd === "subscribe") {
      const channels = Array.isArray(params.channels) ? params.channels : [];
      if (!channels.length || channels.some(ch => !ALLOWED_CHANNELS.has(ch))) {
        client.send(JSON.stringify({ type: "relay_error", message: "Only ticker, orderbook_delta, and trade channels are allowed" })); return;
      }
      const tickers = params.market_tickers ?? (params.market_ticker ? [params.market_ticker] : []);
      if (!Array.isArray(tickers) || tickers.length > 250 ||
          tickers.some(t => typeof t !== "string" || !TICKER_RE.test(t))) {
        client.send(JSON.stringify({ type: "relay_error", message: "Invalid market ticker list" })); return;
      }
      for (const t of tickers) this.subscribedMarkets.add(t);
    }
    if (cmd === "update_subscription") {
      const action = params.action;
      const tickers = params.market_tickers;
      if (!["add_markets", "delete_markets", "get_snapshot"].includes(action) ||
          !Array.isArray(tickers) || tickers.length > 250 ||
          tickers.some(t => typeof t !== "string" || !/^KX[A-Z0-9_-]{2,100}$/.test(t))) {
        client.send(JSON.stringify({ type: "relay_error", message: "Invalid subscription update" })); return;
      }
    }
    if (!this.upstreamReady || !this.upstream) {
      try { await this.ensureUpstream(); } catch {
        client.send(JSON.stringify({ type: "relay_error", message: "Upstream market-data connection failed; retry shortly" })); return;
      }
    }
    try { this.upstream.send(JSON.stringify(msg)); }
    catch { client.send(JSON.stringify({ type: "relay_error", message: "Upstream is reconnecting; reconnect the dashboard" })); }
  }

  webSocketMessage(ws, message) {
    // Required hook for hibernation API compatibility; connections are accepted above.
    this.onClientMessage(ws, message);
  }

  webSocketClose(ws) { this.clients.delete(ws); }
  webSocketError(ws) { this.clients.delete(ws); }
}
