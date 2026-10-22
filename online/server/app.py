"""Independent A-share multiplayer service. Run: uvicorn app:app --host 127.0.0.1 --port 8788"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
import json

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ashare_engine import Engine, GameError
from broadcast_queue import BroadcastQueue


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("A_SHARE_DATA", str(ROOT / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_ORIGINS = {origin.strip().rstrip("/") for origin in os.environ.get(
    "A_SHARE_ALLOWED_ORIGINS", "https://www.bilibilitoy.com,https://www.bilibili.com,http://localhost:4173,http://127.0.0.1:4173"
).split(",") if origin.strip()}
engine = Engine(DATA_DIR / "a-share.sqlite3")
connections: dict[str, set[WebSocket]] = {}
send_lock = asyncio.Lock()
new_account_times: dict[str, list[float]] = {}
last_action_times: dict[str, float] = {}
logger = logging.getLogger(__name__)


def online_count():
    return sum(bool(sockets) for sockets in connections.values())


async def broadcast():
    async with send_lock:
        if not connections:
            return
        payloads = engine.snapshots(list(connections), online_count())
        async def send_one(player_id: str, socket: WebSocket):
            try:
                await asyncio.wait_for(socket.send_json(payloads[player_id]), timeout=1.5)
            except Exception:
                connections.get(player_id, set()).discard(socket)

        await asyncio.gather(*(
            send_one(player_id, socket)
            for player_id, sockets in list(connections.items())
            if player_id in payloads
            for socket in list(sockets)
        ))
        for player_id, sockets in list(connections.items()):
            if not sockets:
                connections.pop(player_id, None)


broadcast_queue = BroadcastQueue(broadcast)


async def bot_loop():
    while True:
        await asyncio.sleep(2)
        try:
            engine.bot_tick()
            broadcast_queue.request()
        except Exception:
            logger.exception("Bot tick failed; retrying next interval")


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(bot_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await broadcast_queue.close()


app = FastAPI(title="大A 简单! 联机版", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=sorted(ALLOWED_ORIGINS),
                   allow_methods=["GET", "POST"],
                   allow_headers=["Authorization", "Content-Type", "X-A-Share-Action"])


@app.middleware("http")
async def check_actions(request: Request, call_next):
    if request.method == "POST":
        origin = request.headers.get("origin")
        if request.headers.get("x-a-share-action") != "1" or (origin and origin not in ALLOWED_ORIGINS):
            return JSONResponse({"error": "请求来源无效。"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def auth(request: Request):
    player_id = engine.player_for_token(request_token(request))
    if not player_id:
        raise HTTPException(401, "请刷新页面建立访客账号。")
    return player_id


def request_token(request: Request):
    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        return authorization[7:]
    return request.cookies.get("a_share_session")


def action_limit(player_id):
    now = time.monotonic()
    if now - last_action_times.get(player_id, 0) < 0.35:
        raise HTTPException(429, "操作太快，请稍后重试。")
    last_action_times[player_id] = now


def handle_error(error: GameError):
    raise HTTPException(400, str(error)) from error


class Order(BaseModel):
    stock: str
    side: int
    margin: float
    leverage: int
    orderType: str = "market"
    limitPrice: float | None = None


class PositionAction(BaseModel):
    id: str
    orderType: str = "market"
    limitPrice: float | None = None


class CancelAction(BaseModel):
    id: str


class Rename(BaseModel):
    nickname: str


@app.get("/api/candles")
async def candles(stock: str, timeframe: str = "sec",
                  before: int | None = None, limit: int = 240):
    try:
        return engine.candle_history(stock, timeframe, before, limit)
    except GameError as error:
        handle_error(error)


@app.post("/api/session")
async def session(request: Request):
    token = request_token(request)
    player_id = engine.player_for_token(token)
    if not player_id:
        ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        recent = [point for point in new_account_times.get(ip, []) if now - point < 600]
        if len(recent) >= 12:
            raise HTTPException(429, "同一网络创建访客账号过于频繁，请稍后再试。")
        recent.append(now)
        new_account_times[ip] = recent
        token, player_id = engine.create_player()
    response = JSONResponse({**engine.snapshot(player_id, online_count()), "sessionToken": token})
    response.set_cookie("a_share_session", token, max_age=60 * 60 * 24 * 365,
                        httponly=True, samesite="lax", secure=request.url.scheme == "https")
    return response


@app.post("/api/order")
async def order(request: Request, body: Order):
    player_id = auth(request)
    action_limit(player_id)
    try:
        result = engine.open_position(player_id, body.stock, body.side, body.margin,
                                      body.leverage, body.orderType, body.limitPrice)
    except GameError as error:
        handle_error(error)
    broadcast_queue.request()
    return result


@app.post("/api/close")
async def close(request: Request, body: PositionAction):
    player_id = auth(request)
    action_limit(player_id)
    try:
        result = engine.close_position(player_id, body.id, body.orderType, body.limitPrice)
    except GameError as error:
        handle_error(error)
    broadcast_queue.request()
    return result


@app.post("/api/cancel")
async def cancel(request: Request, body: CancelAction):
    player_id = auth(request)
    action_limit(player_id)
    try:
        result = engine.cancel_order(player_id, body.id)
    except GameError as error:
        handle_error(error)
    broadcast_queue.request()
    return result


@app.post("/api/reset")
async def reset(request: Request):
    player_id = auth(request)
    action_limit(player_id)
    try:
        result = engine.reset(player_id)
    except GameError as error:
        handle_error(error)
    broadcast_queue.request()
    return result


@app.post("/api/rename")
async def rename(request: Request, body: Rename):
    player_id = auth(request)
    action_limit(player_id)
    try:
        result = engine.rename(player_id, body.nickname)
    except GameError as error:
        handle_error(error)
    broadcast_queue.request()
    return result


@app.websocket("/ws")
async def websocket(ws: WebSocket):
    origin = ws.headers.get("origin")
    if origin and origin not in ALLOWED_ORIGINS:
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        first = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=5))
        token = first.get("sessionToken") if isinstance(first, dict) else None
    except (asyncio.TimeoutError, WebSocketDisconnect, json.JSONDecodeError):
        await ws.close(code=1008)
        return
    player_id = engine.player_for_token(token or ws.cookies.get("a_share_session"))
    if not player_id:
        await ws.close(code=1008)
        return
    try:
        count = online_count() + (player_id not in connections)
        await asyncio.wait_for(ws.send_json(engine.snapshot(player_id, count)),
                               timeout=1.5)
    except Exception:
        return
    connections.setdefault(player_id, set()).add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        connections.get(player_id, set()).discard(ws)
        if not connections.get(player_id):
            connections.pop(player_id, None)


