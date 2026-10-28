"""A chart trader's reads: structure, sweeps, squeezes, divergence, absorption, session."""
import math
import unittest

from bot import expert, hindsight, window_review

T0 = 1_793_188_800 - (1_793_188_800 % 86400) + 14 * 3600        # 14:00 UTC, an hour with the US open in it


def candles(closes, vols=None, spread=0.0002, start=T0 - 300 * 60):
    out = []
    for i, c in enumerate(closes):
        v = 5.0 if vols is None else vols[i]
        out.append({"ts": start + 60 * i, "open": c, "high": c * (1 + spread), "low": c * (1 - spread),
                    "close": c, "volume": v})
    return out


def flat(n=300, p=100_000.0):
    return [p * (1 + 0.0003 * math.sin(i / 3.0)) for i in range(n)]


class TestReads(unittest.TestCase):
    def test_structure_reads_clean_trends_both_ways(self):
        up = [100_000 * (1 + 0.0004 * i) for i in range(300)]
        down = [100_000 * (1 - 0.0004 * i) for i in range(300)]
        self.assertGreater(expert.structure(candles(up)), 0.8)
        self.assertLess(expert.structure(candles(down)), -0.8)
        self.assertLess(abs(expert.structure(candles(flat()))), 0.6)

    def test_sweep_of_the_low_that_is_reclaimed_is_bullish(self):
        c = flat()
        cs = candles(c)
        cs[-3]["low"] = min(x["low"] for x in cs[-35:-5]) * 0.999          # pierces the prior half-hour low
        self.assertEqual(expert.sweep(cs), 1.0)
        cs = candles(c)
        cs[-3]["high"] = max(x["high"] for x in cs[-35:-5]) * 1.001
        self.assertEqual(expert.sweep(cs), -1.0)
        self.assertEqual(expert.sweep(candles(c)), 0.0)

    def test_a_tight_stretch_after_wide_swings_is_a_squeeze(self):
        wide = [100_000 * (1 + 0.004 * math.sin(i / 2.0)) for i in range(200)]
        quiet = [wide[-1] * (1 + 0.00002 * math.sin(i)) for i in range(25)]
        self.assertLessEqual(expert.squeeze_percentile(candles(wide + quiet)), 0.1)
        self.assertGreater(expert.squeeze_percentile(candles(wide)), 0.3)

    def test_price_lower_low_with_stronger_rsi_is_bullish_divergence(self):
        base = [100_000.0] * 240
        drop = [100_000 - 400 * i for i in range(1, 11)]                    # fast fall: RSI collapses
        bounce = [drop[-1] + 150 * i for i in range(1, 11)]
        drift = [bounce[-1] - 200 * i for i in range(1, 11)]                 # slow slide to a lower low
        c = base + drop + bounce + drift
        self.assertLess(min(drift), min(drop) + 1)                          # it really is a lower low
        self.assertEqual(expert.rsi_divergence(candles(c[-300:])), 1.0)

    def test_heavy_volume_with_no_progress_at_the_lows_is_absorption(self):
        c = [100_000 * (1 - 0.0003 * i) for i in range(295)] + [100_000 * (1 - 0.0003 * 295)] * 5
        v = [5.0] * 295 + [30.0] * 5
        self.assertEqual(expert.absorption(candles(c, v, spread=0.00005)), 1.0)
        self.assertEqual(expert.absorption(candles(c, [5.0] * 300, spread=0.00005)), 0.0)


class TestBuild(unittest.TestCase):
    def test_full_set_and_session_flags(self):
        cs = candles(flat())
        f = expert.build(cs, strike=cs[-1]["close"] * 0.999, secs_left=200, now_ts=T0)
        for key in ("ex_structure", "ex_squeeze_pct", "ex_sweep", "ex_rsi_div", "ex_range_pos",
                    "ex_strike_range_pos", "ex_res_sigma", "ex_sup_sigma", "ex_us_open", "ex_session"):
            self.assertIn(key, f)
        self.assertEqual(f["ex_us_open"], 1.0)
        self.assertEqual(f["ex_session"], 2.0)
        self.assertEqual(expert.build(cs, now_ts=T0 - 8 * 3600)["ex_us_open"], 0.0)
        self.assertEqual(expert.build(cs[:50]), {})                            # too little history: no reads

    def test_a_target_sitting_on_the_price_near_expiry_is_pinned(self):
        cs = candles(flat())
        near = expert.build(cs, strike=cs[-1]["close"], secs_left=120, now_ts=T0)
        self.assertEqual(near["ex_pin"], 1.0)
        far = expert.build(cs, strike=cs[-1]["close"], secs_left=600, now_ts=T0)
        self.assertEqual(far["ex_pin"], 0.0)


class TestRulesAndNarrative(unittest.TestCase):
    def test_every_expert_read_is_a_scored_rule(self):
        v = hindsight.votes({"ex_structure": .9, "ex_sweep": -1.0, "ex_rsi_div": 1.0, "ex_absorption": -1.0,
                             "ex_squeeze_pct": .1, "vol_spike_5m": 2.0, "ret_5m": 1.0,
                             "ex_us_open": 1.0, "ret_15m": -1.0, "ex_range_pos": .95, "ex_res_sigma": .3})
        self.assertEqual((v["structure_follow"], v["liquidity_sweep"], v["rsi_divergence"], v["absorption"]),
                         (1, -1, 1, -1))
        self.assertEqual((v["squeeze_breakout"], v["us_open_momentum"], v["range_fade"]), (1, -1, -1))
        quiet = hindsight.votes({})
        for name in ("structure_follow", "liquidity_sweep", "squeeze_breakout", "rsi_divergence",
                     "absorption", "us_open_momentum", "range_fade"):
            self.assertEqual(quiet[name], 0, name)                                # no data, no opinion
            self.assertIn(name, hindsight.LABELS)

    def test_the_window_read_speaks_like_a_trader(self):
        text = window_review.describe("BTC", {"ex_structure": .8, "ex_sweep": 1.0, "ex_squeeze_pct": .1,
                                              "ex_rsi_div": -1.0, "ex_absorption": 1.0, "ex_us_open": 1.0},
                                      .5, .55, 100_100, 100_000)
        for phrase in ("higher highs and lows", "swept the half-hour low and reclaimed it", "volatility squeezed",
                       "bearish RSI divergence", "absorbed at the lows", "US open hour"):
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
