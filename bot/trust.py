"""How much should the bot trust its own model? Learned from its own settled picks, not assumed.

On the NBA props settled so far, the best mix of "model" and "Kalshi's price" put 0% weight on the model: the prices were
well calibrated and the model's chances were not. So each tracker learns a trust weight `lam` from its settled picks (the
weight that scores best on log loss, held between 0 and 0.5, and a cautious default until there are MIN_SETTLED picks) and
only calls a pick OFFICIAL when the chance that results from that trust still beats the price after fees:

    p_cal = price + lam * (model_chance - price)        official when p_cal - price - fee per contract >= BAR

Picks that fail stay recorded as research picks so the weight keeps being re-learned; nothing is hidden. Combos are built
from official picks only. Nothing here places a bet."""
import math

MIN_SETTLED = 60
DEFAULT_LAM = 0.25
MAX_LAM = 0.5
BAR = 0.03
SCHEMA = """CREATE TABLE IF NOT EXISTS pick_trust (
    ticker TEXT PRIMARY KEY, sport TEXT, ts INTEGER, lam REAL, p_cal REAL, edge_cal REAL, official INTEGER)"""
TABLES = {"NBA": "nba_picks", "NFL": "nfl_picks"}


def _logloss(rows, lam):
    tot = 0.0
    for p, price, won in rows:
        q = min(0.999, max(0.001, price + lam * (p - price)))
        tot -= won * math.log(q) + (1 - won) * math.log(1 - q)
    return tot / len(rows)


def learn(store, sport):
    """{"lam", "lam_hat", "n", "source"} from the settled picks of one sport."""
    try:
        rows = [(r["p_model"], r["price"], 1.0 if r["pnl"] > 0 else 0.0) for r in store.q(
            f"SELECT p_model, price, pnl FROM {TABLES[sport]} WHERE status='settled' AND pnl IS NOT NULL AND p_model IS NOT NULL")]
    except Exception:
        rows = []
    if len(rows) < MIN_SETTLED:
        return {"lam": DEFAULT_LAM, "lam_hat": None, "n": len(rows), "source": "default"}
    grid = [i / 20 for i in range(0, 21)]
    hat = min(grid, key=lambda lam: _logloss(rows, lam))
    return {"lam": min(MAX_LAM, max(0.0, hat)), "lam_hat": hat, "n": len(rows), "source": "learned"}


def judge(p_model, price, fee_per_contract, lam):
    """(p_cal, edge_cal, official) for one pick."""
    p_cal = price + lam * (p_model - price)
    edge = p_cal - price - fee_per_contract
    return round(p_cal, 4), round(edge, 4), edge >= BAR


def save(store, pick, sport, lam, now):
    """Record the verdict for a pick. The pick dict needs ticker, price, p_model, fee and contracts."""
    store.x(SCHEMA)
    contracts = pick.get("contracts") or 1
    p_cal, edge, ok = judge(pick["p_model"], pick["price"], (pick.get("fee") or 0.0) / contracts, lam)
    store.x("INSERT OR IGNORE INTO pick_trust VALUES (?,?,?,?,?,?,?)", (pick["ticker"], sport, int(now), lam, p_cal, edge, 1 if ok else 0))
    return ok


def load(store):
    try:
        return {r["ticker"]: {"lam": r["lam"], "p_cal": r["p_cal"], "edge_cal": r["edge_cal"], "official": bool(r["official"])}
                for r in store.q("SELECT * FROM pick_trust")}
    except Exception:
        return {}


def summary(store):
    out = {}
    for sport in TABLES:
        out[sport] = learn(store, sport)
    return out
