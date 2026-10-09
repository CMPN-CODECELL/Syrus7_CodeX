"""A realistic stand-in for the 021 sandbox so the whole platform can be built, demoed and tested.

It behaves like a real exchange: random-walk ticks, partial fills, rejections, idempotent order ids,
lost acknowledgements, network errors - and it keeps its own state (optionally in a JSON file), so it
survives a restart of the platform exactly like a real broker would.
"""
import asyncio
import copy
import json
import math
import os
import random
import time
from dataclasses import dataclass, asdict

from .base import Broker
from ..models import Tick, Fill, BrokerOrder, OrderRequest

BASE_PRICES = {"TCS": 3800.0, "INFY": 1500.0, "RELIANCE": 2900.0}


@dataclass
class Chaos:
    """Failure injection - changeable at runtime from the dashboard's demo tools."""
    reject_rate: float = 0.0        # exchange rejects the order
    error_rate: float = 0.0         # request never reaches the exchange (network error)
    ack_loss_rate: float = 0.0      # exchange accepts the order but the acknowledgement is lost
    partial_fills: bool = True      # fill in several chunks
    fill_probability: float = 1.0   # chance an open order progresses on each fill pass (<1 = slow fills)
    latency: float = 0.0            # artificial delay per request (seconds)
    max_order_qty: int = 1000


class MockExchange(Broker):
    def __init__(self, symbols, time_scale=10.0, tick_interval=0.25, seed=None, state_path=None,
                 clock=time.time):
        self.rng = random.Random(seed)
        self.symbols = list(symbols)
        self.time_scale, self.tick_interval = time_scale, tick_interval
        self.prices = {s: BASE_PRICES.get(s, 1000.0) for s in self.symbols}
        self.sim_ts = float(int(clock() // 60) * 60)             # start aligned to a minute boundary
        self.orders = {}                                         # client_order_id -> BrokerOrder
        self.positions = {}                                      # symbol -> {"qty": int, "avg": float}
        self.chaos = Chaos()
        self.state_path = state_path
        self._tick_cbs, self._fill_cbs, self._tasks = [], [], []
        self._phase = {s: self.rng.uniform(0, 6.28) for s in self.symbols}
        self._n = 0
        self._load()

    # ------------------------------------------------------------ lifecycle
    async def start(self):
        self._tasks = [asyncio.create_task(self._tick_loop()), asyncio.create_task(self._fill_loop())]

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        self._tasks = []

    def subscribe_ticks(self, cb):
        self._tick_cbs.append(cb)

    def unsubscribe_ticks(self, cb):
        if cb in self._tick_cbs:
            self._tick_cbs.remove(cb)

    def subscribe_fills(self, cb):
        self._fill_cbs.append(cb)

    def unsubscribe_fills(self, cb):
        if cb in self._fill_cbs:
            self._fill_cbs.remove(cb)

    # --------------------------------------------------------------- market
    def tick_once(self):
        """Advance the simulated market by one tick and publish a tick for every symbol."""
        self._n += 1
        self.sim_ts += self.tick_interval * self.time_scale
        for s in self.symbols:
            drift = 0.0007 * math.sin(self._n / 90.0 + self._phase[s])      # slow trends
            self.prices[s] = round(max(1.0, self.prices[s] * (1 + drift + self.rng.gauss(0, 0.0007))), 2)
            tick = Tick(s, self.prices[s], self.sim_ts, self.rng.randint(1, 50))
            for cb in list(self._tick_cbs):
                try:
                    cb(tick)
                except Exception:
                    pass

    async def _tick_loop(self):
        while True:
            self.tick_once()
            await asyncio.sleep(self.tick_interval)

    # --------------------------------------------------------------- orders
    async def place_order(self, req: OrderRequest) -> BrokerOrder:
        ch = self.chaos
        if ch.latency:
            await asyncio.sleep(ch.latency)
        if self.rng.random() < ch.error_rate:
            raise ConnectionError("simulated network failure (order never reached the exchange)")
        existing = self.orders.get(req.client_order_id)
        if existing:                                              # IDEMPOTENT: same id => same order
            return copy.deepcopy(existing)
        o = BrokerOrder(req.client_order_id, req.symbol, req.side, req.qty, req.order_type, req.limit_price)
        if req.symbol not in self.prices:
            o.status, o.reject_reason = "REJECTED", f"unknown symbol {req.symbol}"
        elif req.qty <= 0 or req.qty > ch.max_order_qty:
            o.status, o.reject_reason = "REJECTED", f"invalid quantity {req.qty}"
        elif self.rng.random() < ch.reject_rate:
            o.status, o.reject_reason = "REJECTED", "simulated exchange rejection"
        self.orders[o.client_order_id] = o
        self._persist()
        if self.rng.random() < ch.ack_loss_rate:                 # order is live, but caller never hears
            raise asyncio.TimeoutError("simulated lost acknowledgement")
        return copy.deepcopy(o)

    async def cancel_order(self, client_order_id):
        o = self.orders.get(client_order_id)
        if o and o.status in ("OPEN", "PARTIAL"):
            o.status = "CANCELLED"
            self._persist()
        return copy.deepcopy(o) if o else None

    async def get_order(self, client_order_id):
        o = self.orders.get(client_order_id)
        return copy.deepcopy(o) if o else None

    async def get_open_orders(self):
        return [copy.deepcopy(o) for o in self.orders.values() if o.status in ("OPEN", "PARTIAL")]

    async def get_positions(self):
        return {s: p["qty"] for s, p in self.positions.items() if p["qty"] != 0}

    # ---------------------------------------------------------------- fills
    async def process_fills_once(self):
        ch = self.chaos
        for o in list(self.orders.values()):
            if o.status not in ("OPEN", "PARTIAL"):
                continue
            if self.rng.random() > ch.fill_probability:
                continue
            px = self.prices[o.symbol]
            if o.order_type == "LIMIT" and o.limit_price is not None:
                if (o.side == "BUY" and px > o.limit_price) or (o.side == "SELL" and px < o.limit_price):
                    continue
            remaining = o.qty - o.filled_qty
            qty = remaining
            if ch.partial_fills and remaining > 1:
                qty = max(1, min(remaining, int(round(remaining * self.rng.uniform(0.3, 0.8)))))
            slip = 1.0002 if o.side == "BUY" else 0.9998
            self._execute(o, qty, round(px * slip, 2))

    async def _fill_loop(self):
        while True:
            await self.process_fills_once()
            await asyncio.sleep(0.3)

    def _execute(self, o, qty, price):
        f = Fill(f"{o.client_order_id}-{len(o.fills) + 1}", o.client_order_id, o.symbol, o.side, qty, price,
                 self.sim_ts)
        o.fills.append(f)
        total = o.filled_qty + qty
        o.avg_price = round((o.avg_price * o.filled_qty + price * qty) / total, 4)
        o.filled_qty = total
        o.status = "FILLED" if total >= o.qty else "PARTIAL"
        pos = self.positions.setdefault(o.symbol, {"qty": 0, "avg": 0.0})
        pos["qty"] += qty if o.side == "BUY" else -qty
        self._persist()
        for cb in list(self._fill_cbs):
            try:
                cb(copy.deepcopy(f))
            except Exception:
                pass

    # ------------------------------------------------------- state on disk
    def _persist(self):
        if not self.state_path:
            return
        data = {"orders": {k: asdict(v) for k, v in self.orders.items()}, "positions": self.positions,
                "prices": self.prices, "sim_ts": self.sim_ts}
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, self.state_path)

    def _load(self):
        if not self.state_path or not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path) as fh:
                data = json.load(fh)
            for k, v in data["orders"].items():
                v["fills"] = [Fill(**f) for f in v["fills"]]
                self.orders[k] = BrokerOrder(**v)
            self.positions, self.prices = data["positions"], data["prices"]
            self.sim_ts = data.get("sim_ts", self.sim_ts)
        except Exception:
            pass
