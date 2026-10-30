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
from . import scorecard, cross_market, entry_rules, experiment, nba_tonight, sports_consistency, sports_study, readiness, entry_timing, hindsight, lead_lag, online, simulate, slices, sweep, window_review
from .storage import stats_since

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


def _signals_compact(raw):
    """signals_json -> "name:prob:confidence;..." for the app's bet details
    (the full JSON with per-signal details is too big for a phone download)."""
    try:
        sig = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return ""
    out = []
    for name, v in (sig.items() if isinstance(sig, dict) else []):
        if not isinstance(v, dict):
            continue
        p, c = v.get("p", v.get("prob")), v.get("c", v.get("confidence"))
        if isinstance(p, (int, float)):
            out.append(f"{name}:{round(p, 4)}:{round(c, 3) if isinstance(c, (int, float)) else ''}")
    return ";".join(out)


def export_csv(store, mode=None):
    """Export the trade lifecycle since the last stats reset (STATS_SINCE),
    optionally scoped to one account mode."""
    rows = store.q("""SELECT id, ts, exit_ts, mode, ticker, event_ticker, series, team,
                      side, price, contracts, stake, fee, model_prob, edge, confidence,
                      style, order_id, status, result, closing_price, exit_price,
                      pnl, clv, graded_ts, signals_json FROM bets
                      WHERE (? IS NULL OR mode=?) AND ts>=? ORDER BY ts, id""", (mode, mode, stats_since()))
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = [c for c in rows[0].keys() if c != "signals_json"] if rows else [
        "id", "ts", "exit_ts", "mode", "ticker", "event_ticker", "series", "team",
        "side", "price", "contracts", "stake", "fee", "model_prob", "edge",
        "confidence", "style", "order_id", "status", "result", "closing_price",
        "exit_price", "pnl", "clv", "graded_ts"
    ]
    w.writerow(cols + ["entry_time_utc", "closed_time_utc", "signals"])
    for r in rows:
        entry_ts = r["ts"]
        exit_ts = r["exit_ts"] or r["graded_ts"]
        entry_time = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(entry_ts)) if entry_ts else ""
        closed_time = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(exit_ts)) if exit_ts else ""
        w.writerow([r[c] for c in cols] + [entry_time, closed_time, _signals_compact(r["signals_json"])])
    return buf.getvalue()


def build(store, settings, live_path="data/live_summary.json", briefing_path="data/BRIEFING.txt"):
    now = time.time()
    acct = store.account(settings.bankroll, mode=settings.mode)
    ok, why = guard.status(store, settings.bankroll, mode=settings.mode)
    health = store.get_kv("kalshi_health") or {}
    live = {}
    if os.path.exists(live_path):
        try:
            with open(live_path) as f:
                live = json.load(f)
        except (OSError, ValueError):
            pass
    since = stats_since()
    graded = store.q("SELECT pnl, clv, result, side, graded_ts FROM bets WHERE status='graded' AND mode=? AND ts>=?",
                      (settings.mode, since))
    # Win percentage is settled pick accuracy, not profitability: compare the
    # chosen side with Kalshi's actual YES/NO settlement. Sold-early positions
    # have no settlement outcome and are excluded. Open bets are never counted.
    settled = [b for b in graded if b["result"] in ("yes", "no") and b["side"] in ("yes", "no")]
    wins = sum(1 for b in settled if b["result"] == b["side"])
    win_pct = 100.0 * wins / len(settled) if settled else None
    win_pct_text = f"{win_pct:.1f}%" if win_pct is not None else "—"
    # Daily stats use the configured BOT_TIMEZONE (America/Chicago fallback) and settlement
    # time, not bet-placement time. Historical overall stats remain unchanged.
    try:
        report_tz = ZoneInfo(os.environ.get("BOT_TIMEZONE", "America/Chicago"))
    except Exception:
        report_tz = timezone.utc
    today = datetime.fromtimestamp(now, timezone.utc).astimezone(report_tz).date()
    today_settled = [b for b in settled if b["graded_ts"] and
                     datetime.fromtimestamp(b["graded_ts"], timezone.utc).astimezone(report_tz).date() == today]
    today_wins = sum(1 for b in today_settled if b["result"] == b["side"])
    today_win_pct = 100.0 * today_wins / len(today_settled) if today_settled else None
    today_win_pct_text = f"{today_win_pct:.1f}%" if today_win_pct is not None else "—"
    open_bets = store.q("SELECT * FROM bets WHERE status='open' AND mode=? AND ts>=? ORDER BY ts DESC", (settings.mode, since))
    completed_bets = store.q("SELECT * FROM bets WHERE status IN ('graded','sold') AND mode=? AND ts>=? ORDER BY COALESCE(exit_ts, graded_ts, ts) DESC", (settings.mode, since))
    recent = store.q("SELECT * FROM bets WHERE status='graded' AND mode=? AND ts>=? ORDER BY graded_ts DESC LIMIT 15", (settings.mode, since))

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
        result = str(b["result"] or "").lower()
        side = str(b["side"] or "").lower()
        if result == "sold":
            result_label = "SOLD EARLY"
            result_detail = "position closed before settlement"
        elif result in ("yes", "no") and side in ("yes", "no"):
            result_label = "WON" if result == side else "LOST"
            result_detail = f"settled {result.upper()}"
        else:
            result_label = str(b["status"] or "UNKNOWN").upper()
            result_detail = result.upper() or "outcome unavailable"
        pnl = float(b["pnl"] or 0)
        closed_ts = b.get("exit_ts") or b.get("graded_ts") or b["ts"]
        closed_at = time.strftime("%Y-%m-%d %H:%M", time.gmtime(closed_ts or 0))
        entry_price = float(b["price"] or 0)
        exit_price = b.get("exit_price")
        price_detail = "entry $" + f"{entry_price:.2f}"
        if exit_price is not None:
            price_detail += " · exit $" + f"{float(exit_price):.2f}"
        pnl_text = "$" + f"{pnl:+.2f}"
        completed_rows += (
            f"<tr><td>{E(closed_at)} UTC</td>"
            f"<td>{E(short(b['ticker']))}<div class='mut'>{E(str(b['mode']).upper())}</div></td>"
            f"<td>{E(side.upper())} · {int(b['contracts'] or 0)}<div class='mut'>{price_detail}</div></td>"
            f"<td><b class='{'good' if result_label == 'WON' else 'bad' if result_label == 'LOST' else ''}'>{E(result_label)}</b>"
            f"<div class='mut'>{E(result_detail)}</div></td>"
            f"<td class='{'good' if pnl > 0 else 'bad' if pnl < 0 else ''}'><b>{pnl_text}</b></td></tr>"
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
<meta http-equiv="refresh" content="30"><title>Kalshi bot</title>
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
<div class="mut">Snapshot published {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(now))} · last bot run {_age(last_run, now)} · page checks for a newer published snapshot every 30 sec</div>
<div class="{'bad' if not last_run or now - last_run > 900 else 'good'}"><b>{'STALE DATA' if not last_run or now - last_run > 900 else 'Snapshot recent'}</b>{' — no successful bot run in the last 15 minutes; refresh cannot create new data.' if not last_run or now - last_run > 900 else ' — this is the latest saved snapshot, not a live API stream.'}</div>
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
<tr><td class="k">Closed (UTC)</td><td class="k">Market</td><td class="k">Position / prices</td><td class="k">Result</td><td class="k">Realized P&amp;L</td></tr>
{completed_rows or '<tr><td colspan=5 class="mut">No completed bets yet.</td></tr>'}
</table></div></details>
<div class="mut"><a href="bets.csv">Download all trades (CSV)</a></div></div>
<div class="card"><h2>Model</h2><div>{E(model)}</div><div class="mut">Top crypto weights: {E(top_w)}</div></div>
<div class="card"><h2>AI briefing</h2><pre>{E(brief) or 'No briefing yet.'}</pre></div>
</body></html>"""


# kv keys the phone app's Data Floor and health views read directly.
SUMMARY_KV = ("janitors_last_result", "janitors_last_run", "nerd_last_health",
              "nerd_last_healthy_ts", "health_paused_areas", "grading_lookup_health",
              "kalshi_health", "live_discovery_health", "sports_intel_health",
              "sports_scanner_health", "settle_gap_report", "last_run_ts", "last_train_status",
              "members_last_ruling", "don_theo_last", "sunday_sitdown", "odds_quota")


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception:
        return []


CRYPTO_SEGMENTS = ("KXBTC%", "KXETH%", "KXSOL%", "KXBNB%")


MARKET_TYPES = (("15m", "g.segment LIKE '%15M-%'", "series LIKE '%15M'"),
                ("ladder", "g.segment NOT LIKE '%15M-%'", "series NOT LIKE '%15M'"))


def signal_report(store, days=30, min_n=5):
    """Each crypto signal's forecasting record against Kalshi's own price.

    Paired on the same settled markets, and only where Kalshi's price was still
    a real bet (brain.LEARN_BAND) at the decision cutoff: a signal's Brier score
    (lower is better) next to the Kalshi price's Brier on exactly those markets.
    skill > 0 means the signal beat the market; < 0 means following Kalshi
    would have done better. Split by market type (15-minute vs ladders). The
    bot's blended forecast is scored the same way from the canonical pre-close
    decision (one per market).
    """
    since = int(time.time()) - days * 86400
    lo, hi = brain.LEARN_BAND
    like = " OR ".join("g.segment LIKE ?" for _ in CRYPTO_SEGMENTS)
    like_a = " OR ".join("series LIKE ?" for _ in CRYPTO_SEGMENTS)
    try:
        weights = brain.get_weights(store, "crypto")
    except Exception:
        weights = {}
    out = []
    for kind, seg_cond, series_cond in MARKET_TYPES:
        rows = store.q(f"""SELECT g.signal AS signal, COUNT(*) AS n, AVG(g.brier) AS brier,
                               AVG(k.brier) AS market_brier
                           FROM signal_grades g JOIN signal_grades k
                             ON k.segment = g.segment AND k.ts = g.ts AND k.signal = 'kalshi_market'
                           WHERE g.signal != 'kalshi_market' AND g.ts >= ? AND ({like})
                             AND {seg_cond} AND k.prob BETWEEN ? AND ?
                           GROUP BY g.signal""", (since, *CRYPTO_SEGMENTS, lo, hi))
        for r in rows:
            if r["n"] >= min_n and r["market_brier"]:
                out.append({"type": kind, "signal": r["signal"], "n": r["n"],
                            "brier": round(r["brier"], 5), "market_brier": round(r["market_brier"], 5),
                            "skill": round(1 - r["brier"] / r["market_brier"], 4),
                            "weight": round(weights.get(r["signal"], 0.0), 3)})
        # the blended forecast the bot actually trades on
        audit = store.q(f"""SELECT model_prob, outcome, signals_json FROM decision_audit
                            WHERE brier IS NOT NULL AND outcome IN (0,1) AND settled_ts >= ?
                            AND ({like_a}) AND {series_cond}""", (since, *CRYPTO_SEGMENTS))
        pairs = []
        for r in audit:
            try:
                sig = json.loads(r["signals_json"] or "{}")
            except (TypeError, ValueError):
                continue
            k = sig.get("kalshi_market") if isinstance(sig, dict) else None
            mp = k.get("p", k.get("prob")) if isinstance(k, dict) else k
            if isinstance(mp, (int, float)) and lo <= mp <= hi and r["model_prob"] is not None:
                y = int(r["outcome"])
                pairs.append(((float(r["model_prob"]) - y) ** 2, (float(mp) - y) ** 2))
        if len(pairs) >= min_n:
            b = sum(p[0] for p in pairs) / len(pairs)
            mb = sum(p[1] for p in pairs) / len(pairs)
            if mb:
                out.append({"type": kind, "signal": "_blend", "n": len(pairs), "brier": round(b, 5),
                            "market_brier": round(mb, 5), "skill": round(1 - b / mb, 4), "weight": None})
    return sorted(out, key=lambda x: (x["type"] != "15m", x["signal"] != "_blend", -x["skill"]))


def storage_report(store, top=12):
    """Where the memory file's bytes go: the biggest tables and indexes (from
    SQLite's dbstat; row counts if dbstat is unavailable) plus free pages a
    VACUUM would reclaim. Lets a size problem be traced without downloading
    the database."""
    db = store.db
    try:
        page = db.execute("PRAGMA page_size").fetchone()[0]
        pages = db.execute("PRAGMA page_count").fetchone()[0]
        free = db.execute("PRAGMA freelist_count").fetchone()[0]
    except Exception:
        return {}
    out = {"file_mb": round(page * pages / 1e6, 1), "free_mb": round(page * free / 1e6, 1)}
    try:
        rows = db.execute("""SELECT name, SUM(pgsize) FROM dbstat GROUP BY name
                             ORDER BY 2 DESC LIMIT ?""", (top,)).fetchall()
        out["tables_mb"] = {r[0]: round(r[1] / 1e6, 1) for r in rows}
    except Exception:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        out["table_rows"] = {n: db.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}
    try:   # the decision log is the biggest learned table: what is it made of?
        cutoff = int(time.time()) - 3 * 86400
        out["decision_audit"] = [dict(zip(("decision", "rows", "older_3d", "avg_signals_b", "avg_reason_b"), r))
                                 for r in db.execute("""SELECT decision, COUNT(*), SUM(ts < ?),
                                     CAST(AVG(length(signals_json)) AS INT), CAST(AVG(length(reason)) AS INT)
                                     FROM decision_audit GROUP BY decision ORDER BY 2 DESC""", (cutoff,))]
    except Exception:
        pass
    return out


def edge_report(store):
    """Does the bot's forecast beat Kalshi's price on settled 15-minute windows, and how much of
    its disagreement with the price does it bet on (brain.shrink_weight)?"""
    from . import brain
    rows = store.q("""SELECT ticker, model_prob, market_price, outcome, series FROM decision_audit
                      WHERE brier IS NOT NULL AND outcome IN (0,1) AND model_prob IS NOT NULL
                      AND market_price BETWEEN ? AND ? AND ts >= ?""",
                   (*brain.LEARN_BAND, time.time() - 30 * 86400))
    rows = [r for r in rows if brain.shrink_group(r["series"]) == "15m"]
    n = len(rows)
    return {"windows": len({r["ticker"] for r in rows}),
            "bot_brier": round(sum((r["model_prob"] - r["outcome"]) ** 2 for r in rows) / n, 4) if n else None,
            "kalshi_brier": round(sum((r["market_price"] - r["outcome"]) ** 2 for r in rows) / n, 4) if n else None,
            "share": round(brain.shrink_weight(store, "KXBTC15M"), 3),
            "on": os.environ.get("SHRINK_TO_MARKET", "yes") == "yes"}


def summary(store):
    """The small JSON the phone app reads instead of downloading the whole
    database (known problem 11). Same queries the app runs on the database, so
    both paths show the same numbers."""
    def q(sql):
        try:
            return [dict(r) for r in store.q(sql)]
        except Exception:
            return []
    council = []
    for r in q("SELECT k, v FROM kv WHERE k LIKE 'council_%'"):
        try:
            v = json.loads(r["v"])
        except (TypeError, ValueError):
            continue
        res = (v or {}).get("result") or {}
        if res.get("by") != "council":
            continue
        council.append({"ticker": r["k"][8:], "ts": (v.get("ts") or 0) * 1000,
                        "decision": res.get("decision"), "reason": res.get("reason") or "",
                        "roles": res.get("roles") or {}, "skeptic": res.get("skeptic") or {}})
    council.sort(key=lambda c: -c["ts"])
    model = None
    run = q("SELECT * FROM model_runs ORDER BY ts DESC LIMIT 1")
    if run:
        try:
            mt = json.loads(run[0].get("metrics") or "{}")
        except (TypeError, ValueError):
            mt = {}
        model = {"ts": run[0]["ts"] * 1000, "promoted": bool(run[0].get("promoted")),
                 "status": mt.get("status"), "conf": mt.get("bootstrap_confidence"),
                 "windows": mt.get("test_windows") or mt.get("n_test")}
    try:
        from .config import Settings
        from .council import standings
        s = Settings()
        members = standings(store, s)
    except Exception:
        s, members = None, {}
    # The pinned risk limits the bot runs with (the phone app shows these
    # read-only) and when the account's stats last restarted.
    account = {"bankroll": s.bankroll, "stats_since": stats_since(),
               "min_per_bet": max(s.min_per_bet, s.crypto_min_bet),
               "max_per_bet": min(s.max_per_bet, s.crypto_max_per_bet),
               "crypto_max_trades_per_hour": s.crypto_max_trades_per_hour,
               "crypto_max_per_window": s.crypto_max_per_window,
               "max_open_positions": s.max_open_positions, "max_open_cost": s.max_open_cost,
               "max_per_day": s.max_per_day, "max_total_loss": s.max_total_loss,
               "aggression_level": s.aggression_level} if s else None
    kv = {k: store.get_kv(k) for k in SUMMARY_KV}
    kv.update({k: store.get_kv(k) for k in (f"tail_model_{c}" for c in ("BTC", "ETH", "SOL"))})
    out = {
        "version": 1, "ts": int(time.time()),
        "council": council[:50], "members": members, "model": model,
        "lessons": q("SELECT ts, text FROM lessons ORDER BY ts DESC LIMIT 5"),
        "audit": q("""SELECT decision, COUNT(brier) AS n, AVG(brier) AS brier,
                      AVG(hypothetical_pnl) AS pnl FROM decision_audit
                      WHERE outcome IS NOT NULL GROUP BY decision ORDER BY decision"""),
        "combos": q("""SELECT leg_count AS legs, COUNT(*) AS n, AVG(brier) AS brier,
                       AVG(market_proxy_brier) AS mkt, SUM(hypothetical_pnl) AS pnl
                       FROM combo_prediction_audit WHERE outcome IS NOT NULL
                       GROUP BY leg_count ORDER BY leg_count"""),
        "kv": {k: v for k, v in kv.items() if v is not None},
        "storage": storage_report(store),
        "signals": _safe(signal_report, store),
        "windows": _safe(window_review.report, store),
        "playbook": _safe(hindsight.playbook_summary, store),
        "edge": _safe(edge_report, store),
        "entry_timing": _safe(entry_timing.summary, store),
        "entry_rules": _safe(entry_rules.summary, store),
        "slices": _safe(slices.summary, store),
        "simulation": _safe(simulate.report, store),
        "lead_lag": _safe(lead_lag.report, store),
        "cross_market": _safe(cross_market.report, store),
        "sports_consistency": _safe(sports_consistency.report, store),
        "scorecard": _safe(scorecard.report, store),
        "experiment": _safe(experiment.report, store),
        "sports_study": _safe(sports_study.report, store),
        "nba": _safe(nba_tonight.summary, store),
        "perp_lead": _safe(lambda st: lead_lag.report(st, table="perp_lead"), store),
        "online": _safe(online.report, store),
        "sweep": _safe(sweep.report, store),
        "account": account,
    }
    out["readiness"] = _safe(lambda st: readiness.report(st, out), store)
    return out


def write(store, settings, out_dir="data/site"):
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "summary.json"), "w") as f:
        json.dump(summary(store), f, separators=(",", ":"), default=str)
    with open(os.path.join(out_dir, "index.html"), "w") as f:
        f.write(build(store, settings))
    with open(os.path.join(out_dir, "bets.csv"), "w") as f:
        f.write(export_csv(store, mode=settings.mode))
    return out_dir
