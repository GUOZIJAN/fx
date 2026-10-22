"""Server-authoritative A-share simulation with a price-time order book.

Prices change only when orders execute. This is a game, not an exchange feed.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import secrets
import sqlite3
import statistics
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


STOCKS = {
    "300059": ("东方财富", 25.00, 2),
    "300750": ("宁德时代", 400.00, 2),
    "300308": ("中际旭创", 280.00, 2),
}
START_CASH = 10_000.0
RESET_DAILY_LIMIT = 3
BOT_COUNT = 14
TZ = ZoneInfo("Asia/Shanghai")
CHART_PERIODS = {"sec": 1, "hour": 3600, "fiveHour": 18_000, "day": 86_400}
CHART_HISTORY_LIMIT = 400
BOT_QUOTE_NOTIONAL = (60_000, 75_000, 90_000)
BOT_MAX_DEVIATION = .08
REFERENCE_MEDIAN_BARS = 21
_UNSET = object()


def today():
    return datetime.now(TZ).date().isoformat()


def rounded(value):
    return round(float(value) + 1e-9, 2)


class GameError(ValueError):
    pass


class Engine:
    def __init__(self, path: str | Path):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.lock = threading.RLock()
        self._schema()
        self._repair_price_floor()
        self.bot_tick()

    def _schema(self):
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS players (
                    id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL,
                    nickname TEXT NOT NULL, cash REAL NOT NULL,
                    reset_day TEXT NOT NULL DEFAULT '', reset_used INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS players_cash_rank ON players(cash DESC);
                CREATE TABLE IF NOT EXISTS positions (
                    id TEXT PRIMARY KEY, player_id TEXT NOT NULL, stock TEXT NOT NULL,
                    side INTEGER NOT NULL, qty INTEGER NOT NULL, entry REAL NOT NULL,
                    margin REAL NOT NULL, leverage INTEGER NOT NULL, fee REAL NOT NULL,
                    opened_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS positions_player ON positions(player_id);
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY, player_id TEXT, stock TEXT NOT NULL,
                    direction INTEGER NOT NULL, action TEXT NOT NULL, position_id TEXT,
                    quantity INTEGER NOT NULL, remaining INTEGER NOT NULL,
                    price REAL, order_type TEXT NOT NULL, margin_per_share REAL NOT NULL,
                    leverage INTEGER NOT NULL, reason TEXT NOT NULL,
                    created_at REAL NOT NULL, status TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS orders_book ON orders(stock,status,direction,price,created_at);
                CREATE INDEX IF NOT EXISTS orders_player ON orders(player_id,created_at);
                CREATE INDEX IF NOT EXISTS orders_open_closes_by_position
                    ON orders(position_id) WHERE action='close' AND status='open';
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, player_id TEXT NOT NULL,
                    stock TEXT NOT NULL, side INTEGER NOT NULL, qty INTEGER NOT NULL,
                    price REAL NOT NULL, pnl REAL NOT NULL, reason TEXT NOT NULL,
                    closed_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS trades_player_recent ON trades(player_id,id DESC);
                CREATE TABLE IF NOT EXISTS liquidated_players (
                    player_id TEXT PRIMARY KEY, first_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS markets (
                    stock TEXT PRIMARY KEY, price REAL NOT NULL, previous REAL NOT NULL,
                    candles TEXT NOT NULL, buy_volume REAL NOT NULL DEFAULT 0,
                    sell_volume REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS candle_bars (
                    stock TEXT NOT NULL, period INTEGER NOT NULL, bucket INTEGER NOT NULL,
                    o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL,
                    PRIMARY KEY(stock,period,bucket)
                );
                CREATE INDEX IF NOT EXISTS candle_bars_expiry ON candle_bars(period,bucket);
            """)
            for stock, (_, price, _) in STOCKS.items():
                self.db.execute(
                    "INSERT OR IGNORE INTO markets(stock,price,previous,candles) VALUES(?,?,?,?)",
                    (stock, price, price, json.dumps([self._candle(price)])),
                )
            self.db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('tick','0')")
            for stock, (_, price, _) in STOCKS.items():
                self.db.execute(
                    "INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)",
                    (f"fair:{stock}", str(price)),
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)",
                    (f"anchor:{stock}", str(self.market(stock)["price"])),
                )
                now = time.time()
                for period in CHART_PERIODS.values():
                    bucket = self._bar_bucket(now, period)
                    self.db.execute(
                        "INSERT OR IGNORE INTO candle_bars VALUES(?,?,?,?,?,?,?)",
                        (stock, period, bucket, price, price, price, price),
                    )

    def _repair_price_floor(self):
        # Earlier versions could round sub-cent limit orders to ¥0.00. Repair
        # only invalid live quotes and pending orders; preserve player balances,
        # positions and settled trade history.
        with self.lock, self.db:
            for stock in STOCKS:
                row = self.market(stock)
                price = row["price"] if math.isfinite(row["price"]) and row["price"] >= .01 else .01
                previous = (row["previous"] if math.isfinite(row["previous"])
                            and row["previous"] >= .01 else .01)
                if price != row["price"] or previous != row["previous"]:
                    self.db.execute(
                        "UPDATE markets SET price=?,previous=? WHERE stock=?",
                        (price, previous, stock),
                    )
                fair = self._fair_price(stock)
                if not math.isfinite(fair) or fair < .01:
                    self.db.execute("UPDATE meta SET value='0.01' WHERE key=?",
                                    (f"fair:{stock}",))
            self.db.execute(
                "UPDATE orders SET status='canceled' WHERE status='open' "
                "AND price IS NOT NULL AND price<0.01"
            )

    @staticmethod
    def _candle(price, bucket=None):
        return {"t": bucket or int(time.time() // 5 * 5),
                "o": price, "h": price, "l": price, "c": price}

    @staticmethod
    def _bar_bucket(timestamp, period):
        # The epoch offset anchors daily bars to midnight in China.
        return int((timestamp + 28_800) // period * period - 28_800)

    def candle_history(self, stock, timeframe, before=None, limit=240):
        if stock not in STOCKS:
            raise GameError("不支持这只股票。")
        if timeframe not in CHART_PERIODS:
            raise GameError("不支持该K线周期。")
        try:
            limit = int(limit)
            before = int(before) if before is not None else None
        except (TypeError, ValueError, OverflowError):
            raise GameError("时间范围无效。") from None
        limit = max(30, min(CHART_HISTORY_LIMIT, limit))
        period = CHART_PERIODS[timeframe]
        with self.lock:
            if before is None:
                rows = self.db.execute(
                    "SELECT bucket,o,h,l,c FROM candle_bars WHERE stock=? AND period=? "
                    "ORDER BY bucket DESC LIMIT ?",
                    (stock, period, limit + 1),
                ).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT bucket,o,h,l,c FROM candle_bars WHERE stock=? AND period=? "
                    "AND bucket<? ORDER BY bucket DESC LIMIT ?",
                    (stock, period, before, limit + 1),
                ).fetchall()
        return {
            "stock": stock, "timeframe": timeframe,
            "hasMore": len(rows) > limit,
            "candles": [
                {"t": row["bucket"], "o": row["o"], "h": row["h"],
                 "l": row["l"], "c": row["c"]}
                for row in reversed(rows[:limit])
            ],
        }

    @staticmethod
    def _hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def create_player(self):
        token, player_id = secrets.token_urlsafe(32), secrets.token_hex(12)
        with self.lock, self.db:
            self.db.execute("INSERT INTO players VALUES(?,?,?,?,?,?,?)",
                            (player_id, self._hash(token), f"股民 {player_id[:4].upper()}",
                             START_CASH, "", 0, time.time()))
        return token, player_id

    def player_for_token(self, token):
        if not token:
            return None
        with self.lock:
            row = self.db.execute("SELECT id FROM players WHERE token_hash=?",
                                  (self._hash(token),)).fetchone()
            return row["id"] if row else None

    def player(self, player_id):
        row = self.db.execute("SELECT * FROM players WHERE id=?", (player_id,)).fetchone()
        if not row:
            raise GameError("账号不存在，请重新进入游戏。")
        return row

    def market(self, stock):
        if stock not in STOCKS:
            raise GameError("不支持这只股票。")
        return self.db.execute("SELECT * FROM markets WHERE stock=?", (stock,)).fetchone()

    def _fee_rate(self, leverage):
        return 0.00008 + leverage * 0.000002

    def _reserved(self, player_id):
        rows = self.db.execute(
            "SELECT remaining,margin_per_share,leverage,price FROM orders "
            "WHERE player_id=? AND action='open' AND status='open'", (player_id,)
        )
        return sum(row["remaining"] * (row["margin_per_share"] +
                   (row["price"] or 0) * self._fee_rate(row["leverage"]) * 1.02)
                   for row in rows)

    def _pnl(self, position, price=None):
        price = self.market(position["stock"])["price"] if price is None else price
        return position["side"] * position["qty"] * (price - position["entry"])

    def _account(self, player_id):
        player = self.player(player_id)
        positions = self.db.execute(
            "SELECT * FROM positions WHERE player_id=? ORDER BY opened_at DESC",
            (player_id,),
        ).fetchall()
        rows = []
        floating = margin = 0.0
        for pos in positions:
            pnl = self._pnl(pos)
            floating += pnl
            margin += pos["margin"]
            rows.append({"id": pos["id"], "stock": pos["stock"], "side": pos["side"],
                         "qty": pos["qty"], "entry": pos["entry"],
                         "margin": rounded(pos["margin"]), "leverage": pos["leverage"],
                         "pnl": rounded(pnl), "openedAt": pos["opened_at"]})
        orders = self.db.execute(
            "SELECT * FROM orders WHERE player_id=? ORDER BY "
            "CASE WHEN status='open' THEN 0 ELSE 1 END, created_at DESC LIMIT 80",
            (player_id,),
        ).fetchall()
        trade_rows = self.db.execute(
            "SELECT stock,side,qty,price,pnl,reason,closed_at FROM trades "
            "WHERE player_id=? ORDER BY id DESC LIMIT 12", (player_id,),
        ).fetchall()
        day = today()
        reserved = self._reserved(player_id)
        return {
            "id": player_id, "nickname": player["nickname"],
            "cash": rounded(player["cash"]), "available": rounded(player["cash"] - reserved),
            "reserved": rounded(reserved), "equity": rounded(player["cash"] + margin + floating),
            "floating": rounded(floating), "margin": rounded(margin), "positions": rows,
            "resetRemaining": max(0, RESET_DAILY_LIMIT -
                                  (player["reset_used"] if player["reset_day"] == day else 0)),
            "orders": [self._order_json(row) for row in orders],
            "trades": [{"stock": r["stock"], "side": r["side"], "qty": r["qty"],
                        "price": r["price"], "pnl": rounded(r["pnl"]),
                        "reason": r["reason"], "at": r["closed_at"]} for r in trade_rows],
        }

    @staticmethod
    def _order_json(row):
        return {"id": row["id"], "stock": row["stock"], "direction": row["direction"],
                "action": row["action"], "quantity": row["quantity"],
                "remaining": row["remaining"], "price": row["price"],
                "orderType": row["order_type"], "status": row["status"],
                "createdAt": row["created_at"], "positionId": row["position_id"]}

    def _public_snapshot(self, online):
        markets = {}
        books = {}
        for stock in STOCKS:
            row = self.market(stock)
            markets[stock] = {"price": row["price"], "previous": row["previous"],
                              "candles": json.loads(row["candles"]),
                              "buyVolume": rounded(row["buy_volume"]),
                              "sellVolume": rounded(row["sell_volume"])}
            book_rows = self.db.execute(
                "SELECT id,player_id,direction,remaining,price,created_at FROM orders "
                "WHERE stock=? AND status='open' AND price IS NOT NULL", (stock,),
            ).fetchall()
            bids = sorted((r for r in book_rows if r["direction"] == 1),
                          key=lambda r: (-r["price"], r["created_at"], r["id"]))
            asks = sorted((r for r in book_rows if r["direction"] == -1),
                          key=lambda r: (r["price"], r["created_at"], r["id"]))
            books[stock] = {
                "bids": [self._book_row(r) for r in bids],
                "asks": [self._book_row(r) for r in asks],
            }
        marks = {stock: markets[stock]["price"] for stock in STOCKS}
        equity = {}
        names = {}
        for pos in self.db.execute(
            "SELECT pos.player_id,pos.stock,pos.margin,pos.side,pos.qty,pos.entry,"
            "p.nickname,p.cash FROM positions pos "
            "JOIN players p ON p.id=pos.player_id"
        ):
            player_id = pos["player_id"]
            if player_id not in equity:
                equity[player_id] = pos["cash"]
                names[player_id] = pos["nickname"]
            equity[player_id] += (
                pos["margin"] + pos["side"] * pos["qty"] *
                (marks[pos["stock"]] - pos["entry"])
            )
        # Every player without a position has equity equal to cash. Only the
        # ten richest of them can enter the top ten; include all position
        # holders because their floating P/L may put them anywhere.
        candidates = [
            {"id": player_id, "nickname": names[player_id], "equity": value}
            for player_id, value in equity.items()
        ]
        cash_only = 0
        for player in self.db.execute(
            "SELECT id,nickname,cash FROM players ORDER BY cash DESC"
        ):
            if player["id"] in equity:
                continue
            candidates.append({"id": player["id"],
                               "nickname": player["nickname"],
                               "equity": player["cash"]})
            cash_only += 1
            if cash_only == 10:
                break
        ranking = [
            {"id": r["id"], "nickname": r["nickname"],
             "equity": rounded(r["equity"])}
            for r in heapq.nlargest(10, candidates, key=lambda r: r["equity"])
        ]
        liquidations = self.db.execute(
            "SELECT COUNT(*) FROM liquidated_players"
        ).fetchone()[0]
        return {"type": "snapshot", "serverTime": time.time(),
                "stocks": [{"id": k, "name": v[0], "digits": v[2]}
                           for k, v in STOCKS.items()],
                "markets": markets, "orderBooks": books, "ranking": ranking,
                "online": online, "bots": BOT_COUNT, "liquidations": liquidations}

    @staticmethod
    def _book_row(row):
        return {"id": row["id"], "price": row["price"],
                "qty": row["remaining"], "owner": "玩家" if row["player_id"] else "机器人"}

    def _personal_snapshot(self, player_id, public):
        ranking = [{"nickname": r["nickname"], "equity": r["equity"],
                    "mine": r["id"] == player_id} for r in public["ranking"]]
        return {**{k: v for k, v in public.items() if k != "ranking"},
                "account": self._account(player_id), "leaderboard": ranking}

    def snapshot(self, player_id, online):
        with self.lock:
            return self._personal_snapshot(player_id, self._public_snapshot(online))

    def snapshots(self, player_ids, online):
        with self.lock:
            public = self._public_snapshot(online)
            return {pid: self._personal_snapshot(pid, public) for pid in player_ids}

    def _record_trade(self, stock, price, qty, direction, player_involved=False):
        if not math.isfinite(price) or price < .01:
            raise GameError("成交价格异常，请稍后再试。")
        row = self.market(stock)
        old = row["price"]
        traded_at = time.time()
        bucket = int(traded_at // 5 * 5)
        candles = json.loads(row["candles"])
        if candles[-1]["t"] != bucket:
            candles.append(self._candle(old, bucket))
        candles[-1]["h"] = max(candles[-1]["h"], price)
        candles[-1]["l"] = min(candles[-1]["l"], price)
        candles[-1]["c"] = price
        value = price * qty
        self.db.execute(
            "UPDATE markets SET price=?,previous=?,candles=?,buy_volume=buy_volume+?,"
            "sell_volume=sell_volume+? WHERE stock=?",
            (price, old, json.dumps(candles[-90:], separators=(",", ":")),
             value if direction == 1 else 0, value if direction == -1 else 0, stock),
        )
        for period in CHART_PERIODS.values():
            bucket = self._bar_bucket(traded_at, period)
            self.db.execute(
                "INSERT INTO candle_bars(stock,period,bucket,o,h,l,c) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(stock,period,bucket) DO UPDATE SET "
                "h=MAX(h,excluded.h),l=MIN(l,excluded.l),c=excluded.c",
                (stock, period, bucket, old, max(old, price),
                 min(old, price), price),
            )
        if player_involved:
            fair = self._fair_price(stock)
            # Diminishing impact models deeper liquidity for large fills. There
            # is no per-minute quota or fixed ceiling on cumulative order flow.
            impact = .0025 * math.log1p(value / 10_000)
            new_fair = max(.01, fair * math.exp(impact * direction))
            self.db.execute(
                "UPDATE meta SET value=? WHERE key=?",
                (str(round(new_fair, 6)), f"fair:{stock}"),
            )

    def _fair_price(self, stock):
        return float(self.db.execute(
            "SELECT value FROM meta WHERE key=?", (f"fair:{stock}",)
        ).fetchone()["value"])

    def _recent_trade_median(self, stock):
        # Use the most recent seconds that actually traded, even after a quiet
        # period. Otherwise the bot falls back to a stale quote anchor and can
        # take an old limit order at a price far from the established market.
        rows = self.db.execute(
            "SELECT c FROM candle_bars WHERE stock=? AND period=1 "
            "ORDER BY bucket DESC LIMIT ?",
            (stock, REFERENCE_MEDIAN_BARS),
        ).fetchall()
        prices = [r["c"] for r in rows if math.isfinite(r["c"]) and r["c"] >= .01]
        return statistics.median(prices) if len(prices) >= 5 else None

    def _reference_price(self, stock, exclude_ids=(), recent=_UNSET):
        # Exclude the two orders being matched so an outlier cannot set its
        # own reference. The live bid/ask midpoint is preferred; recent traded
        # prices take over if the book is missing or visibly dislocated.
        excluded = tuple(exclude_ids)[:2] + ("",) * (2 - len(exclude_ids))
        bid = self.db.execute(
            "SELECT price FROM orders WHERE stock=? AND status='open' "
            "AND direction=1 AND price>=0.01 AND id NOT IN (?,?) "
            "ORDER BY price DESC,created_at ASC LIMIT 1",
            (stock, *excluded),
        ).fetchone()
        ask = self.db.execute(
            "SELECT price FROM orders WHERE stock=? AND status='open' "
            "AND direction=-1 AND price>=0.01 AND id NOT IN (?,?) "
            "ORDER BY price ASC,created_at ASC LIMIT 1",
            (stock, *excluded),
        ).fetchone()
        if recent is _UNSET:
            recent = self._recent_trade_median(stock)
        if bid and ask and bid["price"] <= ask["price"]:
            middle = (bid["price"] + ask["price"]) / 2
            if math.isfinite(middle) and middle >= .01:
                if recent is None or abs(middle - recent) <= recent * .10:
                    return middle
        return recent if recent is not None else self._fair_price(stock)

    def _bot_can_trade(self, stock, price, exclude_ids=(), recent=_UNSET,
                       bot_price=None):
        reference = self._reference_price(stock, exclude_ids, recent)
        if not math.isfinite(reference) or reference < .01:
            return False
        tolerance = max(reference * BOT_MAX_DEVIATION, .01) + 1e-9
        # The last print cannot set the reference by itself, but a large gap
        # between it and the robust reference means bots should wait for price
        # discovery instead of reviving stale orders.
        last = self.market(stock)["price"]
        last_tolerance = max(last * BOT_MAX_DEVIATION, .01) + 1e-9
        return (abs(last - reference) <= tolerance and
                abs(price - reference) <= tolerance and
                abs(price - last) <= last_tolerance and
                (bot_price is None or
                 (abs(bot_price - reference) <= tolerance and
                  abs(bot_price - last) <= last_tolerance)))

    @staticmethod
    def _bot_choice(tick, stock, channel):
        value = hashlib.blake2s(
            f"{tick}:{stock}:{channel}".encode(), digest_size=8
        ).digest()
        return int.from_bytes(value, "big") / 2**64

    @staticmethod
    def _bot_quote_pair(base, spread):
        # Below ten cents, one tick is a large percentage move. Let both sides
        # rest at the same cent; _best_opposite keeps bot quotes from self-filling.
        middle = max(1, round(base * 100))
        if base < .10:
            return middle / 100, middle / 100
        gap = max(1, round(base * spread * 100))
        return max(1, middle - gap) / 100, (middle + gap) / 100

    def _can_fill(self, order, qty, price):
        if order["action"] == "bot":
            return True
        if order["action"] == "open":
            need = qty * (order["margin_per_share"] +
                          price * self._fee_rate(order["leverage"]))
            return self.player(order["player_id"])["cash"] + 1e-6 >= need
        pos = self.db.execute("SELECT qty FROM positions WHERE id=? AND player_id=?",
                              (order["position_id"], order["player_id"])).fetchone()
        return bool(pos and pos["qty"] >= qty)

    def _apply_fill(self, order, qty, price):
        if order["action"] == "bot":
            return 0.0
        player_id = order["player_id"]
        fee = rounded(price * qty * self._fee_rate(order["leverage"]))
        if order["action"] == "open":
            margin = order["margin_per_share"] * qty
            self.db.execute("UPDATE players SET cash=cash-? WHERE id=?",
                            (margin + fee, player_id))
            pos = self.db.execute("SELECT * FROM positions WHERE id=?",
                                  (order["position_id"],)).fetchone()
            if pos:
                new_qty = pos["qty"] + qty
                entry = (pos["entry"] * pos["qty"] + price * qty) / new_qty
                self.db.execute(
                    "UPDATE positions SET qty=?,entry=?,margin=margin+?,fee=fee+? WHERE id=?",
                    (new_qty, entry, margin, fee, pos["id"]),
                )
            else:
                self.db.execute(
                    "INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (order["position_id"], player_id, order["stock"],
                     order["direction"], qty, price, margin, order["leverage"],
                     fee, time.time()),
                )
            return 0.0
        pos = self.db.execute("SELECT * FROM positions WHERE id=?",
                              (order["position_id"],)).fetchone()
        margin_return = pos["margin"] * qty / pos["qty"]
        pnl = pos["side"] * qty * (price - pos["entry"]) - fee
        self.db.execute("UPDATE players SET cash=cash+? WHERE id=?",
                        (margin_return + pnl, player_id))
        if qty == pos["qty"]:
            self.db.execute("DELETE FROM positions WHERE id=?", (pos["id"],))
        else:
            self.db.execute("UPDATE positions SET qty=qty-?,margin=margin-? WHERE id=?",
                            (qty, margin_return, pos["id"]))
        self.db.execute(
            "INSERT INTO trades(player_id,stock,side,qty,price,pnl,reason,closed_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (player_id, order["stock"], pos["side"], qty, price, pnl,
             order["reason"], time.time()),
        )
        if order["reason"] == "爆仓":
            self.db.execute(
                "INSERT OR IGNORE INTO liquidated_players(player_id,first_at) VALUES(?,?)",
                (player_id, time.time()),
            )
        return rounded(pnl)

    def _best_opposite(self, incoming):
        direction = -incoming["direction"]
        if incoming["direction"] == 1:
            price_clause = "price<=?" if incoming["price"] is not None else "1=1"
            ordering = "price ASC"
        else:
            price_clause = "price>=?" if incoming["price"] is not None else "1=1"
            ordering = "price DESC"
        args = [incoming["stock"], direction, incoming["id"]]
        if incoming["price"] is not None:
            args.append(incoming["price"])
        rows = self.db.execute(
            f"SELECT * FROM orders WHERE stock=? AND status='open' AND direction=? "
            f"AND id<>? AND price IS NOT NULL AND {price_clause} "
            f"ORDER BY {ordering},created_at ASC,id ASC", args
        )
        recent = None
        for row in rows:
            if incoming["player_id"] and row["player_id"] == incoming["player_id"]:
                continue
            if (incoming["action"] == "bot" and incoming["order_type"] == "limit"
                    and row["action"] == "bot"):
                continue
            if not math.isfinite(row["price"]) or row["price"] < .01:
                continue
            if incoming["action"] == "bot" or row["action"] == "bot":
                if recent is None:
                    recent = self._recent_trade_median(incoming["stock"])
                if not self._bot_can_trade(
                    incoming["stock"], row["price"],
                    (incoming["id"], row["id"]), recent,
                    (incoming if incoming["action"] == "bot" else row)["price"],
                ):
                    continue
            return row
        return None

    def _match(self, order_id):
        fills = []
        total_pnl = 0.0
        for _ in range(200):
            incoming = self.db.execute("SELECT * FROM orders WHERE id=?",
                                       (order_id,)).fetchone()
            if incoming["remaining"] <= 0 or incoming["status"] != "open":
                break
            maker = self._best_opposite(incoming)
            if maker is None:
                break
            qty = min(incoming["remaining"], maker["remaining"])
            price = maker["price"]
            if not math.isfinite(price) or price < .01:
                self.db.execute("UPDATE orders SET status='canceled' WHERE id=?",
                                (maker["id"],))
                continue
            if not self._can_fill(maker, qty, price):
                self.db.execute("UPDATE orders SET status='canceled' WHERE id=?",
                                (maker["id"],))
                continue
            if not self._can_fill(incoming, qty, price):
                self.db.execute("UPDATE orders SET status='canceled' WHERE id=?",
                                (incoming["id"],))
                break
            total_pnl += self._apply_fill(incoming, qty, price)
            self._apply_fill(maker, qty, price)
            for row in (incoming, maker):
                remaining = row["remaining"] - qty
                self.db.execute("UPDATE orders SET remaining=?,status=? WHERE id=?",
                                (remaining, "filled" if not remaining else "open", row["id"]))
            self._record_trade(
                incoming["stock"], price, qty, incoming["direction"],
                bool(incoming["player_id"] and incoming["reason"] != "爆仓"),
            )
            fills.append((qty, price))
        incoming = self.db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if incoming["order_type"] == "market" and incoming["status"] == "open":
            self.db.execute("UPDATE orders SET status='canceled' WHERE id=?", (order_id,))
        return fills, rounded(total_pnl)

    def _submit(self, player_id, stock, direction, action, qty, price, order_type,
                margin_per_share=0.0, leverage=1, position_id=None, reason=""):
        if price is not None and (not math.isfinite(price) or price < .01):
            raise GameError("委托价格至少 ¥0.01。")
        order_id = secrets.token_hex(12)
        self.db.execute(
            "INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, player_id, stock, direction, action, position_id,
             qty, qty, price, order_type, margin_per_share, leverage,
             reason, time.time(), "open"),
        )
        fills, pnl = self._match(order_id)
        row = self.db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        filled_qty = sum(qty for qty, _ in fills)
        avg = sum(qty * price for qty, price in fills) / filled_qty if filled_qty else None
        return row, filled_qty, avg, pnl

    @staticmethod
    def _validate_type(order_type, limit_price, reference):
        if order_type not in ("market", "limit"):
            raise GameError("请选择市价或限价委托。")
        if order_type == "market":
            return None
        try:
            price = float(limit_price)
        except (TypeError, ValueError, OverflowError):
            raise GameError("请输入有效的限价。") from None
        if (not math.isfinite(price) or price <= 0 or
                abs(price - reference) > max(reference * .20, .01) + 1e-9):
            raise GameError("限价需在现价上下 20%（至少一个价格档位）范围内。")
        price = round(price + 1e-9, 2)
        if price < .01:
            raise GameError("限价至少 ¥0.01。")
        return price

    def open_position(self, player_id, stock, side, margin, leverage,
                      order_type="market", limit_price=None):
        if stock not in STOCKS or side not in (1, -1):
            raise GameError("交易参数无效。")
        try:
            margin, leverage = float(margin), int(leverage)
        except (TypeError, ValueError, OverflowError):
            raise GameError("请输入有效保证金和杠杆。") from None
        if not math.isfinite(margin) or margin < 20 or not 1 <= leverage <= 50:
            raise GameError("保证金至少 ¥20，杠杆限 1–50 倍。")
        with self.lock, self.db:
            available = self.player(player_id)["cash"] - self._reserved(player_id)
            if margin > available:
                raise GameError("可用余额不足以支付保证金和手续费。")
            reference = self.market(stock)["price"]
            price = self._validate_type(order_type, limit_price, reference)
            basis = price or reference
            qty = math.floor(margin * leverage / basis)
            if qty < 1:
                raise GameError("保证金不足以买入 1 股，请增加保证金或杠杆。")
            count = self.db.execute(
                "SELECT COUNT(*) FROM positions WHERE player_id=?", (player_id,)
            ).fetchone()[0]
            pending = self.db.execute(
                "SELECT COUNT(*) FROM orders WHERE player_id=? AND action='open' "
                "AND status='open' AND position_id NOT IN "
                "(SELECT id FROM positions WHERE player_id=?)", (player_id, player_id)
            ).fetchone()[0]
            if count + pending >= 5:
                raise GameError("最多同时持有或等待 5 笔仓位。")
            reserve = margin + basis * qty * self._fee_rate(leverage) * 1.02
            if available < reserve:
                raise GameError("可用余额不足以支付保证金和手续费。")
            row, filled, avg, _ = self._submit(
                player_id, stock, side, "open", qty, price, order_type,
                margin / qty, leverage, secrets.token_hex(12), "开仓",
            )
            if order_type == "market" and not filled:
                raise GameError("目前没有可成交的对手委托。")
            self._liquidate()
        state = "已挂单" if row["status"] == "open" else "已成交"
        return {"message": f"{STOCKS[stock][0]}{state} {filled}/{qty} 股。" +
                (f" 均价 ¥{avg:.2f}。" if avg else ""),
                "orderId": row["id"], "status": row["status"],
                "filledQty": filled, "avgPrice": avg}

    def close_position(self, player_id, pos_id, order_type="market", limit_price=None):
        with self.lock, self.db:
            pos = self.db.execute(
                "SELECT * FROM positions WHERE id=? AND player_id=?", (pos_id, player_id)
            ).fetchone()
            if not pos:
                raise GameError("仓位不存在或已经平仓。")
            pending = self.db.execute(
                "SELECT COUNT(*) FROM orders WHERE position_id=? AND action='close' "
                "AND status='open'", (pos_id,)
            ).fetchone()[0]
            if pending:
                raise GameError("这笔仓位已有待成交的平仓委托，请先撤单。")
            price = self._validate_type(
                order_type, limit_price, self.market(pos["stock"])["price"]
            )
            row, filled, avg, pnl = self._submit(
                player_id, pos["stock"], -pos["side"], "close", pos["qty"],
                price, order_type, 0.0, pos["leverage"], pos["id"], "手动平仓",
            )
            if order_type == "market" and not filled:
                raise GameError("目前没有可成交的对手委托。")
            self._liquidate()
        state = "已挂单" if row["status"] == "open" else "已成交"
        return {"message": f"平仓{state} {filled}/{pos['qty']} 股。" +
                (f" 本次盈亏 {'+' if pnl >= 0 else ''}¥{pnl:.2f}。" if filled else ""),
                "orderId": row["id"], "status": row["status"],
                "filledQty": filled, "avgPrice": avg, "pnl": pnl}

    def cancel_order(self, player_id, order_id):
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM orders WHERE id=? AND player_id=?", (order_id, player_id)
            ).fetchone()
            if not row or row["status"] != "open":
                raise GameError("委托不存在或已结束。")
            self.db.execute("UPDATE orders SET status='canceled' WHERE id=?", (order_id,))
        return {"message": "委托已撤销，未成交部分的保证金已释放。"}

    def _liquidate(self):
        attempted = set()
        for _ in range(50):
            positions = self.db.execute("SELECT * FROM positions ORDER BY opened_at").fetchall()
            marks = {stock: self._reference_price(stock) for stock in STOCKS}
            victim = next((p for p in positions
                           if p["id"] not in attempted and
                           self._pnl(p, marks[p["stock"]]) <= -.8 * p["margin"]), None)
            if victim is None:
                break
            attempted.add(victim["id"])
            self.db.execute(
                "UPDATE orders SET status='canceled' WHERE position_id=? "
                "AND action='close' AND status='open'", (victim["id"],)
            )
            _, filled, _, _ = self._submit(
                victim["player_id"], victim["stock"], -victim["side"],
                "close", victim["qty"], None, "market", 0.0,
                victim["leverage"], victim["id"], "爆仓",
            )
            if filled:
                attempted.clear()

    def reset(self, player_id):
        day = today()
        with self.lock, self.db:
            player = self.player(player_id)
            used = player["reset_used"] if player["reset_day"] == day else 0
            if used >= RESET_DAILY_LIMIT:
                raise GameError("今天的重启人生机会已用完，明天 00:00（北京时间）恢复。")
            self.db.execute("UPDATE orders SET status='canceled' WHERE player_id=? "
                            "AND status='open'", (player_id,))
            self.db.execute("DELETE FROM positions WHERE player_id=?", (player_id,))
            self.db.execute("DELETE FROM trades WHERE player_id=?", (player_id,))
            self.db.execute(
                "UPDATE players SET cash=?,reset_day=?,reset_used=? WHERE id=?",
                (START_CASH, day, used + 1, player_id),
            )
        return {"message": "人生已重启，资金恢复为 ¥10,000。"}

    def rename(self, player_id, nickname):
        nickname = str(nickname).strip()
        if not 2 <= len(nickname) <= 16 or any(ord(c) < 32 for c in nickname):
            raise GameError("昵称需要 2–16 个字符。")
        with self.lock, self.db:
            self.db.execute("UPDATE players SET nickname=? WHERE id=?",
                            (nickname, player_id))
        return {"message": "昵称已更新。"}

    def bot_tick(self):
        """Makers quote around a player-responsive anchor; prices move only on fills."""
        with self.lock, self.db:
            tick = int(self.db.execute(
                "SELECT value FROM meta WHERE key='tick'"
            ).fetchone()["value"]) + 1
            self.db.execute("UPDATE meta SET value=? WHERE key='tick'", (str(tick),))
            if tick % 900 == 0:
                self.db.execute(
                    "DELETE FROM candle_bars WHERE period=1 AND bucket<?",
                    (int(time.time()) - 7 * 86_400,),
                )
            self.db.execute("DELETE FROM orders WHERE player_id IS NULL")
            for index, stock in enumerate(STOCKS):
                last = self.market(stock)["price"]
                fair = self._fair_price(stock)
                recent = self._recent_trade_median(stock)
                if recent is not None and abs(last - recent) <= max(
                        recent * BOT_MAX_DEVIATION, .01):
                    base = recent
                elif abs(last - fair) <= max(fair * BOT_MAX_DEVIATION, .01):
                    base = fair
                else:
                    # Preserve the uncapped effect of player order flow on
                    # displayed quotes. The fill guard below keeps these
                    # dislocated quotes from trading until prices agree.
                    base = fair
                spread_shift = (self._bot_choice(tick, stock, "spread") - .5) * .0012
                for level, notional in enumerate(BOT_QUOTE_NOTIONAL, start=1):
                    spread = .0015 + level * .0015 + spread_shift
                    # Quote depth is measured in yuan, so falling prices produce
                    # more shares instead of an empty penny-stock order book.
                    qty = max(3, int(notional / base))
                    bid, ask = self._bot_quote_pair(base, spread)
                    for direction, quote in ((1, bid), (-1, ask)):
                        self._submit(None, stock, direction, "bot", qty, quote, "limit")
                # Deterministic, balanced bot decisions supply activity without
                # making the midprice follow a fixed staircase.
                if base < .10:
                    continue
                last = self.market(stock)["price"]
                deviation = (last - base) / base
                buy_chance = max(.25, min(.75, .5 - deviation * 25))
                direction = 1 if self._bot_choice(tick, stock, "side") < buy_chance else -1
                self._submit(None, stock, direction, "bot",
                             max(1, int(3000 / base)), None, "market")
            self._liquidate()
