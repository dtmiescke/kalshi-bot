"""Spot-bet watchlist: specific games, teams, players or leagues to check often.

Edit data/watchlist.json, e.g.
{
  "series":   ["KXNBAGAME"],              // whole leagues
  "teams":    ["Celtics", "Chiefs"],       // any game with these teams
  "tickers":  ["KXNFLGAME-26OCT26KCBUF"],  // exact Kalshi games or markets
  "keywords": ["Mahomes", "rivalry"]       // words in the market title
}
"""
import json
import os


class Watchlist:
    def __init__(self, series=(), teams=(), tickers=(), keywords=()):
        self.series = {x.upper() for x in series}
        self.teams = [x.lower() for x in teams]
        self.tickers = {x.upper() for x in tickers}
        self.keywords = [x.lower() for x in keywords]

    @classmethod
    def load(cls, path="data/watchlist.json"):
        if not os.path.exists(path):
            return cls()
        with open(path) as f:
            d = json.load(f)
        return cls(d.get("series", []), d.get("teams", []),
                   d.get("tickers", []), d.get("keywords", []))

    def empty(self):
        return not (self.series or self.teams or self.tickers or self.keywords)

    def wants_series(self, series):
        # teams/tickers/keywords can appear in any league, so scan it then
        return series in self.series or bool(self.teams or self.tickers or self.keywords)

    def matches(self, ev):
        if ev.get("series_ticker", "").upper() in self.series:
            return True
        tick = ev.get("event_ticker", "").upper()
        if any(tick.startswith(t) or t.startswith(tick) for t in self.tickers):
            return True
        text = " ".join([ev.get("title", "")] + [
            f"{m.get('title', '')} {m.get('yes_sub_title', '')}"
            for m in ev.get("markets", [])]).lower()
        return any(t in text for t in self.teams) or any(k in text for k in self.keywords)
