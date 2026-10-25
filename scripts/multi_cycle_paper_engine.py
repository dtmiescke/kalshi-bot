"""Multi-cycle real-data paper run; hard-blocks all exchange writes."""
import json, os, tempfile, time
from bot.config import Settings
from bot.engine import Engine
from bot.grader import grade
from bot.kalshi_client import KalshiClient
from bot.storage import Store

def main():
    os.environ.update({"BOT_MODE":"paper","LIVE_TRADING_CONFIRMED":"no",
      "LIVE_TRADING_OPERATIONAL_APPROVED":"no","COMBOS_ENABLED":"no",
      "SPORTS_MODE":"configured","SPORTS_SCAN_EVENT_BUDGET":"20",
      "SPORTS_SCAN_MARKET_BUDGET":"50","SPORTS_INTEL_ENABLED":"no",
      "SPORTS_INTEL_EVENT_SEARCH":"no","OPENROUTER_TECH_SCOUT_ENABLED":"no",
      "BRIEFING_ALERTS":"no","BURST_MINUTES":"0"})
    for key in ("KALSHI_API_KEY_ID","KALSHI_PRIVATE_KEY","KALSHI_DATA_KEY_ID",
                "KALSHI_DATA_PRIVATE_KEY","OPENROUTER_API_KEY","ODDS_API_KEY"):
        os.environ.pop(key, None)
    settings=Settings(); settings.validate()
    assert settings.mode=="paper" and not settings.live_confirmed and not settings.combos_enabled
    original=KalshiClient._req
    audit=[]
    def guarded(self,method,path,*args,**kwargs):
        method=str(method).upper(); path=str(path).split("?")[0]
        if method!="GET" or path.startswith("/portfolio/"):
            raise AssertionError("blocked non-public-read request: "+method+" "+path)
        audit.append((method,path))
        return original(self,method,path,*args,**kwargs)
    KalshiClient._req=guarded
    try:
        with tempfile.TemporaryDirectory(prefix="paper-engine-") as tmp:
            settings.db_path=os.path.join(tmp,"paper.sqlite")
            store=Store(settings.db_path); data=KalshiClient(settings.market_data_url(),timeout=12)
            engine=Engine(settings,store,data_client=data,trade_client=None)
            assert engine.trade is None
            cycles=[]
            for i in range(3):
                report=engine.run(event_budget=20,sports=True)
                assert report.get("mode")=="batch"
                cycles.append({"cycle":i+1,"markets":report.get("markets",0),
                  "matched":report.get("matched",0),"paper_bets":report.get("bets",0),
                  "scanner_error":report.get("scanner_fleet_error")})
                if i<2: time.sleep(20)
            grade(store,data)
            integrity=store.db.execute("PRAGMA integrity_check").fetchone()[0]
            snapshots=store.db.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
            signals=store.db.execute("SELECT COUNT(*) FROM signal_readings").fetchone()[0]
            audits=store.db.execute("SELECT COUNT(*) FROM decision_audit").fetchone()[0]
            modes=[r[0] for r in store.db.execute("SELECT DISTINCT mode FROM bets").fetchall()]
            assert integrity=="ok",integrity
            assert snapshots>0,"no real market snapshots persisted"
            assert audit,"no real Kalshi reads occurred"
            assert all(m=="paper" for m in modes),modes
            assert all(m=="GET" for m,_ in audit)
            assert not any(p.startswith("/portfolio/") for _,p in audit)
            print("MULTI-CYCLE REAL-DATA PAPER ENGINE RESULT")
            print(json.dumps({"status":"PASS","mode":settings.mode,"cycles":cycles,
              "http_gets":len(audit),"unique_read_paths":len(set(p for _,p in audit)),
              "exchange_writes":0,"portfolio_calls":0,"market_snapshots_persisted":snapshots,
              "signal_readings_persisted":signals,"decision_audits_persisted":audits,
              "sqlite_integrity":integrity,"ledger_modes":modes,"grade_completed":True},indent=2))
            store.db.close()
    finally: KalshiClient._req=original

if __name__=="__main__": main()
