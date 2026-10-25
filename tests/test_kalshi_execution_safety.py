"""Regression tests for safe Kalshi order transport and input validation."""
import pytest
import requests
from unittest.mock import Mock

from bot.kalshi_client import KalshiClient


@pytest.mark.parametrize("ticker,side,count,price", [
    ("BAD/ROUTE", "yes", 1, 0.5),
    ("MARKET", "maybe", 1, 0.5),
    ("MARKET", "yes", 0, 0.5),
    ("MARKET", "yes", 1.5, 0.5),
    ("MARKET", "yes", True, 0.5),
    ("MARKET", "yes", 1, float("nan")),
    ("MARKET", "yes", 1, 0.001),
    ("MARKET", "yes", 1, 0.555),
])
def test_invalid_orders_never_reach_exchange(ticker, side, count, price):
    client = KalshiClient("https://example.com/trade-api/v2")
    client.session.request = Mock()
    with pytest.raises(ValueError):
        client.place_limit_buy(ticker, side, count, price)
    client.session.request.assert_not_called()


def test_ambiguous_post_timeout_is_not_retried():
    client = KalshiClient("https://example.com/trade-api/v2")
    client.session.request = Mock(side_effect=requests.Timeout("ambiguous"))
    with pytest.raises(requests.Timeout):
        client.place_limit_buy("MARKET", "yes", 1, 0.50)
    assert client.session.request.call_count == 1


def test_order_write_rate_limit_is_not_retried():
    client = KalshiClient("https://example.com/trade-api/v2")
    response = Mock(status_code=429, headers={"Retry-After": "1"})
    response.raise_for_status.side_effect = requests.HTTPError("429")
    client.session.request = Mock(return_value=response)
    with pytest.raises(requests.HTTPError):
        client.place_limit_sell("MARKET", "no", 1, 0.50)
    assert client.session.request.call_count == 1


def test_invalid_resting_expiry_never_reaches_exchange():
    client = KalshiClient("https://example.com/trade-api/v2")
    client.session.request = Mock()
    with pytest.raises(ValueError):
        client.place_resting_buy("MARKET", "yes", 1, 0.50, expires_s=0)
    client.session.request.assert_not_called()
