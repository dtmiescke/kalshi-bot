"""Soccer games have a third market for the draw, which stopped every soccer game from matching a sportsbook event
(0 of 202 live games across 12 leagues). The draw label is now set aside, matched to the book's "Draw" outcome, and
club-name noise (accents, FC/AFC) no longer breaks the team match."""
import unittest

from bot import odds
from bot.matching import match_event, name_score, norm

GAME = {"id": "g1", "home_team": "Chelsea", "away_team": "Manchester United", "commence_time": "2026-10-31T15:30:00Z"}


class TestSoccerMatching(unittest.TestCase):
    def test_a_three_label_soccer_game_matches_and_maps_the_draw(self):
        mapping, ev = match_event(["Chelsea", "Manchester United", "Tie"], "2026-10-31T15:30:00Z", [GAME])
        self.assertIs(ev, GAME)
        self.assertEqual(mapping["Chelsea"], "Chelsea")
        self.assertEqual(mapping["Manchester United"], "Manchester United")
        self.assertEqual(mapping["Tie"], "Draw")

    def test_draw_is_recognised_under_either_name_and_order(self):
        for labels in (["Tie", "Chelsea", "Manchester United"], ["Chelsea", "Draw", "Manchester United"]):
            mapping, ev = match_event(labels, "2026-10-31T15:30:00Z", [GAME])
            self.assertIs(ev, GAME, labels)
            self.assertIn("Draw", mapping.values())

    def test_two_team_games_are_unchanged_and_have_no_draw(self):
        mapping, ev = match_event(["Chelsea", "Manchester United"], "2026-10-31T15:30:00Z", [GAME])
        self.assertIs(ev, GAME)
        self.assertNotIn("Draw", mapping.values())

    def test_malformed_label_sets_never_match(self):
        t = "2026-10-31T15:30:00Z"
        self.assertEqual(match_event(["Chelsea", "Tie", "Draw"], t, [GAME]), (None, None))           # two draws
        self.assertEqual(match_event(["Chelsea", "Manchester United", "Arsenal"], t, [GAME]), (None, None))
        self.assertEqual(match_event(["Chelsea", "Tie"], t, [GAME]), (None, None))                   # one team only

    def test_a_different_game_or_far_start_time_still_does_not_match(self):
        self.assertEqual(match_event(["Chelsea", "Arsenal", "Tie"], "2026-10-31T15:30:00Z", [GAME]), (None, None))
        self.assertEqual(match_event(["Chelsea", "Manchester United", "Tie"], "2026-12-31T15:30:00Z", [GAME]), (None, None))

    def test_accents_club_prefixes_and_abbreviations(self):
        self.assertGreaterEqual(name_score("Atletico", "Atlético Madrid"), 0.9)
        self.assertEqual(name_score("Bournemouth", "AFC Bournemouth"), 1.0)
        self.assertEqual(norm("FC Porto"), "porto")
        self.assertEqual(name_score("PSG", "Paris Saint-Germain"), 1.0)
        self.assertGreaterEqual(name_score("Man City", "Manchester City"), 0.9)

    def test_the_consensus_prices_the_draw_as_its_own_outcome(self):
        event = {"bookmakers": [{"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Chelsea", "price": -110}, {"name": "Manchester United", "price": 300}, {"name": "Draw", "price": 260}]}]}]}
        out = odds.consensus(event)
        self.assertIn("Draw", out)
        self.assertAlmostEqual(sum(p for p, _, _ in out.values()), 1.0, places=2)           # the three fair probabilities sum to one


if __name__ == "__main__":
    unittest.main()
