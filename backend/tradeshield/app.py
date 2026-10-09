"""FastAPI application: REST + WebSocket on top of the Engine."""
import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import Settings
from .engine import Engine
from .models import Signal
from .recovery import reconcile
from .strategies import REGISTRY, catalog

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
engine: Optional[Engine] = None


@asynccontextmanager
async def lifespan(app):
    global engine
    engine = Engine(Settings())
    await engine.start()
    yield
    await engine.stop()


app = FastAPI(title="TradeShield", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ------------------------------------------------------------------ auth
class Credentials(BaseModel):
    username: str
    password: str


def current_user(authorization: str = Header(default="")):
    token = authorization.removeprefix("Bearer ").strip()
    user = engine.db.user_for_token(token) if token else None
    if not user:
        raise HTTPException(401, "Not authenticated")
    return user


def own_sub(sub_id: int, user):
    sub = engine.db.get_sub(sub_id)
    if not sub or sub["user_id"] != user["id"]:
        raise HTTPException(404, "Subscription not found")
    return sub


@app.post("/api/register")
def register(c: Credentials):
    if len(c.username.strip()) < 3 or len(c.password) < 4:
        raise HTTPException(400, "Username needs 3+ characters and password 4+")
    try:
        uid = engine.db.create_user(c.username.strip(), c.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"token": engine.db.create_token(uid), "username": c.username.strip()}


@app.post("/api/login")
def login(c: Credentials):
    uid = engine.db.verify_user(c.username.strip(), c.password)
    if uid is None:
        raise HTTPException(401, "Wrong username or password")
    return {"token": engine.db.create_token(uid), "username": c.username.strip()}


# ------------------------------------------------------------ strategies
@app.get("/api/strategies")
def strategies(user=Depends(current_user)):
    mine = {s["strategy_key"]: s["id"] for s in engine.db.user_subs(user["id"])}
    return [{**c, "subscription_id": mine.get(c["key"])} for c in catalog()]


class SubscribeBody(BaseModel):
    strategy_key: str


@app.post("/api/subscriptions")
def subscribe(b: SubscribeBody, user=Depends(current_user)):
    try:
        return {"id": engine.subscribe(user["id"], b.strategy_key)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/subscriptions/{sub_id}")
def unsubscribe(sub_id: int, user=Depends(current_user)):
    own_sub(sub_id, user)
    engine.unsubscribe(sub_id)
    return {"ok": True}


@app.post("/api/subscriptions/{sub_id}/resume")
def resume(sub_id: int, user=Depends(current_user)):
    own_sub(sub_id, user)
    try:
        engine.resume(sub_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


class LimitsBody(BaseModel):
    max_daily_loss: float
    max_position: int
    max_orders_per_min: int


@app.put("/api/subscriptions/{sub_id}/limits")
def set_limits(sub_id: int, b: LimitsBody, user=Depends(current_user)):
    own_sub(sub_id, user)
    try:
        engine.update_limits(sub_id, b.max_daily_loss, b.max_position, b.max_orders_per_min)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


class ParamsBody(BaseModel):
    params: dict


@app.put("/api/subscriptions/{sub_id}/params")
def set_params(sub_id: int, b: ParamsBody, user=Depends(current_user)):
    own_sub(sub_id, user)
    try:
        engine.update_params(sub_id, b.params)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# ------------------------------------------------------------- read data
@app.get("/api/snapshot")
def snapshot(user=Depends(current_user)):
    return engine.snapshot(user["id"])


@app.get("/api/orders")
def orders(limit: int = 100, user=Depends(current_user)):
    return engine.db.list_orders(user["id"], min(limit, 500))


@app.get("/api/trades")
def trades(limit: int = 100, user=Depends(current_user)):
    return engine.db.list_fills(user["id"], min(limit, 500))


@app.get("/api/events")
def events(category: Optional[str] = None, limit: int = 100, user=Depends(current_user)):
    return engine.db.list_events(min(limit, 500), category)


@app.get("/api/pnl-history")
def pnl_history(user=Depends(current_user)):
    mine = {s["id"] for s in engine.db.user_subs(user["id"])}
    return [{"ts": p["ts"], "total": round(sum(v for k, v in p["subs"].items() if k in mine), 2)}
            for p in engine.pnl_history]


@app.get("/api/candles")
def candles(symbol: str, interval: str, n: int = 60, user=Depends(current_user)):
    interval = interval.lower()
    if interval not in engine.candles.specs or symbol not in engine.settings.symbols:
        raise HTTPException(400, "Unknown symbol or interval")
    out = [c.to_dict() for c in engine.candles.history(symbol, interval, n)]
    cur = engine.candles.current(symbol, interval)
    return {"candles": out, "current": cur.to_dict() if cur else None}


class IntervalBody(BaseModel):
    interval: str


@app.get("/api/intervals")
def intervals(user=Depends(current_user)):
    return {"intervals": list(engine.candles.specs), "symbols": engine.settings.symbols}


@app.post("/api/intervals")
def add_interval(b: IntervalBody, user=Depends(current_user)):
    try:
        return {"interval": engine.add_interval(b.interval), "intervals": list(engine.candles.specs)}
    except ValueError as e:
        raise HTTPException(400, str(e))


# --------------------------------------------------------- what-if simulator
class WhatIfBody(BaseModel):
    subscription_id: int
    symbol: str
    side: str
    qty: int
    price: Optional[float] = None


@app.post("/api/what-if")
def what_if(b: WhatIfBody, user=Depends(current_user)):
    try:
        return engine.what_if(user["id"], b.subscription_id, b.symbol, b.side, b.qty, b.price)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/what-if/kill")
def what_if_kill(user=Depends(current_user)):
    return engine.what_if_kill(user["id"])


# ------------------------------------------------------------ kill switch
@app.post("/api/kill")
async def kill(user=Depends(current_user)):
    return await engine.kill_switch()


@app.post("/api/kill/reset")
def kill_reset(user=Depends(current_user)):
    engine.reset_kill()
    return {"ok": True}


# ------------------------------------------------- demo / testing tools
class ChaosBody(BaseModel):
    reject_rate: Optional[float] = None
    error_rate: Optional[float] = None
    ack_loss_rate: Optional[float] = None
    partial_fills: Optional[bool] = None
    fill_probability: Optional[float] = None
    latency: Optional[float] = None


def _chaos():
    ch = getattr(engine.broker, "chaos", None)
    if ch is None:
        raise HTTPException(400, "Failure injection is only available with the mock exchange")
    return ch


@app.get("/api/admin/chaos")
def get_chaos(user=Depends(current_user)):
    return _chaos().__dict__


@app.post("/api/admin/chaos")
def set_chaos(b: ChaosBody, user=Depends(current_user)):
    ch = _chaos()
    for k, v in b.model_dump().items():
        if v is not None:
            setattr(ch, k, v)
    engine.events.log("incident", "CHAOS", f"Failure injection changed: {ch.__dict__}")
    return ch.__dict__


class CrashBody(BaseModel):
    downtime: float = 3.0


@app.post("/api/admin/simulate-crash")
async def simulate_crash(b: CrashBody, user=Depends(current_user)):
    return await engine.simulate_crash(max(0.5, min(b.downtime, 20)))


@app.post("/api/admin/reconcile")
async def manual_reconcile(user=Depends(current_user)):
    return await reconcile(engine, "manual")


class DupBody(BaseModel):
    subscription_id: int


@app.post("/api/admin/duplicate-test")
async def duplicate_test(b: DupBody, user=Depends(current_user)):
    """Send the SAME logical order twice (like a network retry) and show that it is processed once."""
    sub = own_sub(b.subscription_id, user)
    sym = REGISTRY[sub["strategy_key"]].symbols[0]
    coid = "DUP-" + uuid.uuid4().hex[:10]
    sig = Signal(sym, "BUY", 1, "duplicate-order demo")
    r = await asyncio.gather(engine.gateway.submit(sub["id"], sig, coid=coid), engine.gateway.submit(sub["id"], sig, coid=coid))
    at_broker = await engine.broker.get_order(coid)
    return {"client_order_id": coid, "submissions": 2, "orders_created": 1 if at_broker else 0,
            "statuses": [x["status"] for x in r]}


# ------------------------------------------------------------- websocket
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, token: str = Query("")):
    user = engine.db.user_for_token(token)
    if not user:
        await ws.close(code=4401)
        return
    await ws.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=300)

    def on_event(ev):
        try:
            q.put_nowait(ev)
        except asyncio.QueueFull:
            pass

    engine.events.listeners.append(on_event)
    last = 0.0
    try:
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=0.5)
                await ws.send_json({"type": "event", "event": ev})
            except asyncio.TimeoutError:
                pass
            if time.time() - last >= 1.0:
                await ws.send_json({"type": "snapshot", "data": engine.snapshot(user["id"])})
                last = time.time()
    except Exception:
        pass
    finally:
        if on_event in engine.events.listeners:
            engine.events.listeners.remove(on_event)


# serve the built React app (if present) from the same server
_dist = os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "dist")
if os.path.isdir(_dist):
    app.mount("/", StaticFiles(directory=_dist, html=True), name="ui")
