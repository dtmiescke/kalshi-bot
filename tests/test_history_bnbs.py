from bot.history import SERIES_PAIR, spot_candles_for_series


def test_bnb_history_uses_binance_not_coinbase():
    calls = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return [[60000, "600", "605", "599", "603", "12.5"]]

    def fake_get(url, timeout, params):
        calls.append((url, params))
        return Response()

    candles = spot_candles_for_series("KXBNB15M", 60, 120, get=fake_get)

    assert SERIES_PAIR["KXBNB15M"] == "BNB-USD"
    assert len(calls) == 1
    assert "binance.com/api/v3/klines" in calls[0][0]
    assert calls[0][1]["symbol"] == "BNBUSDT"
    assert candles[60]["close"] == 603.0
    assert candles[60]["volume"] == 12.5


def test_unknown_series_does_not_substitute_another_asset():
    assert spot_candles_for_series("KXUNKNOWN15M", 0, 60, get=lambda *a, **k: None) == {}
