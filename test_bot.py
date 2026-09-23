import os
import unittest
import tempfile
import sqlite3
from PIL import Image

import database
import analytics
import renderer
import keyboards
import api_client


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()
        database.init_db(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_bets_flow(self):
        user_id = 12345
        bet_id = database.db_add_bet(user_id, "100", "1X2", "1", 20.0, odds=2.5, db_path=self.db_path)
        self.assertIsNotNone(bet_id)

        profit = database.db_settle_bet(user_id, bet_id, "win", db_path=self.db_path)
        self.assertEqual(profit, 30.0)

        total, prof, count, wins, roi = database.db_summary(user_id, db_path=self.db_path)
        self.assertEqual(total, 20.0)
        self.assertEqual(prof, 30.0)
        self.assertEqual(count, 1)
        self.assertEqual(wins, 1)
        self.assertEqual(roi, 150.0)

    def test_predictions_flow(self):
        user_id = 12345
        pid = database.db_add_prediction(user_id, "200", "Home", "Away", "1X2", "1", 55.0, db_path=self.db_path)
        self.assertIsNotNone(pid)

        outcome = database.prediction_outcome("1X2", "1", 2, 1)
        self.assertEqual(outcome, "win")

        outcome_loss = database.prediction_outcome("BTTS", "Oui", 1, 0)
        self.assertEqual(outcome_loss, "loss")


class TestAnalytics(unittest.TestCase):
    def test_clamp_and_normalize(self):
        self.assertEqual(analytics.clamp(150), 100.0)
        self.assertEqual(analytics.clamp(-10), 0.0)
        a, b, c = analytics.normalize3(50, 50, 0)
        self.assertAlmostEqual(a, 50.0)
        self.assertAlmostEqual(b, 50.0)
        self.assertAlmostEqual(c, 0.0)

    def test_poisson(self):
        matrix = analytics.poisson_matrix(1.5, 1.2)
        self.assertIsNotNone(matrix)
        markets = analytics.poisson_markets(matrix)
        self.assertIn("home", markets)
        self.assertIn("btts", markets)
        scores = analytics.likely_scores(matrix, limit=3)
        self.assertEqual(len(scores), 3)

    def test_build_model(self):
        home_form = [
            {"gf": 2, "ga": 0, "result": "V", "opponent": "A"},
            {"gf": 1, "ga": 1, "result": "N", "opponent": "B"}
        ]
        away_form = [
            {"gf": 0, "ga": 2, "result": "D", "opponent": "C"},
            {"gf": 1, "ga": 0, "result": "V", "opponent": "D"}
        ]
        model = analytics.build_model(home_form, away_form)
        self.assertIsNotNone(model)
        self.assertIn("home_xg", model)
        self.assertIn("final", model)


class TestRenderer(unittest.TestCase):
    def test_render_dashboard(self):
        data = {
            "match": {
                "id": "123",
                "homeTeam": {"name": "PSG"},
                "awayTeam": {"name": "OM"},
                "competition": {"name": "Ligue 1"},
                "status": "FINISHED"
            },
            "model": {
                "final": (45.0, 30.0, 25.0),
                "markets": {"btts": 55.0, "over15": 75.0, "over25": 50.0, "over35": 25.0, "under25": 50.0, "under35": 75.0},
                "home_xg": 1.8,
                "away_xg": 1.1,
                "matrix": analytics.poisson_matrix(1.8, 1.1)
            },
            "standings_home": {"position": 1, "points": 45, "gf": 40, "ga": 15},
            "standings_away": {"position": 2, "points": 40, "gf": 35, "ga": 20},
            "home_form": [{"result": "V"}, {"result": "V"}],
            "away_form": [{"result": "D"}, {"result": "V"}]
        }
        img = renderer.render_dashboard(data)
        self.assertIsInstance(img, Image.Image)
        self.assertEqual(img.size, (1024, 1536))

    def test_render_screen(self):
        img = renderer.render_screen("TEST TITLE", "Test Subtitle", [
            {"kind": "card", "heading": "CARD", "height": 200, "rows": [("Label", "Value")]}
        ])
        self.assertIsInstance(img, Image.Image)


class TestKeyboards(unittest.TestCase):
    def test_keyboards_construction(self):
        kb_main = keyboards.main_menu()
        self.assertIsNotNone(kb_main)
        kb_match = keyboards.match_keyboard("123")
        self.assertIsNotNone(kb_match)


class TestApiClient(unittest.TestCase):
    def test_tsdb_conversion(self):
        event = {
            "idEvent": "999",
            "strHomeTeam": "Real Madrid",
            "strAwayTeam": "Barcelona",
            "strLeague": "La Liga",
            "strStatus": "FT",
            "intHomeScore": "2",
            "intAwayScore": "1",
            "dateEvent": "2025-03-01",
            "strTime": "21:00:00"
        }
        match = api_client.tsdb_to_match(event)
        self.assertEqual(match["id"], "TSDB-999")
        self.assertEqual(match["homeTeam"]["name"], "Real Madrid")
        self.assertEqual(match["score"]["fullTime"]["home"], 2.0)


if __name__ == "__main__":
    unittest.main()
