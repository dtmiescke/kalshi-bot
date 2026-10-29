"""A credential must never reach a log, an alert or the saved database (which is published on a public branch).
The secret below is a made-up sample, not a real key."""
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from bot import config, maintenance
from bot.engine import Engine
from bot.config import Settings
from bot.odds import OddsClient
from bot.redact import redact, scrub_urls
from bot.storage import Store

SECRET = "SAMPLEKEY0000sample1111KEY2222abcd"
URL = f"https://api.the-odds-api.com/v4/sports/x/odds?apiKey={SECRET}&regions=us&markets=h2h&oddsFormat=american"


class TestRedact(unittest.TestCase):
    def test_a_key_in_a_url_is_masked_and_the_rest_kept(self):
        out = redact(f"404 Client Error: Not Found for url: {URL}")
        self.assertNotIn(SECRET, out)
        self.assertIn("apiKey=***", out)
        self.assertIn("regions=us", out)                              # the rest of the message survives

    def test_other_shapes(self):
        for text in (f"token={SECRET}", f"api_key: {SECRET}", f'"password": "{SECRET}"', f"Authorization: Bearer {SECRET}",
                     f"https://x/y?access_token={SECRET}&a=1"):
            self.assertNotIn(SECRET, redact(text), text)

    def test_ordinary_text_and_non_strings_are_untouched(self):
        self.assertEqual(redact("odds feed is fine, 12 events"), "odds feed is fine, 12 events")
        self.assertEqual(redact(None), None)
        self.assertEqual(redact(5), 5)

    def test_the_strict_scrub_only_touches_url_parameters(self):
        self.assertNotIn(SECRET, scrub_urls(URL))
        data = '{"key": "game-1", "token": "abc", "note": "monkey=banana"}'
        self.assertEqual(scrub_urls(data), data)                      # data fields named key/token are left alone


class TestOddsClientNeverLeaksItsKey(unittest.TestCase):
    def test_an_http_error_message_has_no_key_and_keeps_its_type(self):
        response = Mock()
        response.raise_for_status.side_effect = requests.HTTPError(f"404 Client Error: Not Found for url: {URL}")
        with patch("bot.odds.requests.get", return_value=response):
            with self.assertRaises(requests.HTTPError) as ctx:
                OddsClient(SECRET).fetch("soccer_x")
        self.assertNotIn(SECRET, str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)                    # the original, which holds the URL, is not chained

    def test_a_connection_error_message_has_no_key(self):
        err = requests.ConnectionError(f"HTTPSConnectionPool(host='api.the-odds-api.com'): Max retries exceeded with url: /v4/x/odds?apiKey={SECRET}&regions=us")
        with patch("bot.odds.requests.get", side_effect=err):
            with self.assertRaises(requests.ConnectionError) as ctx:
                OddsClient(SECRET).fetch("soccer_x")
        self.assertNotIn(SECRET, str(ctx.exception))

    def test_the_stored_health_record_has_no_key(self):
        store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))
        eng = Engine(Settings(), store, odds_client=OddsClient(SECRET))
        response = Mock()
        response.raise_for_status.side_effect = requests.HTTPError(f"404 for url: {URL}")
        with patch("bot.engine.refresh_plan", return_value=(True, "test")), \
                patch("bot.odds.requests.get", return_value=response), patch("bot.engine.alerts.send") as send:
            eng.refresh_odds("soccer_x")
        stored = store.get_kv("odds_feed_health_soccer_x")
        self.assertEqual(stored["status"], "error")
        self.assertNotIn(SECRET, str(stored))
        self.assertNotIn(SECRET, str(send.call_args))                 # nor the alert that would go to a phone


class TestStoreAndJanitor(unittest.TestCase):
    def setUp(self):
        self.store = Store(os.path.join(tempfile.mkdtemp(), "t.sqlite"))

    def test_set_kv_scrubs_a_key_in_a_url_but_not_ordinary_data(self):
        self.store.set_kv("a", {"error": f"failed for url: {URL}"})
        self.store.set_kv("b", {"key": "game-1", "token": "abc"})
        self.assertNotIn(SECRET, self.store.q("SELECT v FROM kv WHERE k='a'")[0]["v"])
        self.assertEqual(self.store.get_kv("b"), {"key": "game-1", "token": "abc"})

    def test_the_janitor_cleans_a_row_saved_by_older_code_and_is_idempotent(self):
        self.store.x("INSERT INTO kv(k, v) VALUES(?, ?)", ("old", f'{{"error": "url: {URL}"}}'))
        self.store.x("INSERT INTO kv(k, v) VALUES(?, ?)", ("fine", '{"note": "monkey=banana"}'))
        self.assertEqual(maintenance.janitor_scrub_secrets(self.store)["fixed"], 1)
        self.assertNotIn(SECRET, self.store.q("SELECT v FROM kv WHERE k='old'")[0]["v"])
        self.assertEqual(self.store.q("SELECT v FROM kv WHERE k='fine'")[0]["v"], '{"note": "monkey=banana"}')
        self.assertEqual(maintenance.janitor_scrub_secrets(self.store)["fixed"], 0)

    def test_run_janitors_includes_the_scrub(self):
        self.store.x("INSERT INTO kv(k, v) VALUES(?, ?)", ("old", f'{{"error": "url: {URL}"}}'))
        res = maintenance.run_janitors(self.store, force=True)
        self.assertEqual(res["janitor_e"]["fixed"], 1)


class TestSaudiKey(unittest.TestCase):
    def test_the_saudi_league_uses_the_providers_real_sport_key(self):
        self.assertEqual(config.SPORTS["KXSAUDIPLGAME"], "soccer_saudi_arabia_pro_league")


if __name__ == "__main__":
    unittest.main()
