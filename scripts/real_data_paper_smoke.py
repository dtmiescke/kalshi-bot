"""Real public Kalshi data -> paper-only quote/fill smoke; never writes to exchange.

Requires outbound HTTPS. No credentials and no actual orders are used.
"""
import os
import sys

from bot.config import Settings
from bot.kalshi_client import KalshiClient
from bot.risk import asks_from_book, fill_plan


def main():
    os.environ["BOT_MODE"] = "paper"
    os.environ["COMBOS_ENABLED"] = "no"
    os.environ["LIVE_TRADING_CONFIRMED"] = "no"
    os.environ["LIVE_TRADING_OPERATIONAL_APPROVED"] = "no"
    settings = Settings()
    settings.validate()
    assert settings.mode == "paper" and not settings.live_confirmed
    assert settings.selected_series() == {
        "KXNFLGAME", "KXNCAAFGAME", "KXNBAGAME", "KXMLBGAME", "KXNHLGAME"
    }
    client = KalshiClient(settings.market_data_url(), timeout=10)
    original_req = client._req
    calls = []

    def safe_read(method, path, *args, **kwargs):
        assert method.upper() == "GET", "paper smoke attempted an exchange write"
        assert not path.startswith("/portfolio/"), "portfolio access forbidden in smoke"
        calls.append(path)
        return original_req(method, path, *args, **kwargs)

    client._req = safe_read
    response = client._req("GET", "/markets", params={"status": "open", "limit": 100})
    markets = response.get("markets") or []
    assert isinstance(markets, list) and markets, "No real open Kalshi markets returned"
    sampled = 0
    quoted = 0
    for market in markets:
        ticker = market.get("ticker")
        if not isinstance(ticker, str) or not ticker:
            continue
        try:
            book = client.orderbook(ticker, depth=5)
        except Exception as exc:
            print("SKIP book unavailable:", ticker, type(exc).__name__)
            continue
        sampled += 1
        asks = asks_from_book(book)
        for side in ("yes", "no"):
            if asks[side]:
                quoted += 1
                contracts, avg_price = fill_plan(asks[side], max_dollars=5, limit_price=0.99)
                assert contracts >= 0
                assert avg_price is None or 0 < avg_price <= 1
        if sampled >= 5:
            break
    assert sampled > 0, "No real orderbooks available for paper fill simulation"
    assert all(not path.startswith("/portfolio/") for path in calls)
    print(f"REAL-DATA PAPER SMOKE PASS: open_markets={len(markets)} books={sampled} quoted_sides={quoted} GETs={len(calls)} exchange_writes=0")


if __name__ == "__main__":
    main()
