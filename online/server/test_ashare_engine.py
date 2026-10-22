import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ashare_engine import Engine, GameError, STOCKS
from rebase_market import rebase


class AShareEngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.path = Path(self.tmp.name) / "test.sqlite3"
        self.engine = Engine(self.path)
        self.token, self.player = self.engine.create_player()

    def tearDown(self):
        self.engine.db.close()
        self.tmp.cleanup()

    def test_only_three_stocks_and_live_book(self):
        snap = self.engine.snapshot(self.player, 1)
        self.assertEqual(set(STOCKS), set(snap["markets"]))
        for stock in STOCKS:
            self.assertTrue(snap["orderBooks"][stock]["bids"])
            self.assertTrue(snap["orderBooks"][stock]["asks"])
        self.assertEqual(snap["account"]["cash"], 10000)

    def test_ranking_includes_position_profit_and_skips_position_losses(self):
        stock = "300059"
        price = self.engine.market(stock)["price"]
        players = [self.engine.create_player()[1] for _ in range(25)]
        with self.engine.lock, self.engine.db:
            for index, player_id in enumerate(players):
                self.engine.db.execute(
                    "UPDATE players SET cash=? WHERE id=?",
                    (1000 + index * 500, player_id),
                )
            self.engine.db.execute(
                "UPDATE players SET cash=100 WHERE id=?", (players[0],)
            )
            self.engine.db.execute(
                "UPDATE players SET cash=50000 WHERE id=?", (players[1],)
            )
            self.engine.db.execute(
                "INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("rank-winner", players[0], stock, -1, 100, price + 200,
                 100, 1, 0, time.time()),
            )
            self.engine.db.execute(
                "INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("rank-loser", players[1], stock, 1, 100, price + 600,
                 100, 1, 0, time.time()),
            )
        ranking = self.engine.snapshot(self.player, 1)["leaderboard"]
        self.assertEqual(ranking[0]["equity"], 20200)
        self.assertEqual(ranking[0]["nickname"],
                         self.engine.player(players[0])["nickname"])
        self.assertNotIn(self.engine.player(players[1])["nickname"],
                         [entry["nickname"] for entry in ranking])
        self.assertEqual(len(ranking), 10)

    def test_limit_waits_then_matches_at_resting_price(self):
        stock = "300059"
        before = self.engine.market(stock)["price"]
        limit = round(before * .98, 2)
        placed = self.engine.open_position(self.player, stock, 1, 500, 5,
                                           "limit", limit)
        self.assertEqual(placed["status"], "open")
        self.assertEqual(self.engine.market(stock)["price"], before)
        account = self.engine.snapshot(self.player, 1)["account"]
        self.assertLess(account["available"], account["cash"])
        with self.engine.db:
            self.engine.db.execute(
                "DELETE FROM orders WHERE stock=? AND player_id IS NULL AND direction=1",
                (stock,),
            )
        _, seller = self.engine.create_player()
        self.engine.open_position(seller, stock, -1, 500, 5, "limit", limit)
        account = self.engine.snapshot(self.player, 1)["account"]
        self.assertTrue(account["positions"])
        self.assertEqual(self.engine.market(stock)["price"], limit)
        self.assertEqual(account["positions"][0]["entry"], limit)

    def test_market_uses_opposite_book_and_cancel_releases_cash(self):
        stock = "300750"
        snap = self.engine.snapshot(self.player, 1)
        best_ask = snap["orderBooks"][stock]["asks"][0]["price"]
        result = self.engine.open_position(self.player, stock, 1, 500, 10)
        self.assertGreater(result["filledQty"], 0)
        self.assertEqual(result["avgPrice"], best_ask)
        pos = self.engine.snapshot(self.player, 1)["account"]["positions"][0]
        self.assertEqual(pos["entry"], best_ask)
        pending = self.engine.close_position(self.player, pos["id"], "limit",
                                            round(best_ask * 1.1, 2))
        self.assertEqual(pending["status"], "open")
        self.engine.cancel_order(self.player, pending["orderId"])
        self.assertEqual(self.engine.snapshot(self.player, 1)["account"]["orders"][0]["status"],
                         "canceled")
        closed = self.engine.close_position(self.player, pos["id"])
        self.assertGreater(closed["filledQty"], 0)
        self.assertFalse(self.engine.snapshot(self.player, 1)["account"]["positions"])

    def test_penny_price_keeps_both_sides_of_book_and_can_buy(self):
        stock = "300750"
        with self.engine.lock, self.engine.db:
            self.engine.db.execute(
                "UPDATE markets SET price=? WHERE stock=?", (.02, stock)
            )
            self.engine.db.execute(
                "UPDATE meta SET value=? WHERE key=?", ("0.0201", f"fair:{stock}")
            )
        self.engine.bot_tick()
        book = self.engine.snapshot(self.player, 1)["orderBooks"][stock]
        self.assertTrue(book["bids"])
        self.assertTrue(book["asks"])
        self.assertEqual(book["bids"][0]["price"], .02)
        self.assertEqual(book["asks"][0]["price"], .02)
        self.assertGreater(sum(level["qty"] for level in book["bids"]), 5_000_000)
        self.assertEqual(self.engine.market(stock)["price"], .02)
        for _ in range(10):
            self.engine.bot_tick()
        stable_book = self.engine.snapshot(self.player, 1)["orderBooks"][stock]
        self.assertTrue(stable_book["bids"] and stable_book["asks"])
        self.assertEqual(self.engine.market(stock)["price"], .02)
        self.assertEqual(self.engine._validate_type("limit", .03, .02), .03)
        result = self.engine.open_position(self.player, stock, 1, 500, 20)
        self.assertGreater(result["filledQty"], 0)

    def test_zero_price_is_rejected_and_repaired_on_restart(self):
        stock = "300750"
        opened = self.engine.open_position(self.player, stock, 1, 500, 10)
        self.assertGreater(opened["filledQty"], 0)
        positions_before = len(self.engine.snapshot(self.player, 1)["account"]["positions"])
        with self.assertRaisesRegex(GameError, "至少 ¥0.01"):
            self.engine._validate_type("limit", .001, .01)
        with self.assertRaisesRegex(GameError, "至少 ¥0.01"):
            self.engine._submit(None, stock, 1, "bot", 1, 0, "limit")
        with self.engine.lock, self.engine.db:
            self.engine.db.execute(
                "UPDATE markets SET price=0,previous=0 WHERE stock=?", (stock,)
            )
            self.engine.db.execute(
                "UPDATE meta SET value='0.0031' WHERE key=?", (f"fair:{stock}",)
            )
            self.engine.db.execute(
                "UPDATE orders SET price=0 WHERE id=(SELECT id FROM orders "
                "WHERE stock=? AND status='open' LIMIT 1)", (stock,)
            )
        self.engine._repair_price_floor()
        self.assertEqual(len(self.engine.snapshot(self.player, 1)["account"]["positions"]),
                         positions_before)
        with self.engine.db:
            self.engine.db.execute(
                "UPDATE markets SET price=0,previous=0 WHERE stock=?", (stock,)
            )
        self.engine.db.close()
        self.engine = Engine(self.path)
        market = self.engine.market(stock)
        self.assertEqual((market["price"], market["previous"]), (.01, .01))
        self.assertEqual(self.engine._fair_price(stock), .01)
        self.assertEqual(self.engine.db.execute(
            "SELECT COUNT(*) FROM orders WHERE status='open' AND price=0"
        ).fetchone()[0], 0)
        _, newcomer = self.engine.create_player()
        self.assertGreater(self.engine.open_position(
            newcomer, stock, 1, 500, 20)["filledQty"], 0)

    def test_no_fixed_margin_or_notional_cap(self):
        stock = "300750"
        with self.engine.lock, self.engine.db:
            self.engine.db.execute(
                "UPDATE players SET cash=? WHERE id=?", (30_000, self.player)
            )
        result = self.engine.open_position(self.player, stock, 1, 15_000, 10)
        self.assertGreater(result["filledQty"], 0)
        self.assertIn(result["status"], ("filled", "canceled"))
        with self.assertRaisesRegex(GameError, "可用余额不足"):
            self.engine.open_position(self.player, stock, 1, 100_000, 10)

    def test_three_daily_resets_and_invalid_limit(self):
        with self.assertRaises(GameError):
            self.engine.open_position(self.player, "300059", 1, 500, 5,
                                      "limit", .01)
        self.assertEqual(self.engine.snapshot(self.player, 1)["account"]["resetRemaining"], 3)
        for remaining in (2, 1, 0):
            self.engine.reset(self.player)
            account = self.engine.snapshot(self.player, 1)["account"]
            self.assertEqual(account["cash"], 10000)
            self.assertEqual(account["resetRemaining"], remaining)
        with self.assertRaises(GameError):
            self.engine.reset(self.player)
        with patch("ashare_engine.today", return_value="2099-01-01"):
            self.assertEqual(self.engine.snapshot(self.player, 1)["account"]["resetRemaining"], 3)
            self.engine.reset(self.player)
            self.assertEqual(self.engine.snapshot(self.player, 1)["account"]["resetRemaining"], 2)

    def test_gap_liquidation_can_leave_negative_cash(self):
        stock = "300059"
        opened = self.engine.open_position(self.player, stock, 1, 9000, 10)
        self.assertGreater(opened["filledQty"], 0)
        pos = self.engine.snapshot(self.player, 1)["account"]["positions"][0]
        gap_price = round(pos["entry"] * .05, 2)
        with self.engine.lock, self.engine.db:
            self.engine.db.execute(
                "DELETE FROM orders WHERE stock=? AND player_id IS NULL", (stock,)
            )
            self.engine.db.execute(
                "UPDATE markets SET price=? WHERE stock=?", (gap_price, stock)
            )
            self.engine.db.execute(
                "UPDATE meta SET value=? WHERE key=?", (str(gap_price), f"fair:{stock}")
            )
            for second in range(1, 7):
                bucket = int(time.time()) - second
                self.engine.db.execute(
                    "INSERT OR REPLACE INTO candle_bars VALUES(?,?,?,?,?,?,?)",
                    (stock, 1, bucket, gap_price, gap_price, gap_price, gap_price),
                )
            self.engine._submit(None, stock, 1, "bot", pos["qty"] * 2,
                                gap_price, "limit")
            self.engine._liquidate()
        account = self.engine.snapshot(self.player, 1)["account"]
        self.assertFalse(account["positions"])
        self.assertLess(account["cash"], 0)
        self.assertEqual(self.engine.snapshot(self.player, 1)["liquidations"], 1)

    def test_idle_bots_have_two_way_prices_without_upward_drift(self):
        history = {
            stock: [self.engine.market(stock)["price"]] for stock in STOCKS
        }
        for _ in range(400):
            self.engine.bot_tick()
            for stock in STOCKS:
                history[stock].append(self.engine.market(stock)["price"])
        for stock, (_, seed, _) in STOCKS.items():
            prices = history[stock]
            self.assertLess(max(abs(price / seed - 1) for price in prices), .03)
            moves = [b - a for a, b in zip(prices, prices[1:])]
            self.assertGreater(sum(move > 0 for move in moves), 40)
            self.assertGreater(sum(move < 0 for move in moves), 40)

    def test_player_trade_moves_quote_anchor(self):
        stock = "300059"
        before = self.engine._fair_price(stock)
        order = self.engine.open_position(self.player, stock, 1, 500, 5)
        self.assertGreater(order["filledQty"], 0)
        self.assertGreater(self.engine._fair_price(stock), before)

    def test_bot_skips_outlier_orders_but_players_can_match_them(self):
        stock = "300059"
        _, seller = self.engine.create_player()
        with self.engine.lock, self.engine.db:
            self.engine.db.execute(
                "DELETE FROM orders WHERE stock=? AND player_id IS NULL", (stock,)
            )
        outlier = self.engine.open_position(seller, stock, -1, 500, 5,
                                            "limit", 20.0)
        self.assertEqual(outlier["status"], "open")
        self.engine.bot_tick()
        order = self.engine.db.execute(
            "SELECT status,remaining FROM orders WHERE id=?", (outlier["orderId"],)
        ).fetchone()
        self.assertEqual(order["status"], "open")
        self.assertGreater(order["remaining"], 0)
        self.assertGreater(self.engine.market(stock)["price"], 24)
        bought = self.engine.open_position(self.player, stock, 1, 500, 5)
        self.assertGreater(bought["filledQty"], 0)
        self.assertEqual(bought["avgPrice"], 20.0)

    def test_bot_does_not_fill_old_limit_after_market_gap(self):
        stock = "300059"
        with self.engine.lock, self.engine.db:
            self.engine.db.execute("DELETE FROM orders WHERE stock=?", (stock,))
            self.engine.db.execute(
                "UPDATE markets SET price=20,previous=20 WHERE stock=?", (stock,)
            )
            self.engine.db.execute(
                "UPDATE meta SET value='19.5' WHERE key=?", (f"fair:{stock}",)
            )
            old, _, _, _ = self.engine._submit(
                self.player, stock, -1, "open", 100, 18, "limit",
                5, 1, "old-short", "开仓",
            )
            self.engine.db.execute(
                "UPDATE markets SET price=58,previous=57.9 WHERE stock=?", (stock,)
            )
            self.engine.db.execute(
                "DELETE FROM candle_bars WHERE stock=? AND period=1", (stock,)
            )
            self.engine.db.execute(
                "INSERT INTO candle_bars VALUES(?,?,?,?,?,?,?)",
                (stock, 1, int(time.time()), 58, 58, 58, 58),
            )
        self.engine.bot_tick()
        resting = self.engine.db.execute(
            "SELECT status,remaining FROM orders WHERE id=?", (old["id"],)
        ).fetchone()
        self.assertEqual((resting["status"], resting["remaining"]), ("open", 100))
        self.assertEqual(self.engine.market(stock)["price"], 58)

    def test_bot_quotes_at_established_price_after_old_orders(self):
        stock = "300059"
        with self.engine.lock, self.engine.db:
            self.engine.db.execute("DELETE FROM orders WHERE stock=?", (stock,))
            self.engine.db.execute(
                "UPDATE markets SET price=58,previous=57.9 WHERE stock=?", (stock,)
            )
            self.engine.db.execute(
                "UPDATE meta SET value='19.5' WHERE key=?", (f"fair:{stock}",)
            )
            self.engine.db.execute(
                "DELETE FROM candle_bars WHERE stock=? AND period=1", (stock,)
            )
            for second in range(6):
                self.engine.db.execute(
                    "INSERT INTO candle_bars VALUES(?,?,?,?,?,?,?)",
                    (stock, 1, int(time.time()) - 3600 - second,
                     58, 58, 58, 58),
                )
            old, _, _, _ = self.engine._submit(
                self.player, stock, -1, "open", 100, 18, "limit",
                5, 1, "old-short", "开仓",
            )
        self.engine.bot_tick()
        bids = self.engine.snapshot(self.player, 1)["orderBooks"][stock]["bids"]
        bot_bids = [row["price"] for row in bids if row["owner"] == "机器人"]
        self.assertTrue(bot_bids)
        self.assertGreater(min(bot_bids), 56)
        resting = self.engine.db.execute(
            "SELECT status,remaining FROM orders WHERE id=?", (old["id"],)
        ).fetchone()
        self.assertEqual((resting["status"], resting["remaining"]), ("open", 100))

    def test_bot_does_not_take_old_bid_after_price_gap(self):
        stock = "300059"
        with self.engine.lock, self.engine.db:
            self.engine.db.execute("DELETE FROM orders WHERE stock=?", (stock,))
            self.engine.db.execute(
                "UPDATE markets SET price=20,previous=20 WHERE stock=?", (stock,)
            )
            self.engine.db.execute(
                "UPDATE meta SET value='17.5' WHERE key=?", (f"fair:{stock}",)
            )
            old, _, _, _ = self.engine._submit(
                self.player, stock, 1, "open", 100, 18, "limit",
                5, 1, "old-long", "开仓",
            )
            self.engine.db.execute(
                "UPDATE markets SET price=58,previous=57.9 WHERE stock=?", (stock,)
            )
            self.engine.db.execute(
                "DELETE FROM candle_bars WHERE stock=? AND period=1", (stock,)
            )
            self.engine.db.execute(
                "INSERT INTO candle_bars VALUES(?,?,?,?,?,?,?)",
                (stock, 1, int(time.time()), 58, 58, 58, 58),
            )
        self.engine.bot_tick()
        resting = self.engine.db.execute(
            "SELECT status,remaining FROM orders WHERE id=?", (old["id"],)
        ).fetchone()
        self.assertEqual((resting["status"], resting["remaining"]), ("open", 100))
        self.assertEqual(self.engine.market(stock)["price"], 58)

    def test_bot_quote_must_also_be_near_reference(self):
        stock = "300059"
        with self.engine.lock, self.engine.db:
            self.engine.db.execute("DELETE FROM orders WHERE stock=?", (stock,))
            self.engine.db.execute(
                "UPDATE markets SET price=58,previous=57.9 WHERE stock=?", (stock,)
            )
            self.engine.db.execute(
                "DELETE FROM candle_bars WHERE stock=? AND period=1", (stock,)
            )
            for second in range(6):
                self.engine.db.execute(
                    "INSERT INTO candle_bars VALUES(?,?,?,?,?,?,?)",
                    (stock, 1, int(time.time()) - second, 58, 58, 58, 58),
                )
            player_bid, _, _, _ = self.engine._submit(
                self.player, stock, 1, "open", 100, 58, "limit",
                5, 1, "player-long", "开仓",
            )
            self.engine._submit(None, stock, -1, "bot", 100, 20, "limit")
        resting = self.engine.db.execute(
            "SELECT status,remaining FROM orders WHERE id=?", (player_bid["id"],)
        ).fetchone()
        self.assertEqual((resting["status"], resting["remaining"]), ("open", 100))
        self.assertEqual(self.engine.market(stock)["price"], 58)

    def test_reference_uses_recent_median_when_book_is_one_sided(self):
        stock = "300059"
        with self.engine.lock, self.engine.db:
            self.engine.db.execute("DELETE FROM orders WHERE stock=?", (stock,))
            now = int(time.time())
            for second in range(1, 7):
                self.engine.db.execute(
                    "INSERT OR REPLACE INTO candle_bars VALUES(?,?,?,?,?,?,?)",
                    (stock, 1, now - second, 25, 25, 25, 25),
                )
            self.assertEqual(self.engine._reference_price(stock), 25)
            self.assertFalse(self.engine._bot_can_trade(stock, 20))
            self.assertTrue(self.engine._bot_can_trade(stock, 25.5))

    def test_high_player_print_does_not_instantly_liquidate_short(self):
        stock = "300059"
        self.engine.open_position(self.player, stock, -1, 1000, 10)
        short_id = self.engine.snapshot(self.player, 1)["account"]["positions"][0]["id"]
        with self.engine.lock, self.engine.db:
            self.engine.db.execute(
                "DELETE FROM orders WHERE stock=? AND player_id IS NULL", (stock,)
            )
        _, buyer = self.engine.create_player()
        high = round(self.engine.market(stock)["price"] * 1.18, 2)
        order = self.engine.open_position(buyer, stock, 1, 500, 5, "limit", high)
        self.assertEqual(order["status"], "open")
        self.engine.bot_tick()
        self.assertEqual(self.engine.db.execute(
            "SELECT status FROM orders WHERE id=?", (order["orderId"],)
        ).fetchone()[0], "open")
        _, seller = self.engine.create_player()
        self.engine.open_position(seller, stock, -1, 500, 5)
        self.assertGreaterEqual(
            max(bar["h"] for bar in json.loads(
                self.engine.market(stock)["candles"])), high,
        )
        self.assertIsNotNone(self.engine.db.execute(
            "SELECT id FROM positions WHERE id=?", (short_id,)
        ).fetchone())

    def test_player_flow_can_move_reference_without_fixed_cap(self):
        stock = "300059"
        initial = self.engine._fair_price(stock)
        with self.engine.lock, self.engine.db:
            for _ in range(200):
                self.engine._record_trade(stock, initial, 1_000,
                                          1, player_involved=True)
        self.assertGreater(self.engine._fair_price(stock), initial * 1.35)
        self.engine.bot_tick()
        book = self.engine.snapshot(self.player, 1)["orderBooks"][stock]
        self.assertTrue(book["bids"] and book["asks"])
        before_sells = self.engine._fair_price(stock)
        with self.engine.lock, self.engine.db:
            for _ in range(400):
                self.engine._record_trade(stock, initial, 1_000,
                                          -1, player_involved=True)
        self.assertLess(self.engine._fair_price(stock), before_sells)
        self.assertLess(self.engine._fair_price(stock), initial * .65)
        self.engine.bot_tick()
        book = self.engine.snapshot(self.player, 1)["orderBooks"][stock]
        self.assertTrue(book["bids"] and book["asks"])

    def test_market_rebase_preserves_equity_and_trade_history(self):
        stock = "300059"
        self.engine.open_position(self.player, stock, 1, 1000, 5)
        before = self.engine.snapshot(self.player, 1)["account"]["equity"]
        self.assertEqual(rebase(self.path)["positions"], 1)
        self.assertEqual(self.engine.snapshot(self.player, 1)["account"]["equity"], before)
        self.engine.db.close()
        rebase(self.path, apply=True)
        self.engine = Engine(self.path)
        after = self.engine.snapshot(self.player, 1)["account"]
        self.assertAlmostEqual(after["equity"], before, places=2)
        self.assertFalse(after["positions"])
        self.assertEqual(after["trades"][0]["reason"],
                         "行情重整（按原价无手续费结算）")
        self.assertAlmostEqual(self.engine._fair_price(stock), STOCKS[stock][1])
        self.assertLess(abs(self.engine.market(stock)["price"] / STOCKS[stock][1] - 1), .01)
        self.assertGreater(self.engine.db.execute(
            "SELECT COUNT(*) FROM candle_bars_archive"
        ).fetchone()[0], 0)
        with self.assertRaises(RuntimeError):
            rebase(self.path, apply=True)

    def test_chart_history_aggregates_and_pages_by_time(self):
        stock = "300059"
        base = int(time.time()) + 10
        first = self.engine.market(stock)["price"]
        with self.engine.lock, self.engine.db:
            with patch("ashare_engine.time.time", return_value=base + .1):
                self.engine._record_trade(stock, first + 1, 2, 1)
            with patch("ashare_engine.time.time", return_value=base + .7):
                self.engine._record_trade(stock, first - 1, 3, -1)
            with patch("ashare_engine.time.time", return_value=base + 1.1):
                self.engine._record_trade(stock, first + 2, 1, 1)
        seconds = self.engine.candle_history(stock, "sec")["candles"]
        first_bar = next(bar for bar in seconds if bar["t"] == base)
        self.assertEqual((first_bar["o"], first_bar["h"],
                          first_bar["l"], first_bar["c"]),
                         (first, first + 1, first - 1, first - 1))
        self.assertEqual(self.engine.candle_history(
            stock, "sec", before=base + 1)["candles"][-1]["t"], base)
        for frame in ("hour", "fiveHour", "day"):
            bars = self.engine.candle_history(stock, frame)["candles"]
            self.assertTrue(bars)
            self.assertEqual(bars[-1]["c"], first + 2)
        with self.engine.lock, self.engine.db:
            for second in range(2, 36):
                with patch("ashare_engine.time.time",
                           return_value=base + second + .1):
                    self.engine._record_trade(stock, first + second / 100, 1, 1)
        newest = self.engine.candle_history(stock, "sec", limit=30)
        self.assertEqual(len(newest["candles"]), 30)
        self.assertTrue(newest["hasMore"])
        older = self.engine.candle_history(
            stock, "sec", before=newest["candles"][0]["t"], limit=30)
        self.assertTrue(older["candles"])
        self.assertLess(older["candles"][-1]["t"], newest["candles"][0]["t"])


if __name__ == "__main__":
    unittest.main()
