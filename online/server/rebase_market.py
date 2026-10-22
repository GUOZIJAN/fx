"""One-time market recovery after the 2026-10 penny-price failure.

Run with the server stopped. Back up the SQLite database first. Without
--apply this prints a read-only settlement summary.
"""

import argparse
import json
import sqlite3
import time
from collections import defaultdict

from ashare_engine import CHART_PERIODS, STOCKS, Engine


MARKER = "rebase:2026-10-penny-recovery"
REASON = "行情重整（按原价无手续费结算）"


def rebase(path, apply=False):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        if db.execute("SELECT 1 FROM meta WHERE key=?", (MARKER,)).fetchone():
            raise RuntimeError("This market recovery has already been applied.")
        prices = {r["stock"]: r["price"] for r in db.execute("SELECT stock,price FROM markets")}
        positions = db.execute("SELECT * FROM positions").fetchall()
        totals = defaultdict(float)
        accounts_before = {}
        for r in db.execute("SELECT id,cash FROM players"):
            accounts_before[r["id"]] = r["cash"]
        for pos in positions:
            pnl = pos["side"] * pos["qty"] * (prices[pos["stock"]] - pos["entry"])
            delta = pos["margin"] + pnl
            totals[pos["stock"]] += delta
            accounts_before[pos["player_id"]] += delta
        summary = {"positions": len(positions), "accounts": len(accounts_before),
                   "openOrders": db.execute("SELECT COUNT(*) FROM orders WHERE status='open'").fetchone()[0],
                   "settlementByStock": dict(totals), "oldPrices": prices,
                   "newPrices": {s: p for s, (_, p, _) in STOCKS.items()}}
        if not apply:
            return summary
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM meta WHERE key=?", (MARKER,)).fetchone():
            raise RuntimeError("This market recovery has already been applied.")
        now = time.time()
        # Preserve the former chart in a separate table so the displayed chart
        # starts on the new price scale without losing the old bars from SQLite.
        db.execute("CREATE TABLE IF NOT EXISTS candle_bars_archive AS "
                   "SELECT *, 0.0 AS archived_at FROM candle_bars WHERE 0")
        db.execute("INSERT INTO candle_bars_archive "
                   "SELECT *, ? FROM candle_bars", (now,))
        db.execute("DELETE FROM candle_bars")
        db.execute("UPDATE orders SET status='canceled' WHERE status='open'")
        db.execute("DELETE FROM orders WHERE player_id IS NULL")
        for pos in positions:
            price = prices[pos["stock"]]
            pnl = pos["side"] * pos["qty"] * (price - pos["entry"])
            db.execute("UPDATE players SET cash=cash+? WHERE id=?",
                       (pos["margin"] + pnl, pos["player_id"]))
            db.execute("INSERT INTO trades(player_id,stock,side,qty,price,pnl,reason,closed_at) "
                       "VALUES(?,?,?,?,?,?,?,?)",
                       (pos["player_id"], pos["stock"], pos["side"], pos["qty"],
                        price, pnl, REASON, now))
        db.execute("DELETE FROM positions")
        for stock, (_, seed, _) in STOCKS.items():
            candle = {"t": int(now // 5 * 5), "o": seed, "h": seed,
                      "l": seed, "c": seed}
            db.execute("UPDATE markets SET price=?,previous=?,candles=?,"
                       "buy_volume=0,sell_volume=0 WHERE stock=?",
                       (seed, seed, json.dumps([candle]), stock))
            for period in CHART_PERIODS.values():
                bucket = Engine._bar_bucket(now, period)
                db.execute("INSERT INTO candle_bars VALUES(?,?,?,?,?,?,?)",
                           (stock, period, bucket, seed, seed, seed, seed))
            for key, value in ((f"fair:{stock}", seed),
                               (f"anchor:{stock}", seed),
                               (f"flow:{stock}", json.dumps([int(now // 60), 0]))):
                db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) "
                           "DO UPDATE SET value=excluded.value", (key, str(value)))
        db.execute("INSERT INTO meta(key,value) VALUES(?,?)", (MARKER, str(now)))
        for player_id, expected in accounts_before.items():
            actual = db.execute("SELECT cash FROM players WHERE id=?", (player_id,)).fetchone()[0]
            if abs(actual - expected) > max(.001, abs(expected) * 1e-10):
                raise RuntimeError(f"Equity mismatch for {player_id}")
        db.commit()
        return summary
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("database")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(rebase(args.database, args.apply), ensure_ascii=False, indent=2))
