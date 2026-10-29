"""Pre-registered edge hunt over replayed Kalshi 15-minute windows (ml_rows, source=history).
Usage: python scripts/edge_hunt.py path/to/db.sqlite
The hypotheses, split and bars below were fixed BEFORE looking at any result; see docs/EDGE_HUNT.md."""
import os, sys, json, math, sqlite3, datetime
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from statistics import NormalDist
from bot import risk

DBS = sys.argv[1:] or ["hist.sqlite"]
FEE = 0.07; SLIP = 0.005
def sig(x): return 1 / (1 + math.exp(-x))

def hour(ts): return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).hour
def dow(ts): return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).weekday()
SESS = {"asia": lambda h: 0 <= h < 7, "europe": lambda h: 7 <= h < 13, "us": lambda h: 13 <= h < 21, "late": lambda h: h >= 21}

H = {}
def add(name, fn): H[name] = fn
fav = lambda f, th: ("yes" if f["_mid"] >= 1 - th else "no" if f["_mid"] <= th else None)
for th in (0.10, 0.15, 0.20): add(f"favorite_when_other_side<={th}", lambda f, th=th: fav(f, th))
for lim, ds in ((4, 1.0), (4, 1.5), (6, 2.0)):
    add(f"late_certainty_left<={lim}_dist>={ds}", lambda f, lim=lim, ds=ds: (("yes" if f["dist_sigma"] > 0 else "no") if f["min_left"] <= lim and abs(f["dist_sigma"]) >= ds else None))
for k in ("ret_1m", "ret_5m", "ret_15m"):
    th = 1.5 if k == "ret_1m" else 1.0
    add(f"momentum_{k}>={th}", lambda f, k=k, th=th: ("yes" if f[k] >= th else "no" if f[k] <= -th else None))
    add(f"reversal_{k}>={th}", lambda f, k=k, th=th: ("no" if f[k] >= th else "yes" if f[k] <= -th else None))
for k in ("rsi_5m", "rsi_15m"):
    add(f"fade_{k}_extreme", lambda f, k=k: ("no" if f[k] >= 70 else "yes" if f[k] <= 30 else None))
    add(f"follow_{k}_extreme", lambda f, k=k: ("yes" if f[k] >= 70 else "no" if f[k] <= 30 else None))
for sname, sf in SESS.items():
    for side in ("yes", "no"):
        add(f"always_{side}_{sname}", lambda f, sf=sf, side=side: side if sf(hour(f["_ts"])) else None)
add("always_yes_weekend", lambda f: "yes" if dow(f["_ts"]) >= 5 else None)
add("always_no_weekend", lambda f: "no" if dow(f["_ts"]) >= 5 else None)
add("highvol_buy_underdog", lambda f: ("yes" if f["_mid"] < 0.5 else "no") if f["vol_ratio_15_60"] >= 1.5 else None)
add("lowvol_buy_favorite", lambda f: ("yes" if f["_mid"] >= 0.5 else "no") if f["vol_ratio_15_60"] <= 0.7 else None)
add("highvol_buy_favorite", lambda f: ("yes" if f["_mid"] >= 0.5 else "no") if f["vol_ratio_15_60"] >= 1.5 else None)
add("lowvol_buy_underdog", lambda f: ("yes" if f["_mid"] < 0.5 else "no") if f["vol_ratio_15_60"] <= 0.7 else None)
for th in (0.05, 0.10, 0.15):
    add(f"model_gap>={th}", lambda f, th=th: ("yes" if f["model_minus_kalshi"] >= th else "no" if f["model_minus_kalshi"] <= -th else None))
for c in ("BTC", "ETH", "SOL"):
    for side in ("yes", "no"):
        add(f"always_{side}_{c}", lambda f, c=c, side=side: side if f["_coin"] == c else None)
add("fade_bollinger_edge", lambda f: ("no" if f["bb_pctb"] >= 1.0 else "yes" if f["bb_pctb"] <= 0.0 else None))
add("follow_bollinger_edge", lambda f: ("yes" if f["bb_pctb"] >= 1.0 else "no" if f["bb_pctb"] <= 0.0 else None))

N = len(H)
Z_FAMILY = NormalDist().inv_cdf(1 - 0.05 / N)        # discovery bar: any false pass in the family <= 5%
Z_CONFIRM = 2.0                                      # confirmation bar on the untouched newer 40%
TRAIN = 0.6; MIN_TRADES = 100

rows = []
for p in DBS:
    c = sqlite3.connect(p)
    for t, ts, coin, f, y in c.execute("select ticker, ts, coin, features, y from ml_rows where ticker like 'KX%15M%' and source='history'"):
        rows.append((t, ts, coin, json.loads(f), y))
def close_key(t): return t.split("-")[1]
wins = {}
for t, ts, coin, f, y in sorted(rows, key=lambda r: r[1]):
    if not (2.0 <= f["min_left"] <= 13.5) or coin not in ("BTC", "ETH", "SOL"): continue
    f["_mid"] = sig(f["kalshi_logit"]); f["_ts"] = ts; f["_coin"] = coin
    wins.setdefault(t, []).append((f, y))
tickers = sorted(wins, key=lambda t: wins[t][0][0]["_ts"] + wins[t][0][0]["min_left"] * 60)
cut = int(len(tickers) * TRAIN)
part = {t: ("train" if i < cut else "test") for i, t in enumerate(tickers)}
print(f"windows {len(tickers)} (train {cut}, test {len(tickers)-cut}); hypotheses {N}; discovery bar z>={Z_FAMILY:.2f}; confirm z>={Z_CONFIRM}")

def entry(rule, snaps):
    for f, y in snaps:
        side = rule(f)
        if side is None: continue
        mid = f["_mid"]; spr = max(f.get("kalshi_spread") or 0.02, 0.01)
        ask = (mid + spr / 2) if side == "yes" else (1 - mid + spr / 2)
        cost = min(ask + SLIP, 0.99)
        if not 0.04 <= cost <= 0.96: continue
        fee = risk.taker_fee(100, cost, FEE) / 100
        won = (y == 1) == (side == "yes")
        return (1 - cost - fee) if won else (-cost - fee)
    return None

def stat(pairs):          # [(cluster, pnl)] -> n, mean, cluster-robust z
    n = len(pairs)
    if n < 2: return n, 0.0, 0.0
    m = sum(p for _, p in pairs) / n
    cl = {}
    for k, p in pairs: cl[k] = cl.get(k, 0.0) + (p - m)
    se = math.sqrt(sum(v * v for v in cl.values())) / n
    return n, m, (m / se if se else 0.0)

out = []
for name, rule in H.items():
    tr, te = [], []
    for t in tickers:
        r = entry(rule, wins[t])
        if r is None: continue
        (tr if part[t] == "train" else te).append((close_key(t), r))
    a, b = stat(tr), stat(te)
    passed = a[0] >= MIN_TRADES and a[1] > 0 and a[2] >= Z_FAMILY and b[0] >= MIN_TRADES // 3 and b[1] > 0 and b[2] >= Z_CONFIRM
    out.append((name, a, b, passed))
out.sort(key=lambda r: -r[1][2])
print(f"{'rule':44s} {'train n':>7s} {'mean':>7s} {'z':>6s} | {'test n':>6s} {'mean':>7s} {'z':>6s}  pass")
for name, a, b, ok in out:
    print(f"{name:44s} {a[0]:7d} {a[1]*100:6.1f}c {a[2]:6.2f} | {b[0]:6d} {b[1]*100:6.1f}c {b[2]:6.2f}  {'PASS' if ok else ''}")
print("PASSED:", [o[0] for o in out if o[3]])
