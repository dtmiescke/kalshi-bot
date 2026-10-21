"""Free mobile dashboard + CSV export, rebuilt after every run.

The page (index.html) and bets.csv are saved to the repo's "bot-data" branch.
Turn on GitHub Pages for that branch (Settings -> Pages -> Branch: bot-data)
and the dashboard is at https://<your-username>.github.io/<repo>/ .
Your repo is public, so this page is public too (same data as the repo).
"""
import csv
import html
import io
import json
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import brain, guard

E = html.escape


def _age(ts, now):
    if not ts:
        return "never"
    s = now - ts
    return f"{int(s)}s ago" if s < 120 else f"{int(s // 60)} min ago" if s < 7200 else f"{s / 3600:.1f} h ago"


SERIES_NAME = {"KXBTC15M": "BTC 15m", "KXETH15M": "ETH 15m", "KXSOL15M": "SOL 15m",
               "KXBTCD": "BTC ladder", "KXETHD": "ETH ladder", "KXNFLGAME": "NFL",
               "KXNBAGAME": "NBA", "KXMLBGAME": "MLB", "KXNHLGAME": "NHL", "KXNCAAFGAME": "NCAAF"}


def short(ticker):
    """KXBTC15M-26OCT202000-00 -> 'BTC 15m 20:00'; KXNBAGAME-..-BOS -> 'NBA BOS'."""
    parts = (ticker or "").split("-")
    name = SERIES_NAME.get(parts[0], parts[0].replace("KX", ""))
    if len(parts) >= 2 and name.endswith("15m") and len(parts[1]) >= 11:
        return f"{name} {parts[1][-4:-2]}:{parts[1][-2:]}"
    if "ladder" in name and len(parts) >= 3:
        return f"{name} {parts[-1].lstrip('T')}"
    return f"{name} {parts[-1]}" if len(parts) >= 3 else name


def export_csv(store):
    rows = store.q("""SELECT id, ts, mode, ticker, series, side, price, contracts, stake, fee,
                      model_prob, edge, confidence, style, status, result, exit_price,
                      pnl, clv, graded_ts FROM bets ORDER BY ts""")
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = list(rows[0].keys()) if rows else ["id"]
    w.writerow(cols + ["time_utc"])
    for r in rows:
        w.writerow([r[c] for c in cols] + [time.strftime("%Y-%m-%d %H:%M", time.gmtime(r["ts"] or 0))])
    return buf.getvalue()


def build(store, settings, live_path="data/live_summary.json", briefing_path="data/BRIEFING.txt"):
    now = time.time()
    acct = store.account(settings.bankroll)
    ok, why = guard.status(store, settings.bankroll)
    health = store.get_kv("kalshi_health") or {}
    live = {}
    if os.path.exists(live_path):
        try:
            with open(live_path) as f:
                live = json.load(f)
        except (OSError, ValueError):
            pass
    graded = store.q("SELECT pnl, clv, result, side, graded_ts FROM bets WHERE status='graded'")
    # Win percentage is settled pick accuracy, not profitability: compare the
    # chosen side with Kalshi's actual YES/NO settlement. Sold-early positions
    # have no settlement outcome and are excluded. Open bets are never counted.
    settled = [b for b in graded if b["result"] in ("yes", "no") and b["side"] in ("yes", "no")]
    wins = sum(1 for b in settled if b["result"] == b["side"])
    win_pct = 100.0 * wins / len(settled) if settled else None
    win_pct_text = f"{win_pct:.1f}%" if win_pct is not None else "—"
    # Daily stats use the configured BOT_TIMEZONE (UTC fallback) and settlement
    # time, not bet-placement time. Historical overall stats remain unchanged.
    try:
        report_tz = ZoneInfo(os.environ.get("BOT_TIMEZONE", "UTC"))
    except Exception:
        report_tz = timezone.utc
    today = datetime.fromtimestamp(now, timezone.utc).astimezone(report_tz).date()
    today_settled = [b for b in settled if b["graded_ts"] and
                     datetime.fromtimestamp(b["graded_ts"], timezone.utc).astimezone(report_tz).date() == today]
    today_wins = sum(1 for b in today_settled if b["result"] == b["side"])
    today_win_pct = 100.0 * today_wins / len(today_settled) if today_settled else None
    today_win_pct_text = f"{today_win_pct:.1f}%" if today_win_pct is not None else "—"
    open_bets = store.q("SELECT * FROM bets WHERE status='open' ORDER BY ts DESC")
    completed_bets = store.q("SELECT * FROM bets WHERE status IN ('graded','sold') ORDER BY COALESCE(graded_ts, ts) DESC")
    recent = store.q("SELECT * FROM bets WHERE status='graded' ORDER BY graded_ts DESC LIMIT 15")

    def last_quote(ticker):
        r = store.q("""SELECT ts, yes_bid, yes_ask FROM market_snapshots WHERE ticker=?
                       ORDER BY ts DESC LIMIT 1""", (ticker,))
        return r[0] if r else None

    pos_rows = ""
    for b in open_bets:
        q = last_quote(b["ticker"])
        mid = f"${(q['yes_bid'] + q['yes_ask']) / 2:.2f}" if q and q["yes_bid"] is not None and q["yes_ask"] else "—"
        stale = (not q) or now - q["ts"] > 900
        pos_rows += (f"<tr><td><b>{E(b['side'].upper())}</b> {b['contracts']} @ ${b['price']:.2f}"
                     f"<div class='mut'>{E(short(b['ticker']))}</div></td><td>{mid}</td>"
                     f"<td class='{'bad' if stale else ''}'>{_age(q['ts'] if q else None, now)}</td></tr>")
    rec_rows = "".join(
        f"<tr><td class='{'good' if (b['pnl'] or 0) > 0 else 'bad'}'><b>${(b['pnl'] or 0):+.2f}</b></td>"
        f"<td>{E(short(b['ticker']))}<div class='mut'>{E(b['side'].upper())} · {E(str(b['result']))}</div></td></tr>"
        for b in recent)

    completed_rows = ""
    for b in completed_bets:
        result_label = str(b["result"] or b["status"]).upper()
        pnl = float(b["pnl"] or 0)
        closed_ts = b["graded_ts"] or b["ts"]
        closed_at = time.strftime("%Y-%m-%d %H:%M", time.gmtime(closed_ts or 0))
        completed_rows += (
            f"<tr><td>{E(closed_at)} UTC</td>"
            f"<td>{E(short(b['ticker']))}<div class='mut'>{E(str(b['mode']).upper())}</div></td>"
            f"<td>{E(str(b['side']).upper())} · {b['contracts']} @ ${b['price']:.2f}</td>"
            f"<td>{E(result_label)}</td>"
            f"<td class='{'good' if pnl > 0 else 'bad' if pnl < 0 else ''}'><b>${pnl:+.2f}</b></td></tr>"
        )

    coins = ""
    for s, c in (live.get("coins") or {}).items():
        dq = c.get("data_quality", {})
        coins += (f"<tr><td>{E(short(s))}</td><td>${(c.get('composite') or 0):,.2f}</td>"
                  f"<td>{E(str(c.get('regime')))}</td>"
                  f"<td class='{'bad' if dq.get('stale') else 'good'}'>{'stale' if dq.get('stale') else 'live'}</td>"
                  f"<td>{dq.get('checksum_fail', 0)} / {dq.get('feed_gaps', 0)}</td></tr>")

    runs = store.q("SELECT name FROM sqlite_master WHERE name='model_runs'")
    model = "No training run yet."
    if runs:
        r = store.q("SELECT ts, promoted, metrics FROM model_runs ORDER BY ts DESC LIMIT 1")
        if r:
            m = json.loads(r[0]["metrics"])
            model = (m.get("status") or
                     f"{'PROMOTED' if r[0]['promoted'] else 'not promoted'} — model "
                     f"{m.get('test_logloss_model')} vs Kalshi {m.get('test_logloss_kalshi')} "
                     f"(bootstrap {m.get('bootstrap_confidence', 0):.0%})")
    weights = brain.get_weights(store, "crypto")
    if not os.path.exists(os.path.join(os.environ.get("MODEL_DIR", "data/models"), "crypto_model.json")):
        weights.pop("ml_model", None)      # no trained model yet: don't show its weight
    crypto_sigs = ("kalshi_market", "crypto_model", "crypto_model_iv", "crypto_momentum",
                   "spot_flow", "trade_flow", "ml_model")
    top_w = ", ".join(f"{k} {v:.2f}" for k, v in sorted(weights.items(), key=lambda x: -x[1])
                      if k in crypto_sigs)
    brief = ""
    if os.path.exists(briefing_path):
        with open(briefing_path) as f:
            brief = f.read()[:2500]

    k_ok, k_429 = health.get("ok", 0), health.get("429", 0)
    k_cls = "good" if k_ok and not k_429 else "bad" if k_429 and not k_ok else "warn"
    last_run = store.get_kv("last_run_ts")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta http-equiv="refresh" content="120"><title>Kalshi bot</title>
<style>
:root{{--bg:#fbfaf7;--fg:#1d1d1b;--mut:#6b6a66;--card:#fff;--line:#e7e4dd;--good:#1b7f4b;--bad:#b3261e;--warn:#9a6700}}
@media (prefers-color-scheme:dark){{:root{{--bg:#141413;--fg:#efede8;--mut:#9c9a94;--card:#1d1d1b;--line:#2e2d2a;--good:#5fd39a;--bad:#ff8a80;--warn:#e3b341}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
padding:env(safe-area-inset-top,0) 14px env(safe-area-inset-bottom,0)}}
h1{{font-size:20px;margin:16px 0 4px}}h2{{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--mut);margin:0 0 8px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin:12px 0}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}.k{{color:var(--mut);font-size:12px}}.v{{font-size:20px;font-weight:600}}
.good{{color:var(--good)}}.bad{{color:var(--bad)}}.warn{{color:var(--warn)}}
.scroll{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:13px}}td{{padding:6px 4px;border-top:1px solid var(--line);white-space:nowrap}}
details{{margin-top:12px;border-top:1px solid var(--line);padding-top:10px}}summary{{cursor:pointer;font-weight:600;padding:4px 0}}summary:focus-visible{{outline:2px solid var(--mut);outline-offset:3}}
pre{{white-space:pre-wrap;font-family:inherit;font-size:14px;line-height:1.5;margin:0}}
td div.mut{{white-space:normal}}.mut{{color:var(--mut);font-size:12px}}a{{color:inherit}}
</style></head><body>
<h1>Kalshi bot <span class="mut">· {E(settings.mode.upper())}</span></h1>
<div class="mut">Updated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(now))} · last run {_age(last_run, now)} · refreshes every 2 min</div>
<div class="card"><h2>Status</h2>
<div class="{'good' if ok else 'bad'}"><b>{'Betting active' if ok else 'PAUSED'}</b> — {E(why)}</div>
<div class="{k_cls}">Kalshi connection: {k_ok} ok · {k_429} rate-limited (429) last run</div></div>
<div class="card"><h2>Account</h2><div class="grid">
<div><div class="k">Cash</div><div class="v">${acct['cash']:,.2f}</div></div>
<div><div class="k">Realized P&amp;L</div><div class="v {'good' if acct['realized'] >= 0 else 'bad'}">${acct['realized']:+,.2f}</div></div>
<div><div class="k">Open cost</div><div class="v">${acct['open_cost']:,.2f}</div></div>
<div><div class="k">Overall · settled · won · win %</div><div class="v">{len(settled)} · {wins} · {win_pct_text}</div></div>
<div><div class="k">Today · settled · won · win %</div><div class="v">{len(today_settled)} · {today_wins} · {today_win_pct_text}</div></div></div></div>
<div class="card"><h2>Open positions ({len(open_bets)})</h2><div class="scroll"><table>
<tr><td class="k">Position</td><td class="k">Now</td><td class="k">Quote age</td></tr>
{pos_rows or '<tr><td colspan=3 class="mut">none</td></tr>'}</table></div></div>
<div class="card"><h2>Live feeds</h2><div class="scroll"><table>
<tr><td class="k">Coin</td><td class="k">Price</td><td class="k">Regime</td><td class="k">Data</td><td class="k">Errors</td></tr>
{coins or '<tr><td colspan=5 class="mut">no live burst yet</td></tr>'}</table></div></div>
<div class="card"><h2>Recent results</h2><div class="scroll"><table>{rec_rows or '<tr><td class="mut">none yet</td></tr>'}</table></div>
<details><summary>All completed bets ({len(completed_bets)})</summary>
<div class="scroll"><table>
<tr><td class="k">Closed (UTC)</td><td class="k">Market</td><td class="k">Position</td><td class="k">Outcome</td><td class="k">P&amp;L</td></tr>
{completed_rows or '<tr><td colspan=5 class="mut">No completed bets yet.</td></tr>'}
</table></div></details>
<div class="mut"><a href="bets.csv">Download all trades (CSV)</a></div></div>
<div class="card"><h2>Model</h2><div>{E(model)}</div><div class="mut">Top crypto weights: {E(top_w)}</div></div>
<div class="card"><h2>AI briefing</h2><pre>{E(brief) or 'No briefing yet.'}</pre></div>
</body></html>"""


def write(store, settings, out_dir="data/site"):
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "index.html"), "w") as f:
        f.write(build(store, settings))
    with open(os.path.join(out_dir, "bets.csv"), "w") as f:
        f.write(export_csv(store))
    return out_dir
