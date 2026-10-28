"""Entry point.

    python -m bot.main run      # one research + trade cycle
    python -m bot.main grade    # settle bets, learn from settled markets
    python -m bot.main report   # print performance
    python -m bot.main cycle    # run + grade + report (what the scheduler calls)
    python -m bot.main check    # verify keys and connectivity, trade nothing
    python -m bot.main watch    # spot check: only watchlist games + games held
    python -m bot.main scheduled  # what GitHub runs every 12 min: crypto + AI briefing
                                  # every run, sports every SPORTS_EVERY_HOURS (5.5)
    python -m bot.main live 340 # stream crypto live for N minutes (15-min markets)
    python -m bot.main dashboard  # rebuild the mobile page + trades CSV (data/site)
    python -m bot.main diagnose   # evidence report if Kalshi refuses requests
    python -m bot.main train    # train + test the model; promoted only if it wins on unseen data
    python -m bot.main history  # download real settled Kalshi 15-min windows and replay them
    python -m bot.main backtest # replay recorded research: what thresholds would have worked
    python -m bot.main accuracy90 # validate 90% high-confidence predictions and paper bets
    python -m bot.main coin-report # rank BTC/BNB/SOL/ETH by net paper profit
    python -m bot.main trade-reviews 20 # inspect recent persisted trade postmortems
    python -m bot.main trade-learning # rank historical signal associations from reviews
    python -m bot.main brief    # AI briefing on the bot's results now
    python -m bot.main auto     # what the 12-min scheduler calls: spot check
                                # every run, full scan + grading once an hour
"""
import time
import json
import logging
import os
import sys

from .config import Settings, SPORTS
from .engine import Engine
from .grader import grade
from .kalshi_client import KalshiClient
from .report import build
from .storage import Store
from .watchlist import Watchlist
from . import alerts, backtest, briefing, handoff
from .llm import LLM

FULL_SCAN_MINUTES = 60



def aligned_burst_minutes(now, default, env=None):
    """How long to stream. One long run (BURST_TARGET_MINUTES) that ends BURST_END_PHASE_SECONDS
    (default 240) into a window, so the restart after it falls in minutes 4-9, after the read
    from 20 s before the start and the bet at 90 s (bot/handoff.py). BURST_ALIGN=no keeps the
    fixed BURST_MINUTES."""
    env = os.environ if env is None else env
    if str(env.get("BURST_ALIGN", "yes")).lower() in ("no", "false", "0"):
        return default
    try:
        phase = float(env.get("BURST_END_PHASE_SECONDS", 240))
        lo = float(env.get("BURST_MIN_MINUTES", 4)) * 60
        target = float(env.get("BURST_TARGET_MINUTES", 13)) * 60
    except (TypeError, ValueError):
        return default
    return (handoff.stop_point(now, target, phase, lo) - now) / 60.0


def run_ladders(s, store, data):
    """Hourly/daily ladders + cross-market arbitrage."""
    if os.environ.get("LADDERS_ENABLED", "yes") != "yes":
        return
    try:
        eng = Engine(s, store, data_client=data)
        eng.betting_ok, _ = __import__("bot.guard", fromlist=["status"]).status(store, s.bankroll)
        print("ladders:", eng.run_ladders({"bets": 0, "skipped": {}}))
    except Exception as e:
        print("ladders failed:", e)


def run_combos(s, store, data):
    """Combo discovery (read-only) and the synthetic paper parlays."""
    # Combo discovery is read-only and cheap enough to run on every scheduled
    # cycle, independent of the less-frequent full sports scan. The Kalshi client
    # briefly caches paginated event responses, so a full-scan cycle reuses them.
    if getattr(s, "combo_research_enabled", True):
        try:
            from . import combo_research
            combo_result = combo_research.scan(data, store, settings=s)
            print("combo research:", json.dumps({
                "ok": combo_result.get("ok"),
                "scanned_markets": combo_result.get("scanned_markets"),
                "ranked_count": combo_result.get("ranked_count"),
                "skipped": combo_result.get("skipped", {}),
                "execution_enabled": False,
            }, default=str))
        except Exception as e:
            logging.warning("combo research scan failed: %s", e)
    # Synthetic BTC/ETH/SOL parlay simulations are paper-only and use the
    # existing crypto forecasts/market snapshots. They never submit exchange orders.
    try:
        from . import crypto_combo
        crypto_combo_result = crypto_combo.scan(data, store, settings=s)
        print("crypto combo paper:", json.dumps({
            "ok": crypto_combo_result.get("ok"),
            "created": crypto_combo_result.get("created", 0),
            "skipped": crypto_combo_result.get("skipped"),
            "execution": crypto_combo_result.get("execution", "none"),
        }, default=str))
    except Exception as e:
        logging.warning("crypto combo paper scan failed: %s", e)


def periodic_cycle(s, store, data):
    """What every short run used to do between streams, now every PERIODIC_EVERY_SECONDS
    inside a long stream: ladders, combo research, paper parlays and their grading."""
    run_ladders(s, store, data)
    run_combos(s, store, data)
    try:
        from . import crypto_combo
        crypto_combo.grade(store)
    except Exception as e:
        logging.warning("crypto combo grading failed: %s", e)


def _attach_code_health(store_health, code_health):
    """Keep source-structure results alongside the final database health check."""
    store_health.setdefault("checks", {})["source_structure"] = code_health
    return store_health


def training_due(store, now, every_hours=24.0, retry_hours=6.0):
    """Avoid false 24-hour success cooldowns when training lacks enough data.

    Older versions updated last_train even when ml.train returned
    "not enough data". A missing status is treated as a one-time migration
    so the scheduler can retry and establish an authoritative status.
    """
    last_attempt = float(store.get_kv("last_train_attempt", 0) or 0)
    if now - last_attempt < max(0.25, float(retry_hours)) * 3600:
        return False
    status = store.get_kv("last_train_status")
    if status in (None, "", "not enough data", "error", "running", "time limit", "not promoted"):
        # A completed fit that failed promotion is NOT a successful learning
        # milestone. Re-evaluate after the retry backoff instead of suppressing
        # candidate evaluation for a full day. The champion remains unchanged.
        return True
    last_success = float(store.get_kv("last_train", 0) or 0)
    return now - last_success >= max(0.25, float(every_hours)) * 3600


def run_scheduled(s, store, data):
    """What GitHub runs every 12 minutes.
    - sports: every SPORTS_EVERY_HOURS (default 5.5)
    - crypto: a live streaming burst of BURST_MINUTES (default 8) so each
      15-minute window is watched continuously, not glimpsed once.
      BURST_MINUTES=0 falls back to one quick snapshot.
    - grading, learning, AI briefing, and model retraining when due."""
    # Validate all modes before data collection or any execution path. In
    # demo/live this also enforces confirmation and credentials; in paper it
    # still prevents invalid risk limits and unsupported execution settings.
    s.validate()
    cycle_started = int(time.time())
    # The Nerd watchdog runs before market work and fails closed on schema/database
    # damage. Two janitors run on an hourly cadence; only raw observations are
    # deduplicated/trimmed. Bets, model weights, grades and audit history are kept.
    from . import nerd, maintenance
    code_health = nerd.inspect_code()
    full_check_due = time.time() - float(store.get_kv("nerd_last_full_check_ts", 0) or 0) >= 24 * 3600
    health = nerd.inspect_store(store, full_integrity=full_check_due)
    health = _attach_code_health(health, code_health)
    cleanup = maintenance.run_janitors(store)
    if cleanup.get("ok") and not cleanup.get("skipped"):
        print("janitors:", json.dumps(cleanup, default=str))
    health = nerd.inspect_store(store, full_integrity=full_check_due)
    # Reattach code health after the post-janitor database recheck; otherwise
    # the second inspect_store result discards the source-structure watchdog.
    health = _attach_code_health(health, code_health)
    nerd.record_success(store, health)
    if full_check_due:
        store.set_kv("nerd_last_full_check_ts", time.time())
    # Kalshi fee type/multiplier per traded series, re-read at most daily (fees.py).
    try:
        from . import fees
        from .config import CRYPTO_15M
        fee_series = list(CRYPTO_15M) + os.environ.get("CRYPTO_LADDER_SERIES", "KXBTCD,KXETHD").split(",")
        fee_series += sorted(s.selected_series() or SPORTS.keys())
        changed = fees.refresh(store, data, fee_series)
        if changed:
            print("fee changes:", json.dumps(changed))
            alerts.send("Kalshi fees changed", json.dumps(changed)[:500], tags=["warning"])
    except Exception as exc:
        logging.getLogger("bot").warning("fee table refresh failed; keeping last known fees: %s", exc)
    # Free-first public sports intelligence collection; data only, never order authority.
    if os.environ.get("SPORTS_INTEL_ENABLED", "yes").lower() in ("yes", "true", "1"):
        try:
            from . import sports_intel
            selected_series = s.selected_series()
            intel_categories = sports_intel.categories_for_series(
                selected_series if selected_series is not None else SPORTS.keys(),
                SPORTS, mode=s.sports_mode)
            intel_result = sports_intel.collect(store, categories=intel_categories)
            print("sports intelligence:", json.dumps(intel_result, default=str))
            if os.environ.get("SPORTS_INTEL_EVENT_SEARCH", "yes").lower() in ("yes", "true", "1"):
                try:
                    catalog = data.sports_series()
                    open_events = data.sports_events_all()
                    selected = s.selected_series()
                    eligible = [ev for ev in open_events
                                if ev.get("series_ticker") in catalog
                                and (selected is None or ev.get("series_ticker") in selected)]
                    targeted = sports_intel.collect_event_news(
                        store, eligible,
                        max_events=int(os.environ.get("SPORTS_INTEL_EVENT_BUDGET", "6")))
                    store.set_kv("sports_intel_event_news_health", {
                        "ts": int(time.time()), "series_discovered": len(catalog),
                        "open_events_seen": len(open_events), **targeted})
                    print("event news:", json.dumps(targeted, default=str))
                except Exception as e:
                    logging.warning("event-targeted sports news scan failed: %s", e)
        except Exception as e:
            logging.warning("sports intelligence collection failed: %s", e)
    hours = float(os.environ.get("SPORTS_EVERY_HOURS", 5.5))
    burst = float(os.environ.get("BURST_MINUTES", 8))
    do_sports = time.time() - store.get_kv("last_sports_scan", 0) >= hours * 3600 - 300
    crypto_snapshot = s.crypto_enabled and burst <= 0
    # Configured-sports and all-sports modes process a rotating bounded event
    # batch each scheduled cycle. Manual mode uses only its explicit allowlist.
    sports_batch = s.sports_mode in ("configured", "all", "manual") and burst > 0
    event_budget = max(25, min(1000, int(os.environ.get("SPORTS_SCAN_EVENT_BUDGET", "150"))))
    keep = s.crypto_enabled
    s.crypto_enabled = crypto_snapshot
    try:
        if do_sports or crypto_snapshot or sports_batch:
            out = Engine(s, store, data_client=data).run(
                sports=(do_sports or sports_batch),
                event_budget=event_budget if sports_batch else None)
            print("snapshot run:", out)
            if do_sports or sports_batch:
                store.set_kv("last_sports_scan", time.time())
    finally:
        s.crypto_enabled = keep
    every = float(os.environ.get("PERIODIC_EVERY_SECONDS", 0))
    streaming = keep and burst > 0 and every > 0       # a long stream runs these itself, every PERIODIC_EVERY_SECONDS
    if keep and not streaming:
        run_ladders(s, store, data)
    if keep and burst > 0:
        from .live.runner import run as run_live
        burst = aligned_burst_minutes(time.time(), burst)
        print("live burst:", json.dumps(run_live(
            s, store, burst, sports=False,
            periodic=(lambda: periodic_cycle(s, store, data)) if every > 0 else None,
            periodic_every=every), default=str))
    if not streaming:       # during a long stream they already ran minutes ago; starting and ending the stream is the only blind time
        run_combos(s, store, data)
    grade(store, data)
    try:
        from . import crypto_combo
        print("crypto combo grading:", json.dumps(crypto_combo.grade(store), default=str))
    except Exception as e:
        logging.warning("crypto combo grading failed: %s", e)
    # A failed-to-train attempt is not a successful training cycle. Record the
    # attempt and status separately so insufficient data is retried on a short
    # backoff instead of silently suppressing training for a full day.
    train_now = time.time()
    # kalshi-train trains in the background now (bot/train_delta.py); training
    # inside a bot run delayed that run's trading by up to 8 minutes.
    if os.environ.get("TRAIN_IN_RUN", "yes") == "yes" and training_due(
        store, train_now,
        every_hours=float(os.environ.get("TRAIN_EVERY_HOURS", 24)),
        retry_hours=float(os.environ.get("TRAIN_RETRY_HOURS", 6)),
    ):
        from . import ml
        store.set_kv("last_train_attempt", train_now)
        store.set_kv("last_train_status", "running")
        # bounded so a slow fit can't push the scheduled job past its timeout
        limit_min = float(os.environ.get("TRAIN_TIME_LIMIT_MINUTES_SCHEDULED", 8))
        m = ml.train(store, min_windows=int(os.environ.get("ML_MIN_WINDOWS", 300)),
                     fee_rate=s.taker_fee_rate, deadline_ts=time.time() + limit_min * 60)
        # A fit that finishes but fails the out-of-sample promotion gates is
        # an evaluated candidate, not a successful model update. Record that
        # distinction so the scheduler retries after TRAIN_RETRY_HOURS instead
        # of treating a rejected candidate as a 24-hour training success.
        status = m.get("status") or ("promoted" if m.get("promoted") else "not promoted")
        store.set_kv("last_train_status", status)
        if m.get("promoted"):
            store.set_kv("last_train", train_now)
        print(ml.text(m))
        if m.get("promoted"):
            alerts.send("New model promoted", ml.text(m)[:1500], tags=["brain"])
    # Daily: re-fit each coin's tail model from 14 days of free Coinbase minutes.
    # A fit is used for pricing only if it beat current pricing on newer windows.
    if s.crypto_enabled and time.time() - float(store.get_kv("tail_model_last_fit", 0) or 0) >= 86400:
        try:
            from . import history
            report = history.vol_check(store, days=14, fit=True)
            store.set_kv("tail_model_last_fit", time.time())
            print("tail models:", json.dumps({c: {k: v.get("tail_model", {}).get(k) for k in
                                                  ("accepted", "df", "vol_scale")}
                                              for c, v in report["coins"].items()}))
        except Exception as exc:
            logging.warning("tail model fit failed; pricing unchanged: %s", exc)
    print(build(store))
    if s.ai_enabled() and briefing.due(store):
        text = briefing.write(store, LLM(s.openrouter_key, store, max_per_run=2))
        if text:
            print("\n=== AI BRIEFING ===\n" + text)
            if os.environ.get("BRIEFING_ALERTS", "no") == "yes":
                alerts.send("AI briefing", text[:1500])
    # Weekly public-source engineering scout. Findings are stored as candidates;
    # it cannot edit source, run downloaded code, alter weights, or authorize bets.
    if s.openrouter_key and os.environ.get("OPENROUTER_TECH_SCOUT_ENABLED", "yes").lower() in ("yes", "true", "1"):
        try:
            from . import research_brain
            scout = research_brain.run_tech_scout(
                store, LLM(s.openrouter_key, store, max_per_run=1))
            if scout.get("source_count"):
                print("OpenRouter tech scout:", json.dumps(scout, default=str))
        except Exception as exc:
            logging.warning("OpenRouter tech scout unavailable: %s", exc)
    # connection health for the dashboard: how did Kalshi answer this run?
    from .kalshi_client import request_log
    log_ = request_log()
    store.set_kv("kalshi_health", {"ok": sum(1 for r in log_ if r["status"] < 400),
                                   "429": sum(1 for r in log_ if r["status"] == 429),
                                   "other": sum(1 for r in log_ if r["status"] >= 400 and r["status"] != 429),
                                   "ts": time.time()})
    # Silent-failure detector: did this cycle actually price markets and produce
    # signals? A critical alarm pauses only the affected area until it recovers.
    try:
        health_result = nerd.silent_failure_check(store, cycle_started, alert=alerts.send)
        if health_result["alarms"]:
            print("health alarms:", json.dumps(health_result["alarms"], default=str))
    except Exception as exc:
        logging.warning("silent-failure check failed: %s", exc)
    # Don Theo's line for the app (after the health check, so bad news is current)
    # and his Sunday Sit-Down. AI-written only within its own small daily cap.
    try:
        from . import don_theo
        theo_llm = LLM(s.openrouter_key, store, max_per_run=2) if s.ai_enabled() else None
        print("don theo:", don_theo.speak(store, s, theo_llm)["line"])
        sitdown = briefing.sunday_sitdown(store, s, theo_llm)
        if sitdown:
            print("\n=== SUNDAY SIT-DOWN ===\n" + sitdown["text"])
    except Exception as exc:
        logging.warning("don theo line failed: %s", exc)
    store.set_kv("last_run_ts", time.time())
    if time.time() - store.get_kv("last_compact", 0) >= 86400:
        store.compact()
        store.set_kv("last_compact", time.time())


def clear_pause(store, confirmed=False):
    """Clear the durable execution stop only after explicit reconciliation confirmation."""
    if not confirmed:
        raise RuntimeError(
            "Refusing to clear the execution hard stop without CONFIRM_RECONCILED=yes. "
            "First reconcile Kalshi open orders and positions against the bot ledger."
        )
    reason = store.get_kv("execution_reconciliation_required")
    store.set_kv("execution_reconciliation_required", None)
    return reason


def main(argv=None):
    argv = argv or sys.argv[1:]
    cmd = argv[0] if argv else "cycle"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    s = Settings()
    store = Store(s.db_path)
    # market-data reads are SIGNED with your data key when you add one, so Kalshi
    # sees an identified client instead of an anonymous shared GitHub server
    data = KalshiClient(s.market_data_url(), *s.data_key())

    if cmd in ("dashboard", "export"):
        from . import dashboard
        print("wrote", dashboard.write(store, s))
        return
    if cmd == "diagnose":
        from . import diagnose
        text = diagnose.run(s)
        print(text)
        with open("data/DIAGNOSE.txt", "w") as f:
            f.write(text)
        return
    if cmd == "nerd-check":
        from . import nerd
        report = nerd.inspect_store(store, full_integrity="--full" in argv)
        nerd.record_success(store, report)
        print(json.dumps(report, indent=2, default=str))
        return
    if cmd == "check":
        s.validate()
        print("mode:", s.mode)
        print("market data:", s.market_data_url())
        ev = data.sports_events("KXNFLGAME")
        print("Kalshi public data OK, NFL events open:", len(ev))
        print("odds key:", "set" if s.odds_api_key else "MISSING (consensus signal off)")
        print("openrouter key:", "set" if s.openrouter_key else "missing")
        print("omniroute:", os.environ.get("OMNIROUTE_URL") or "not configured (optional)")
        if s.ai_enabled():
            text, used = LLM(s.openrouter_key, store, max_per_run=1).ask("Reply with the word OK.")
            print("AI test:", f"OK via {used}" if text else "FAILED (check keys / model names)")
        if s.mode in ("demo", "live"):
            t = KalshiClient(s.base_url(), s.kalshi_key_id, s.kalshi_private_key)
            print("signed request OK, balance:", t.balance())
        return
    if cmd == "sports-intel":
        from . import sports_intel
        result = sports_intel.collect(store, force="--force" in argv)
        print(json.dumps(result, indent=2, default=str))
        print("latest evidence:", json.dumps(sports_intel.recent(store, limit=10), indent=2, default=str))
        return
    if cmd == "scheduled":
        try:
            run_scheduled(s, store, data)
        except Exception as exc:
            incident = {"ts": int(time.time()), "command": "scheduled",
                        "error_type": type(exc).__name__, "error": str(exc)[:2000]}
            try:
                store.set_kv("nerd_last_incident", incident)
            except Exception:
                logging.exception("could not persist scheduled-cycle incident")
            try:
                alerts.send("ATI/Kalshi bot scheduled cycle failed",
                            json.dumps(incident), priority="urgent", tags=["warning", "nerd"])
            except Exception:
                logging.exception("could not send scheduled-cycle incident alert")
            raise
        return
    if cmd == "train":
        from . import ml
        limit_min = float(os.environ.get("TRAIN_TIME_LIMIT_MINUTES", 45))
        m = ml.train(store, min_windows=int(os.environ.get("ML_MIN_WINDOWS", 300)),
                     fee_rate=s.taker_fee_rate, deadline_ts=time.time() + limit_min * 60)
        print(ml.text(m))
        if m.get("status") == "time limit":
            # Stop cleanly and exit 0 so the downloaded history is still saved;
            # the status makes the scheduler retry on the short backoff.
            store.set_kv("last_train_attempt", time.time())
            store.set_kv("last_train_status", "time limit")
            alerts.send("Training hit its time limit",
                        f"Stopped at {m.get('stage')} after {limit_min:g} min; champion kept.",
                        tags=["warning"])
            return
        if m.get("status") == "not enough data":
            # A green workflow must not imply training succeeded when no model
            # evaluation was possible.
            raise SystemExit(2)
        if m.get("promoted"):
            alerts.send("New model promoted", ml.text(m)[:1500], tags=["brain"])
        store.set_kv("last_train", time.time())
        return
    if cmd == "learning-impact":
        # Read-only replay: compare saved crypto snapshots with pre/post learned weights.
        from .learning_impact import report as learning_impact_report
        print(json.dumps(learning_impact_report(store, limit=int(argv[1]) if len(argv) > 1 else 100), indent=2))
        return
    if cmd == "price-gap":
        # read-only: our spot vs Kalshi's CF Benchmarks settlement value
        #   price-gap            the bot's own final-minute readings
        #   price-gap replay 3   Coinbase minutes over the last N days of windows
        from .grader import replay_settlement_gap, settlement_gap
        if len(argv) > 1 and argv[1] == "replay":
            days = float(argv[2]) if len(argv) > 2 else 3
            report = replay_settlement_gap(store, data, days=days)
        else:
            report = settlement_gap(store, data, max_fetch=int(argv[1]) if len(argv) > 1 else 150)
        print(json.dumps(report, indent=2))
        return
    if cmd == "vol-check":
        # read-only: is the volatility forecast the right size? (history.vol_check)
        #   vol-check 7        report only
        #   vol-check 14 fit   also fit each coin's tail model (kept only if it wins)
        days = float(argv[1]) if len(argv) > 1 else 7
        from . import history
        print(json.dumps(history.vol_check(store, days=days, fit="fit" in argv[2:]), indent=2))
        return
    if cmd == "compact":
        store.compact()
        print("compacted")
        return
    if cmd == "clear-pause":
        confirmed = os.environ.get("CONFIRM_RECONCILED", "").strip().lower() == "yes"
        reason = clear_pause(store, confirmed=confirmed)
        print("cleared hard stop:", reason or "(none was set)")
        return
    if cmd == "live":
        from .live.runner import run as run_live
        s.validate()
        minutes = float(argv[1]) if len(argv) > 1 else 60
        print(json.dumps(run_live(s, store, minutes), indent=2, default=str))
        print(build(store))
        return
    if cmd in ("train-mark", "train-export", "train-import"):
        # No-pause training (bot/train_delta.py): mark before, export after, import in the bot run.
        from . import train_delta
        db_path = os.environ.get("BOT_DB", "data/bot.sqlite")
        if cmd == "train-mark":
            train_delta.mark(db_path, argv[1])
        elif cmd == "train-export":
            print(json.dumps(train_delta.export(db_path, argv[1], argv[2], argv[3])))
        else:
            got = train_delta.import_delta(db_path, argv[1], models_src=argv[2] if len(argv) > 2 else None)
            print("training delta: " + ("already applied" if got is None else json.dumps(got)))
        return
    if cmd == "history":
        from . import history
        days = float(argv[1]) if len(argv) > 1 else 3
        # Keep CI history replays bounded. A per-run cap prevents a long-tail
        # market catalog or rate-limit cooldown from monopolizing the runner.
        max_markets = max(1, min(300, int(os.environ.get("HISTORY_MAX_MARKETS", "150"))))
        limit_min = float(os.environ.get("HISTORY_TIME_LIMIT_MINUTES", 10))
        print(history.text(history.run(store, data, days=days, fee_rate=s.taker_fee_rate,
                                       max_markets=max_markets,
                                       deadline_ts=time.time() + limit_min * 60)))
        return
    if cmd == "backtest":
        print(backtest.text(backtest.run(store, s.taker_fee_rate)))
        return
    if cmd in ("accuracy90", "confidence-report"):
        from . import high_confidence
        print(high_confidence.format_report(high_confidence.report(store)))
        return
    if cmd in ("coin-report", "coin-performance"):
        from . import coin_learning
        print(coin_learning.format_coin_report(coin_learning.coin_report(store)))
        return
    if cmd == "trade-reviews":
        from . import coin_learning
        coin_learning.backfill_trade_reviews(store)
        limit = int(argv[1]) if len(argv) > 1 else 20
        print(json.dumps(coin_learning.trade_review_report(store, limit=limit), indent=2, default=str))
        return
    if cmd == "trade-learning":
        from . import coin_learning
        coin_learning.backfill_trade_reviews(store)
        print(coin_learning.format_learning_summary(
            coin_learning.learning_summary(store),
            setups=coin_learning.setup_performance(store),
            forecasts=coin_learning.signal_forecast_summary(store)))
        return
    if cmd == "auto":
        last = store.get_kv("last_full_scan", 0)
        cmd = "cycle" if time.time() - last >= FULL_SCAN_MINUTES * 60 - 90 else "watch"
    if cmd == "watch":
        wl = Watchlist.load()
        if wl.empty() and not store.open_bets():
            print("Watchlist empty and no open bets: nothing to spot check.")
            return
        out = Engine(s, store, data_client=data).run(watch=wl)
        for a in out["alerts"]:
            print("ALERT:", a)
        return
    if cmd == "cycle":
        store.set_kv("last_full_scan", time.time())
    if cmd in ("run", "cycle"):
        Engine(s, store, data_client=data).run()
    if cmd in ("grade", "cycle"):
        grade(store, data)
    if cmd in ("report", "cycle"):
        print(build(store))
    if cmd in ("brief", "cycle") and s.ai_enabled() and (cmd == "brief" or briefing.due(store)):
        text = briefing.write(store, LLM(s.openrouter_key, store, max_per_run=2))
        if text:
            print("\n=== AI BRIEFING ===\n" + text)


if __name__ == "__main__":
    main()
