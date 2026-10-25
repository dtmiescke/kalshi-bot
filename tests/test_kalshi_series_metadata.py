import unittest
from unittest.mock import patch

from bot.kalshi_client import KalshiClient


class TestKalshiSeriesMetadata(unittest.TestCase):
    def test_series_metadata_uses_existing_read_only_client_route(self):
        client = KalshiClient("https://api.elections.kalshi.com/trade-api/v2")
        payload = {
            "series": {
                "ticker": "KXNFLGAME",
                "settlement_sources": [{"name": "Official league result"}],
                "additional_prohibitions": ["example restriction"],
                "contract_terms_url": "https://kalshi-public-docs.s3.amazonaws.com/example.pdf",
            }
        }
        with patch.object(client, "_req", return_value=payload) as req:
            result = client.series("KXNFLGAME")
        self.assertEqual(result, payload["series"])
        req.assert_called_once_with("GET", "/series/KXNFLGAME")

    def test_invalid_series_ticker_is_rejected_before_request(self):
        client = KalshiClient("https://api.elections.kalshi.com/trade-api/v2")
        with patch.object(client, "_req") as req:
            with self.assertRaises(ValueError):
                client.series("../portfolio/orders")
            req.assert_not_called()


if __name__ == "__main__":
    unittest.main()
