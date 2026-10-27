"""Real public Kalshi data -> paper-only quote/fill smoke; never writes to exchange.

Requires outbound HTTPS. No credentials and no actual orders are used.
"""
import os
import sys

from bot.config import Settings, CRYPTO_15M
from bot.crypto_brain import COIN_OF_SERIES
from bot.live.runner import COINS, COINS_TO_COIN
from bot.signals import crypto as crypto_sig
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
    expected = {"KXBTC15M": "BTC", "KXBNB15M": "BNB", "KXSOL15M": "SOL", "KXETH15M": "ETH"}
    assert set(CRYPTO_15M) == set(expected), f"config series mismatch: {set(CRYPTO_15M)}"
    assert set(COINS) == set(expected), f"live feed series mismatch: {set(COINS)}"
    assert COINS_TO_COIN == expected, f"live runner coin mapping mismatch: {COINS_TO_COIN}"
    assert COIN_OF_SERIES == expected, f"crypto brain mapping mismatch: {COIN_OF_SERIES}"
    client = KalshiClient(settings.market_data_url(), timeout=10)
    original_req = client._req
    calls = []

    def safe_read(method, path, *args, **kwargs):
        assert method.upper() == "GET", "paper smoke attempted an exchange write"
        assert not path.startswith("/portfolio/"), "portfolio access forbidden in smoke"
        calls.append(path)
        return original_req(method, path, *args, **kwargs)

    client._req = safe_read

    # Verify all four Kalshi series, live order books, and fresh public spot feeds.
    coin_checks = {}
    for series, coin in expected.items():
        events = client.sports_events(series)
        active = []
        for event in events:
            for market in (event.get("markets") or []):
                ticker = market.get("ticker")
                status = str(market.get("status") or "").lower()
                if ticker and ticker.startswith(series) and status in ("active", "open"):
                    active.append(market)
        assert active, f"{coin}: no open Kalshi markets found for {series}"
        ticker = active[0]["ticker"]
        book = client.orderbook(ticker, depth=5)
        assert isinstance(book, dict) and "yes" in book and "no" in book, f"{coin}: malformed order book"
        kraken_pair, coinbase_pair, _deribit = COINS[series]
        if coinbase_pair:
            spot, spot_ts = crypto_sig.fetch_spot(coinbase_pair)
            source = "coinbase"
        else:
            spot, spot_ts = crypto_sig.fetch_kraken_spot(kraken_pair)
            source = "kraken"
        assert isinstance(spot, (int, float)) and spot > 0 and spot_ts, f"{coin}: no valid fresh {source} spot quote"
        coin_checks[coin] = {"series": series, "open_markets": len(active),
                             "book_ticker": ticker, "spot": round(float(spot), 8),
                             "spot_source": source}

    response = client._req("GET", "/markets", params={"status": "open", "limit": 100})
    markets = response.get("markets") or []
    assert isinstance(markets, list) and markets, "No real open Kalshi markets returned"
    sampled = 0
    quoted = 0
    simulated_fills = 0
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
                if contracts > 0:
                    simulated_fills += 1
        if simulated_fills > 0 and sampled >= 5:
            break
        if sampled >= 100:
            break
    assert sampled > 0, "No real orderbooks available for paper fill simulation"
    assert simulated_fills > 0, "No positive paper fill against real quoted liquidity"
    assert all(not path.startswith("/portfolio/") for path in calls)
    import json
    print("FOUR-COIN PAPER DATA PASS:", json.dumps(coin_checks, sort_keys=True))
    print(f"REAL-DATA PAPER SMOKE PASS: open_markets={len(markets)} books={sampled} quoted_sides={quoted} simulated_fills={simulated_fills} GETs={len(calls)} exchange_writes=0")


if __name__ == "__main__":
    main()
