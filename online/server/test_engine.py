import tempfile
import unittest
from pathlib import Path

from engine import Engine, GameError, PAIRS


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.engine = Engine(Path(self.tmp.name) / "test.sqlite3")
        self.token, self.player = self.engine.create_player()

    def tearDown(self):
        self.engine.db.close()
        self.tmp.cleanup()

    def test_orders_drive_shared_price_and_server_account(self):
        second_token, second = self.engine.create_player()
        before = self.engine.snapshot(second, 0)["markets"]["EUR/USD"]["price"]
        self.engine.open_position(self.player, "EUR/USD", 1, 500, 20)
        first = self.engine.snapshot(self.player, 1)
        second_view = self.engine.snapshot(second, 1)
        self.assertGreater(first["markets"]["EUR/USD"]["price"], before)
        self.assertEqual(first["markets"]["EUR/USD"]["price"], second_view["markets"]["EUR/USD"]["price"])
        pushed = self.engine.snapshots([self.player, second], 2)
        self.assertEqual(pushed[self.player]["markets"]["EUR/USD"]["price"], pushed[second]["markets"]["EUR/USD"]["price"])
        self.assertTrue(any(row["mine"] for row in pushed[second]["leaderboard"]))
        self.assertEqual(first["account"]["positions"][0]["margin"], 500)
        self.assertLess(first["account"]["cash"], 9500)
        self.assertEqual(self.engine.player_for_token(second_token), second)

    def test_bot_trade_changes_quote_without_random_price_tick(self):
        pair = list(PAIRS)[3]
        before = self.engine.market(pair)["price"]
        for _ in range(10):
            self.engine.bot_tick()
        self.assertNotEqual(before, self.engine.market(pair)["price"])
        self.assertGreater(self.engine.market(pair)["buy_volume"] + self.engine.market(pair)["sell_volume"], 0)

    def test_negative_balance_and_lifetime_liquidation_count(self):
        self.engine.open_position(self.player, "EUR/USD", 1, 9000, 10)
        # A sequence of very large sell orders represents a real gap through
        # the maintenance threshold. The server settles at the observed quote.
        with self.engine.lock, self.engine.db:
            for _ in range(10):
                self.engine._apply_flow("EUR/USD", -1_000_000)
            self.engine._liquidate()
        result = self.engine.snapshot(self.player, 1)
        self.assertEqual(result["account"]["positions"], [])
        self.assertLess(result["account"]["cash"], 0)
        self.assertEqual(result["liquidations"], 1)
        self.engine.reset(self.player)
        self.engine.open_position(self.player, "EUR/USD", 1, 9000, 10)
        with self.engine.lock, self.engine.db:
            for _ in range(10):
                self.engine._apply_flow("EUR/USD", -1_000_000)
            self.engine._liquidate()
        self.assertEqual(self.engine.snapshot(self.player, 1)["liquidations"], 1)

    def test_only_one_reset_per_beijing_day(self):
        self.engine.open_position(self.player, "GBP/USD", -1, 100, 10)
        self.engine.reset(self.player)
        account = self.engine.snapshot(self.player, 1)["account"]
        self.assertEqual(account["cash"], 10000)
        self.assertEqual(account["positions"], [])
        self.assertEqual(account["resetRemaining"], 0)
        with self.assertRaises(GameError):
            self.engine.reset(self.player)

    def test_order_limits_are_enforced_by_server(self):
        with self.assertRaises(GameError):
            self.engine.open_position(self.player, "EUR/USD", 1, 10000, 50)
        with self.assertRaises(GameError):
            self.engine.open_position(self.player, "EUR/USD", 1, -20, 20)
        self.assertEqual(self.engine.snapshot(self.player, 1)["account"]["cash"], 10000)


if __name__ == "__main__":
    unittest.main()
