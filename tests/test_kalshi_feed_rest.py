"""The Worker's read-only REST passthrough (realtime/kalshi-feed/src/rest.js):
only public market lists, one market and price candles, with strict input
checks and CORS for the phone app. Runs the module under Node."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REST = ROOT / "realtime" / "kalshi-feed" / "src" / "rest.js"

SCRIPT = r"""
const { handleRest, upstreamFor, ladderOf } = await import(process.argv[1]);
const calls = [];
const fake = async (u) => { calls.push(u); return new Response('{"markets":[]}', { status: 200 }); };
const go = async (path, method = "GET") => {
  const r = await handleRest(new Request("https://feed.example" + path, { method }), fake);
  return r && { status: r.status, cors: r.headers.get("access-control-allow-origin"), body: await r.text() };
};
const out = {
  markets: await go("/api/markets?series_ticker=KXBTC15M&status=open&limit=500"),
  market: await go("/api/market?ticker=KXBTC15M-26OCT272245-45"),
  candles: await go("/api/candles?series_ticker=KXBTC15M&ticker=KXBTC15M-26OCT272245-45&start_ts=1000&end_ts=1900&period_interval=1"),
  badSeries: await go("/api/markets?series_ticker=../portfolio"),
  badSpan: await go("/api/candles?series_ticker=KXBTC15M&ticker=KXBTC15M-X&start_ts=1&end_ts=999999&period_interval=1"),
  post: await go("/api/markets?series_ticker=KXBTC15M", "POST"),
  unknown: await go("/api/portfolio/balance"),
  other: await go("/ws"),
  calls,
};
// signing, 429 retry and the shared cache
const seen = [];
let n = 0;
const flaky = async (u, init) => { seen.push(init.headers); n++; return new Response(n === 1 ? "{}" : '{"markets":[1]}', { status: n === 1 ? 429 : 200 }); };
const store = new Map();
const cache = { match: async k => store.get(k)?.clone(), put: async (k, r) => { store.set(k, r); } };
const sign = async path => ({ "KALSHI-ACCESS-KEY": "k", "SIGNED-PATH": path });
const req = () => new Request("https://feed.example/api/markets?series_ticker=KXETH15M");
const first = await handleRest(req(), flaky, sign, cache, async () => {});
const second = await handleRest(req(), flaky, sign, cache, async () => {});
out.retry = { status: first.status, body: await first.text(), cached: await second.text(), upstreamCalls: n,
  signedPath: seen[0]["SIGNED-PATH"], key: seen[0]["KALSHI-ACCESS-KEY"] };
// ladder: hundreds of strikes trimmed to the next closes' in-play strikes
const now = Date.parse("2026-10-28T10:00:00Z");
const mk = (close, strike, bid, ask) => ({ ticker: "KXBTCD-" + close.slice(11, 13) + "-T" + strike, close_time: close,
  open_time: "2026-10-27T22:00:00Z", floor_strike: strike, strike_type: "greater", yes_bid_dollars: String(bid),
  yes_ask_dollars: String(ask), no_ask_dollars: String(1 - bid), last_price_dollars: "0.5", volume: 99, rules_primary: "long text" });
const many = [];
for (let i = 0; i < 60; i++) { const p = Math.max(0, Math.min(1, 1 - i / 60)); many.push(mk("2026-10-28T21:00:00Z", 80000 + i * 100, Math.max(0, p - 0.01).toFixed(2), Math.min(1, p + 0.01).toFixed(2))); }
many.push(mk("2026-10-28T11:00:00Z", 81000, 0.4, 0.42), mk("2026-10-28T09:00:00Z", 81000, 0.4, 0.42),
  mk("2026-10-30T21:00:00Z", 81000, 0.5, 0.52), mk("2026-10-31T21:00:00Z", 81000, 0.5, 0.52), mk("2026-10-28T11:00:00Z", 70000, 0.99, 1));
const lad = ladderOf({ markets: many }, now);
out.ladder = { closes: lad.closes.map(c => [c.close_time, c.markets.length]), keys: Object.keys(lad.closes[1].markets[0]).sort(),
  strikes: lad.closes[1].markets.map(m => m.floor_strike) };
const ladFake = async (u) => { calls.push(u); return new Response(JSON.stringify({ markets: [mk("2099-01-01T00:00:00Z", 1, 0.5, 0.52)] }), { status: 200 }); };
const lr = await handleRest(new Request("https://feed.example/api/ladder?series_ticker=KXBTCD"), ladFake);
out.ladderHttp = { status: lr.status, body: JSON.parse(await lr.text()), url: calls[calls.length - 1] };
out.ladderBad = (await handleRest(new Request("https://feed.example/api/ladder?series_ticker=x"), ladFake)).status;
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestFeedRest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        r = subprocess.run(["node", "--input-type=module", "-e", SCRIPT, REST.as_uri()],
                           capture_output=True, text=True, timeout=60)
        if r.returncode:
            raise AssertionError(r.stderr)
        cls.out = json.loads(r.stdout)

    def test_allowed_reads_pass_through_with_cors(self):
        for k in ("markets", "market", "candles"):
            self.assertEqual(self.out[k]["status"], 200, k)
            self.assertEqual(self.out[k]["cors"], "*")
        calls = self.out["calls"]
        self.assertEqual(len(calls), 4)
        self.assertTrue(calls[0].endswith("/markets?series_ticker=KXBTC15M&status=open&limit=100"))  # capped
        self.assertTrue(calls[1].endswith("/markets/KXBTC15M-26OCT272245-45"))
        self.assertIn("/series/KXBTC15M/markets/KXBTC15M-26OCT272245-45/candlesticks?start_ts=1000", calls[2])

    def test_ladder_trims_to_next_closes_and_in_play_strikes(self):
        lad = self.out["ladder"]
        # past close and the 4th future close dropped; the 99c strike dropped; 25 strikes max
        self.assertEqual(lad["closes"], [["2026-10-28T11:00:00Z", 1], ["2026-10-28T21:00:00Z", 25],
                                         ["2026-10-30T21:00:00Z", 1]])
        self.assertNotIn("rules_primary", lad["keys"])      # slim payload for phones
        self.assertIn("yes_ask_dollars", lad["keys"])
        self.assertEqual(lad["strikes"], sorted(lad["strikes"]))
        self.assertIn(83000, lad["strikes"])                 # the 50% strike is kept
        h = self.out["ladderHttp"]
        self.assertEqual(h["status"], 200)
        self.assertEqual(len(h["body"]["closes"]), 1)
        self.assertTrue(h["url"].endswith("/markets?series_ticker=KXBTCD&status=open&limit=1000"))
        self.assertEqual(self.out["ladderBad"], 400)

    def test_signed_retried_once_on_429_and_cached(self):
        r = self.out["retry"]
        self.assertEqual(r["status"], 200)
        self.assertEqual(r["body"], '{"markets":[1]}')
        self.assertEqual(r["cached"], r["body"])            # second phone served from cache
        self.assertEqual(r["upstreamCalls"], 2)             # 429 + retry, then nothing
        self.assertEqual(r["signedPath"], "/trade-api/v2/markets")   # signature covers the path only
        self.assertEqual(r["key"], "k")

    def test_everything_else_is_refused_without_calling_kalshi(self):
        self.assertEqual(self.out["badSeries"]["status"], 400)
        self.assertEqual(self.out["badSpan"]["status"], 400)
        self.assertEqual(self.out["post"]["status"], 405)
        self.assertEqual(self.out["unknown"]["status"], 404)
        self.assertIsNone(self.out["other"])            # /ws is left to the relay


if __name__ == "__main__":
    unittest.main()
