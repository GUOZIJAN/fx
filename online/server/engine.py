"""Server-authoritative, order-flow driven FX game state.

No market-price randomizer exists here: every quote change comes from a player
order, a forced close, or a named bot order. SQLite is the source of truth.
"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


PAIRS = {
    "EUR/USD": ("欧元 / 美元", 1.08742, 5),
    "GBP/USD": ("英镑 / 美元", 1.27486, 5),
    "USD/JPY": ("美元 / 日元", 149.382, 3),
    "USD/CHF": ("美元 / 瑞郎", 0.88521, 5),
    "EUR/CHF": ("欧元 / 瑞郎", 0.94170, 5),
    "AUD/USD": ("澳元 / 美元", 0.65432, 5),
    "USD/CAD": ("美元 / 加元", 1.37185, 5),
}
START_CASH = 10_000.0
LIQUIDITY = 10_000_000.0
MAX_NOTIONAL = 100_000.0
BOT_COUNT = 14
TZ = ZoneInfo("Asia/Shanghai")


def today() -> str:
    return datetime.now(TZ).date().isoformat()


def rounded(value: float) -> float:
    return round(value + 1e-10, 2)


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

    def _schema(self):
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS players (
                    id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL,
                    nickname TEXT NOT NULL, cash REAL NOT NULL,
                    reset_day TEXT NOT NULL DEFAULT '', reset_used INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS positions (
                    id TEXT PRIMARY KEY, player_id TEXT NOT NULL, pair TEXT NOT NULL,
                    side INTEGER NOT NULL, margin REAL NOT NULL, leverage INTEGER NOT NULL,
                    notional REAL NOT NULL, entry REAL NOT NULL, fee REAL NOT NULL,
                    opened_at REAL NOT NULL,
                    FOREIGN KEY(player_id) REFERENCES players(id)
                );
                CREATE INDEX IF NOT EXISTS positions_player ON positions(player_id);
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, player_id TEXT NOT NULL,
                    pair TEXT NOT NULL, side INTEGER NOT NULL, pnl REAL NOT NULL,
                    reason TEXT NOT NULL, closed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS liquidated_players (
                    player_id TEXT PRIMARY KEY, first_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS markets (
                    pair TEXT PRIMARY KEY, price REAL NOT NULL, previous REAL NOT NULL,
                    candles TEXT NOT NULL, buy_volume REAL NOT NULL DEFAULT 0,
                    sell_volume REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS bots (
                    id INTEGER PRIMARY KEY, pair TEXT NOT NULL, side INTEGER NOT NULL DEFAULT 0,
                    notional REAL NOT NULL DEFAULT 0, opened_tick INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            for pair, (_, price, _) in PAIRS.items():
                self.db.execute("INSERT OR IGNORE INTO markets(pair,price,previous,candles) VALUES(?,?,?,?)",
                                (pair, price, price, json.dumps([self._candle(price)])))
            for i in range(BOT_COUNT):
                self.db.execute("INSERT OR IGNORE INTO bots(id,pair) VALUES(?,?)", (i, list(PAIRS)[i % len(PAIRS)]))
            self.db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('liquidations','0')")
            self.db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('tick','0')")

    @staticmethod
    def _candle(price, bucket=None):
        return {"t": bucket or int(time.time() // 5 * 5), "o": price, "h": price, "l": price, "c": price}

    def create_player(self):
        token = secrets.token_urlsafe(32)
        player_id = secrets.token_hex(12)
        nickname = f"交易员 {player_id[:4].upper()}"
        with self.lock, self.db:
            self.db.execute("INSERT INTO players VALUES(?,?,?,?,?,?,?)",
                            (player_id, self._hash(token), nickname, START_CASH, '', 0, time.time()))
        return token, player_id

    @staticmethod
    def _hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def player_for_token(self, token):
        if not token:
            return None
        with self.lock:
            row = self.db.execute("SELECT id FROM players WHERE token_hash=?", (self._hash(token),)).fetchone()
            return row["id"] if row else None

    def player(self, player_id):
        row = self.db.execute("SELECT * FROM players WHERE id=?", (player_id,)).fetchone()
        if not row:
            raise GameError("账号不存在，请刷新页面。")
        return row

    def market(self, pair):
        row = self.db.execute("SELECT * FROM markets WHERE pair=?", (pair,)).fetchone()
        if not row:
            raise GameError("不支持这个货币对。")
        return row

    def _price(self, pair):
        return self.market(pair)["price"]

    def _pnl(self, position, price=None):
        price = self._price(position["pair"]) if price is None else price
        return position["side"] * position["notional"] * (price / position["entry"] - 1)

    def _account(self, player_id):
        player = self.player(player_id)
        positions = self.db.execute("SELECT * FROM positions WHERE player_id=? ORDER BY opened_at DESC", (player_id,)).fetchall()
        rows = []
        floating = 0.0
        margin = 0.0
        for pos in positions:
            pnl = self._pnl(pos)
            floating += pnl
            margin += pos["margin"]
            rows.append({"id": pos["id"], "pair": pos["pair"], "side": pos["side"],
                         "entry": pos["entry"], "margin": pos["margin"], "leverage": pos["leverage"],
                         "notional": pos["notional"], "pnl": rounded(pnl), "openedAt": pos["opened_at"]})
        day = today()
        trades = self.db.execute("SELECT pair,side,pnl,reason,closed_at FROM trades WHERE player_id=? ORDER BY id DESC LIMIT 8", (player_id,)).fetchall()
        return {"id": player_id, "nickname": player["nickname"], "cash": rounded(player["cash"]),
                "equity": rounded(player["cash"] + margin + floating), "floating": rounded(floating),
                "margin": rounded(margin), "positions": rows,
                "resetRemaining": 1 - (player["reset_used"] if player["reset_day"] == day else 0),
                "trades": [{"pair": r["pair"], "side": r["side"], "pnl": rounded(r["pnl"]),
                            "reason": r["reason"], "at": r["closed_at"]} for r in trades]}

    def _public_snapshot(self, online):
        markets = {}
        for row in self.db.execute("SELECT * FROM markets"):
            markets[row["pair"]] = {"price": row["price"], "previous": row["previous"],
                                     "candles": json.loads(row["candles"]),
                                     "buyVolume": rounded(row["buy_volume"]),
                                     "sellVolume": rounded(row["sell_volume"])}
        players = list(self.db.execute("SELECT id,nickname,cash FROM players"))
        equity_by_player = {row["id"]: row["cash"] for row in players}
        for pos in self.db.execute("SELECT * FROM positions"):
            equity_by_player[pos["player_id"]] += pos["margin"] + self._pnl(pos)
        ranking = [{"id": row["id"], "nickname": row["nickname"],
                    "equity": rounded(equity_by_player[row["id"]])} for row in players]
        ranking.sort(key=lambda item: item["equity"], reverse=True)
        total = self.db.execute("SELECT COUNT(*) FROM liquidated_players").fetchone()[0]
        return {"type": "snapshot", "serverTime": time.time(), "pairs":
                [{"id": k, "name": v[0], "digits": v[2]} for k, v in PAIRS.items()],
                "markets": markets, "ranking": ranking[:10], "online": online,
                "bots": BOT_COUNT, "liquidations": total}

    def _personal_snapshot(self, player_id, public):
        ranking = [{"nickname": r["nickname"], "equity": r["equity"], "mine": r["id"] == player_id}
                   for r in public["ranking"]]
        return {**{key: value for key, value in public.items() if key != "ranking"},
                "account": self._account(player_id), "leaderboard": ranking}

    def snapshot(self, player_id, online):
        with self.lock:
            return self._personal_snapshot(player_id, self._public_snapshot(online))

    def snapshots(self, player_ids, online):
        with self.lock:
            public = self._public_snapshot(online)
            return {player_id: self._personal_snapshot(player_id, public) for player_id in player_ids}

    def _apply_flow(self, pair, signed_notional):
        row = self.market(pair)
        old = row["price"]
        # Capped logarithmic impact: a $100k order moves ~0.8% at $10m depth.
        move = max(-0.025, min(0.025, signed_notional * 0.8 / LIQUIDITY))
        new = old * math.exp(move)
        candles = json.loads(row["candles"])
        bucket = int(time.time() // 5 * 5)
        if candles[-1]["t"] != bucket:
            candles.append(self._candle(old, bucket))
        candles[-1]["h"] = max(candles[-1]["h"], new)
        candles[-1]["l"] = min(candles[-1]["l"], new)
        candles[-1]["c"] = new
        self.db.execute("UPDATE markets SET price=?,previous=?,candles=?,buy_volume=buy_volume+?,sell_volume=sell_volume+? WHERE pair=?",
                        (new, old, json.dumps(candles[-90:], separators=(",", ":")),
                         max(0, signed_notional), max(0, -signed_notional), pair))
        return new

    def open_position(self, player_id, pair, side, margin, leverage):
        if pair not in PAIRS or side not in (1, -1):
            raise GameError("交易参数无效。")
        try:
            margin, leverage = float(margin), int(leverage)
        except (TypeError, ValueError, OverflowError):
            raise GameError("请输入有效保证金和杠杆。") from None
        if not math.isfinite(margin) or not 20 <= margin <= 10_000 or not 1 <= leverage <= 50:
            raise GameError("保证金限 $20–$10,000，杠杆限 1–50 倍。")
        notional = margin * leverage
        if notional > MAX_NOTIONAL:
            raise GameError("单笔名义仓位上限 $100,000。")
        fee = rounded(notional * (0.00008 + leverage * 0.000002))
        with self.lock, self.db:
            player = self.player(player_id)
            count = self.db.execute("SELECT COUNT(*) FROM positions WHERE player_id=?", (player_id,)).fetchone()[0]
            if count >= 5:
                raise GameError("最多同时持有 5 笔仓位。")
            if player["cash"] < margin + fee:
                raise GameError("可用余额不足以支付保证金和手续费。")
            self.db.execute("UPDATE players SET cash=cash-? WHERE id=?", (margin + fee, player_id))
            traded_price = self._apply_flow(pair, side * notional)
            entry = traded_price * (1 + side * 0.0002)
            pos_id = secrets.token_hex(12)
            self.db.execute("INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (pos_id, player_id, pair, side, margin, leverage, notional, entry, fee, time.time()))
            self._liquidate()
        return {"message": f"{pair} {'做多' if side == 1 else '做空'}成功，手续费 ${fee:.2f}。"}

    def _settle(self, pos, reason, price):
        pnl = self._pnl(pos, price)
        self.db.execute("UPDATE players SET cash=cash+? WHERE id=?", (pos["margin"] + pnl, pos["player_id"]))
        self.db.execute("DELETE FROM positions WHERE id=?", (pos["id"],))
        self.db.execute("INSERT INTO trades(player_id,pair,side,pnl,reason,closed_at) VALUES(?,?,?,?,?,?)",
                        (pos["player_id"], pos["pair"], pos["side"], pnl, reason, time.time()))
        if reason == "爆仓":
            self.db.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='liquidations'")
            self.db.execute("INSERT OR IGNORE INTO liquidated_players(player_id,first_at) VALUES(?,?)",
                            (pos["player_id"], time.time()))
        return rounded(pnl)

    def close_position(self, player_id, pos_id):
        with self.lock, self.db:
            pos = self.db.execute("SELECT * FROM positions WHERE id=? AND player_id=?", (pos_id, player_id)).fetchone()
            if not pos:
                raise GameError("仓位不存在或已经平仓。")
            price = self._apply_flow(pos["pair"], -pos["side"] * pos["notional"])
            price *= 1 - pos["side"] * 0.0002
            pnl = self._settle(pos, "手动平仓", price)
            self._liquidate()
        return {"message": f"平仓完成，本笔盈亏 {'+' if pnl >= 0 else ''}${pnl:.2f}。", "pnl": pnl}

    def _liquidate(self):
        # Each forced unwind is an order; it can move the price and cascade.
        for _ in range(100):
            positions = self.db.execute("SELECT * FROM positions ORDER BY opened_at").fetchall()
            victim = next((p for p in positions if self._pnl(p) <= -0.8 * p["margin"]), None)
            if victim is None:
                break
            price = self._price(victim["pair"])
            self._settle(victim, "爆仓", price)
            self._apply_flow(victim["pair"], -victim["side"] * victim["notional"])

    def reset(self, player_id):
        day = today()
        with self.lock, self.db:
            player = self.player(player_id)
            if player["reset_day"] == day and player["reset_used"] >= 1:
                raise GameError("今天的重新开始机会已经用完，明天 00:00（北京时间）恢复。")
            positions = self.db.execute("SELECT * FROM positions WHERE player_id=?", (player_id,)).fetchall()
            for pos in positions:
                self._apply_flow(pos["pair"], -pos["side"] * pos["notional"])
                self.db.execute("DELETE FROM positions WHERE id=?", (pos["id"],))
            self.db.execute("DELETE FROM trades WHERE player_id=?", (player_id,))
            self.db.execute("UPDATE players SET cash=?,reset_day=?,reset_used=1 WHERE id=?",
                            (START_CASH, day, player_id))
            self._liquidate()
        return {"message": "账户已恢复 $10,000，今天的重新开始机会已使用。"}

    def rename(self, player_id, nickname):
        nickname = str(nickname).strip()
        if not 2 <= len(nickname) <= 16 or any(ord(c) < 32 for c in nickname):
            raise GameError("昵称需要 2–16 个字符。")
        with self.lock, self.db:
            self.db.execute("UPDATE players SET nickname=? WHERE id=?", (nickname, player_id))
        return {"message": "昵称已更新。"}

    def bot_tick(self):
        """Deterministic agents provide liquidity when few humans are connected."""
        with self.lock, self.db:
            tick = int(self.db.execute("SELECT value FROM meta WHERE key='tick'").fetchone()["value"]) + 1
            self.db.execute("UPDATE meta SET value=? WHERE key='tick'", (str(tick),))
            # Three of fourteen agents act per tick; the others keep inventory.
            for offset in range(3):
                bot_id = (tick * 3 + offset) % BOT_COUNT
                bot = self.db.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
                pair = bot["pair"]
                if bot["side"] and tick - bot["opened_tick"] >= 4 + bot_id % 5:
                    self._apply_flow(pair, -bot["side"] * bot["notional"])
                    self.db.execute("UPDATE bots SET side=0,notional=0 WHERE id=?", (bot_id,))
                    continue
                if bot["side"]:
                    continue
                market = self.market(pair)
                momentum = (market["price"] / market["previous"] - 1) if market["previous"] else 0
                signal = math.sin((tick + bot_id * 7) * 0.37) + math.cos((tick - bot_id * 3) * 0.13) * 0.45 - momentum * 80
                side = 1 if signal >= 0 else -1
                volume = 3500 + ((tick * 739 + bot_id * 1907) % 11500)
                self._apply_flow(pair, side * volume)
                self.db.execute("UPDATE bots SET side=?,notional=?,opened_tick=? WHERE id=?",
                                (side, volume, tick, bot_id))
            self._liquidate()

