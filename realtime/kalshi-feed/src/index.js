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
  // Also accept literal escaped newline sequences from secret-entry forms.
  const normalized = String(pem).replace(/\r/g, "").replace(/\\n/g, "\n").trim().replace(/^\uFEFF/, "");
  const match = normalized.match(/-----BEGIN ([A-Z ]+)-----([\s\S]*?)-----END \1-----/);
  if (!match) throw new Error("private key PEM envelope is invalid");
  const label = match[1];
  const clean = match[2].replace(/\s+/g, "");
  let rawText;
  try { rawText = atob(clean); } catch { throw new Error("private key PEM base64 is invalid"); }
  const raw = Uint8Array.from(rawText, c => c.charCodeAt(0));
  if (label !== "RSA PRIVATE KEY") return { der: raw, label };

  // Wrap legacy PKCS#1 RSA DER as PKCS#8 for Web Crypto.
  const derLength = n => n < 128 ? [n] : n < 256 ? [0x81, n] : [0x82, (n >> 8) & 255, n & 255];
  const algorithm = [0x30, 0x0d, 0x06, 0x09, 0x2a, 0x86, 0x48, 0x86, 0xf7, 0x0d, 0x01, 0x01, 0x01, 0x05, 0x00];
  const octetString = [0x04, ...derLength(raw.length), ...raw];
  const body = [0x02, 0x01, 0x00, ...algorithm, ...octetString];
  return { der: Uint8Array.from([0x30, ...derLength(body.length), ...body]), label };
}

function containsBytes(haystack, needle) {
  outer: for (let i = 0; i <= haystack.length - needle.length; i++) {
    for (let j = 0; j < needle.length; j++) if (haystack[i + j] !== needle[j]) continue outer;
    return true;
  }
  return false;
}

async function authHeaders(env) {
  if (!env.KALSHI_DATA_KEY_ID || !env.KALSHI_DATA_PRIVATE_KEY) {
    throw new Error("Kalshi feed credentials are not configured");
  }
  const timestamp = String(Date.now());
  const parsed = pemBytes(env.KALSHI_DATA_PRIVATE_KEY);
  const der = parsed.der;
  // Kalshi keys may be Ed25519 (PKCS#8) or legacy RSA-PSS (PKCS#1/PKCS#8).
  const isEd25519 = parsed.label === "PRIVATE KEY" && containsBytes(der, [0x06, 0x03, 0x2b, 0x65, 0x70]);
  const message = new TextEncoder().encode(timestamp + "GET" + SIGN_PATH);
  let signature;
  try {
    if (isEd25519) {
      const key = await crypto.subtle.importKey("pkcs8", der, { name: "Ed25519" }, false, ["sign"]);
      signature = await crypto.subtle.sign({ name: "Ed25519" }, key, message);
    } else {
      const key = await crypto.subtle.importKey("pkcs8", der, { name: "RSA-PSS", hash: "SHA-256" }, false, ["sign"]);
      signature = await crypto.subtle.sign({ name: "RSA-PSS", saltLength: 32 }, key, message);
    }
  } catch (error) {
    // Never include the key, PEM text, or signature in diagnostics.
    throw new Error("private key import/sign failed (" + (error?.name || "CryptoError") + "; detected " + (isEd25519 ? "Ed25519" : "RSA") + ")");
  }
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
        ? String(error.message || error.name || "connection error").replace(/[\\r\\n]/g, " ").slice(0, 120)
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
        if (type && !ALLOWED_CHANNELS.has(type) && !["subscribed", "unsubscribed", "error", "ok"].includes(type)) return;
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
    try {
      this.upstream.send(JSON.stringify(msg));
      client.send(JSON.stringify({ type: "relay_status", status: "command_forwarded", command: cmd, id: msg.id ?? null, at: Date.now() }));
    } catch {
      client.send(JSON.stringify({ type: "relay_error", message: "Upstream is reconnecting; reconnect the dashboard" }));
    }
  }

  webSocketMessage(ws, message) {
    // Required hook for hibernation API compatibility; connections are accepted above.
    this.onClientMessage(ws, message);
  }

  webSocketClose(ws) { this.clients.delete(ws); }
  webSocketError(ws) { this.clients.delete(ws); }
}
