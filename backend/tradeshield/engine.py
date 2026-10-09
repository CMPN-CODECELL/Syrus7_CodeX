"""The Engine wires everything together: ticks -> candles -> strategies -> risk -> gateway -> broker -> ledger."""
import asyncio
import time

from .candles import CandleEngine
from .config import Settings
from .db import Database
from .events import EventLog
from .gateway import OrderGateway
from .ledger import Ledger, compute_charges
from .models import State, Signal, Flatten
from .strategies import ist_now
from .recovery import reconcile
from .risk import RiskEngine
from .strategies import REGISTRY, Ctx, catalog


def build_broker(settings):
    if settings.broker_mode == "021":
        if not (settings.o21_username and settings.o21_password):
            raise RuntimeError("021 mode needs credentials: set O21_USERNAME (your UCC, e.g. HACK1234) and "
                               "O21_PASSWORD in the .env file (copy .env.example to .env).")
        from .broker.o21 import O21Broker
        return O21Broker(settings)
    from .broker.mock import MockExchange
    return MockExchange(settings.symbols, settings.time_scale, settings.tick_interval,
                        state_path=settings.mock_state_path or None)


class Engine:
    def __init__(self, settings=None, broker=None, clock=time.time, db=None):
        self.settings = settings or Settings()
        self.clock = clock
        self.db = db or Database(self.settings.db_path)
        self.state = State()
        self.prices = {}
        self.events = EventLog(self.db, clock)
        self.broker = broker or build_broker(self.settings)
        if hasattr(self.broker, "attach_events"):
            self.broker.attach_events(self.events)           # lets the 021 adapter write retries/relogins to the Activity log
        self.opens = {}                                      # symbol -> (IST date, open price)
        self.candles = CandleEngine(self.settings.symbols, ("1m", "5m", "40t"))
        for spec in self.db.list_intervals():
            self.candles.add_interval(spec)
        self.ledger = Ledger(self.db, self.settings, self.prices, clock)
        self.risk = RiskEngine(self.db, self.ledger, self.events, self.state, clock)
        self.risk.on_loss_halt = lambda sid: self.spawn(self.flatten_sub(sid))
        self.gateway = OrderGateway(self.db, self.broker, self.risk, self.events, self.settings, self.state, clock)
        self._instances = {}
        self._tasks = set()
        self._bg = []
        self.pnl_history = []
        for sub in self.db.all_subs():
            self._ensure_intervals(sub["params"].get("interval"))

    # ----------------------------------------------------------- lifecycle
    async def start(self):
        self.broker.subscribe_ticks(self.on_tick)
        self.broker.subscribe_fills(self.gateway.on_fill)
        self.state.paused, self.state.pause_reason = True, "startup reconciliation"
        await self.broker.start()
        await reconcile(self, "startup")
        self._bg = [asyncio.create_task(self._pnl_loop()), asyncio.create_task(self._sync_loop()),
                    asyncio.create_task(self._clock_loop())]

    async def stop(self):
        for t in self._bg + list(self._tasks):
            t.cancel()
        await self.broker.stop()

    # ------------------------------------------------------------- ticks
    def on_tick(self, tick):
        self.prices[tick.symbol] = tick.price
        today = ist_now(self.clock()).date().isoformat()
        known = self.opens.get(tick.symbol)
        if tick.open and tick.open > 0:
            self.opens[tick.symbol] = (today, tick.open)
        elif known is None or known[0] != today:
            self.opens[tick.symbol] = (today, tick.price)        # no open in the feed: first price seen today
        for candle in self.candles.on_tick(tick):
            self._dispatch_candle(candle)
        self._dispatch_tick(tick)

    def open_price(self, symbol):
        o = self.opens.get(symbol)
        return o[1] if o and o[0] == ist_now(self.clock()).date().isoformat() else None

    def _dispatch_candle(self, candle):
        if self.state.killed or self.state.paused:
            return
        for sub in self.db.active_subs():
            strat = self._instance(sub)
            if candle.symbol not in strat.symbols or candle.interval not in strat.required_intervals(sub["params"]):
                continue
            try:
                signals = strat.on_candle(candle, Ctx(self, sub))
            except Exception as e:                      # a crashing strategy must never hurt the platform
                self.risk.halt(sub["id"], "HALTED_ERROR", f"strategy crashed: {e!r}")
                continue
            self._emit(sub, signals)

    def _emit(self, sub, signals):
        """Turn what a strategy returned into platform actions. Signals still pass the risk engine."""
        for sig in signals[:200]:
            if isinstance(sig, Flatten):
                self.spawn(self.close_sub(sub["id"], sig.reason))
            else:
                self.spawn(self.gateway.submit(sub["id"], sig))

    def _dispatch_tick(self, tick):
        if self.state.killed or self.state.paused:
            return
        for sub in self.db.active_subs():
            strat = self._instance(sub)
            if not strat.wants_ticks or tick.symbol not in strat.symbols:
                continue
            try:
                signals = strat.on_tick(tick, Ctx(self, sub))
            except Exception as e:
                self.risk.halt(sub["id"], "HALTED_ERROR", f"strategy crashed: {e!r}")
                continue
            self._emit(sub, signals)

    def _dispatch_time(self):
        if self.state.killed or self.state.paused:
            return
        now = ist_now(self.clock())
        for sub in self.db.active_subs():
            strat = self._instance(sub)
            if not strat.wants_time:
                continue
            try:
                signals = strat.on_time(now, Ctx(self, sub))
            except Exception as e:
                self.risk.halt(sub["id"], "HALTED_ERROR", f"strategy crashed: {e!r}")
                continue
            self._emit(sub, signals)

    async def _clock_loop(self):
        while True:
            await asyncio.sleep(1.0)
            try:
                self._dispatch_time()
            except Exception as e:
                self.events.log("incident", "TASK_ERROR", f"clock loop failed: {e!r}")

    def _instance(self, sub):
        if sub["id"] not in self._instances:
            self._instances[sub["id"]] = REGISTRY[sub["strategy_key"]]()
        return self._instances[sub["id"]]

    def spawn(self, coro):
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._task_done)
        return t

    def _task_done(self, t):
        self._tasks.discard(t)
        if not t.cancelled() and t.exception():
            self.events.log("incident", "TASK_ERROR", f"background task failed: {t.exception()!r}")

    # ----------------------------------------------------- subscriptions
    def _ensure_intervals(self, spec):
        if spec:
            self.candles.add_interval(spec)

    def add_interval(self, spec):
        spec = self.candles.add_interval(spec)          # raises ValueError on bad input
        self.db.add_interval(spec)
        return spec

    def subscribe(self, user_id, key):
        if key not in REGISTRY:
            raise ValueError("Unknown strategy")
        cls = REGISTRY[key]
        sub_id = self.db.create_sub(user_id, key, cls.default_limits, cls.default_params)
        self._ensure_intervals(self.db.get_sub(sub_id)["params"].get("interval"))
        self.events.log("incident", "SUBSCRIBED", f"Subscribed to {cls.name} (subscription #{sub_id})", sub_id)
        return sub_id

    def unsubscribe(self, sub_id):
        self.db.update_sub(sub_id, status="STOPPED", halt_reason="unsubscribed by user")
        self.events.log("incident", "STOPPED", f"Subscription #{sub_id} stopped by user", sub_id)

    def resume(self, sub_id):
        if self.state.killed:
            raise ValueError("Kill switch is active - reset it first")
        self.db.update_sub(sub_id, status="ACTIVE", halt_reason="")
        self.events.log("incident", "RESUMED", f"Subscription #{sub_id} resumed", sub_id)

    def update_limits(self, sub_id, max_daily_loss, max_position, max_orders_per_min):
        if max_daily_loss <= 0 or max_position <= 0 or max_orders_per_min <= 0:
            raise ValueError("Limits must be positive numbers")
        self.db.update_sub(sub_id, max_daily_loss=max_daily_loss, max_position=int(max_position),
                           max_orders_per_min=int(max_orders_per_min))
        self.events.log("incident", "LIMITS", f"Limits updated for #{sub_id}: loss Rs{max_daily_loss:.0f}, "
                        f"position {int(max_position)}, orders/min {int(max_orders_per_min)}", sub_id)

    def update_params(self, sub_id, params):
        sub = self.db.get_sub(sub_id)
        merged = {**sub["params"], **params}
        if merged.get("interval"):
            self.add_interval(merged["interval"])
        self.db.update_sub(sub_id, params=merged)
        self._instances.pop(sub_id, None)
        self.events.log("incident", "PARAMS", f"Parameters for #{sub_id} set to {merged}", sub_id)

    async def close_sub(self, sub_id, reason=""):
        """Platform action behind Flatten(): cancel the strategy's open orders, then close what is left."""
        self.events.log("incident", "SQUARE_OFF", f"Strategy #{sub_id}: {reason}", sub_id)
        await asyncio.gather(*(self.gateway.cancel(r["client_order_id"]) for r in self.db.open_orders(sub_id)),
                             return_exceptions=True)
        await self.flatten_sub(sub_id)

    async def flatten_sub(self, sub_id):
        """Close one strategy's positions (used when its daily-loss limit is breached)."""
        for p in self.db.sub_positions(sub_id):
            await self.gateway.submit(sub_id, Signal(p["symbol"], "SELL" if p["qty"] > 0 else "BUY", abs(p["qty"]),
                                                     "daily loss limit: close position"), force=True, is_close=True)

    # -------------------------------------------------------------- what-if
    def what_if(self, user_id, sub_id, symbol, side, qty, price=None):
        sub = self.db.get_sub(sub_id)
        if not sub or sub["user_id"] != user_id:
            raise ValueError("Subscription not found")
        side = side.upper()
        if side not in ("BUY", "SELL"):
            raise ValueError("Side must be BUY or SELL")
        if symbol not in self.settings.symbols:
            raise ValueError(f"Unknown symbol {symbol}")
        if not isinstance(qty, int) or qty < 1 or qty > 100000:
            raise ValueError("Quantity must be between 1 and 100000")
        return self.risk.simulate(sub_id, Signal(symbol, side, qty, "what-if"), price=price)

    def what_if_kill(self, user_id):
        """Preview what the kill switch would do right now (nothing is changed)."""
        rows, charges, slip, open_orders = [], 0.0, 0.0, 0
        for sub in self.db.user_subs(user_id):
            open_orders += len(self.db.open_orders(sub["id"]))
            for p in self.db.sub_positions(sub["id"]):
                px = self.prices.get(p["symbol"], p["avg_price"])
                side = "SELL" if p["qty"] > 0 else "BUY"
                notional = abs(p["qty"]) * px
                c = compute_charges(side, abs(p["qty"]), px, self.settings)
                s = notional * 0.0002                       # same slippage the mock exchange applies
                charges += c
                slip += s
                rows.append({"strategy": REGISTRY[sub["strategy_key"]].name, "symbol": p["symbol"], "qty": p["qty"],
                             "close_side": side, "price": round(px, 2), "notional": round(notional, 2),
                             "est_charges": round(c, 2), "est_slippage": round(s, 2)})
        return {"open_orders_to_cancel": open_orders, "positions_to_close": rows,
                "total_est_charges": round(charges, 2), "total_est_slippage": round(slip, 2)}

    # ----------------------------------------------------------- kill switch
    async def kill_switch(self):
        t0 = time.monotonic()
        deadline = t0 + self.settings.kill_deadline
        self.state.killed = True                                  # 1. instantly block every new order
        self.events.log("incident", "KILL_SWITCH", "KILL SWITCH activated: stopping strategies, cancelling orders, closing positions")
        for sub in self.db.all_subs():                            # 2. stop every strategy
            if sub["status"] == "ACTIVE":
                self.db.update_sub(sub["id"], status="HALTED_KILL", halt_reason="kill switch")
        for t in list(self._tasks):                               # 3. drop strategy orders still in flight
            t.cancel()
        # 4. cancel every open order (in parallel) - ours and anything the broker still shows
        await asyncio.gather(*(self.gateway.cancel(r["client_order_id"]) for r in self.db.open_orders()),
                             return_exceptions=True)
        try:
            await asyncio.gather(*(self.broker.cancel_order(o.client_order_id)
                                   for o in await self.broker.get_open_orders()), return_exceptions=True)
        except Exception:
            pass
        # 5. close every position (in parallel); repeat until flat or the deadline
        while time.monotonic() < deadline:
            open_keys = {(r["sub_id"], r["symbol"]) for r in self.db.open_orders()}
            todo = []
            for sub_id in [0] + [s["id"] for s in self.db.all_subs()]:
                for p in self.db.sub_positions(sub_id):
                    if (sub_id, p["symbol"]) not in open_keys:
                        todo.append((sub_id, p["symbol"], p["qty"]))
            if not todo and not self.db.open_orders():
                break
            await self._resync_open_orders()
            if todo:
                await asyncio.gather(*(self.gateway.submit(
                    sid, Signal(sym, "SELL" if q > 0 else "BUY", abs(q), "kill switch: close position"),
                    force=True, is_close=True) for sid, sym, q in todo), return_exceptions=True)
            await asyncio.sleep(0.15)
        elapsed = time.monotonic() - t0
        flat = not self.db.open_orders() and not any(self.db.sub_positions(s) for s in [0] + [x["id"] for x in self.db.all_subs()])
        result = {"elapsed_seconds": round(elapsed, 2), "within_deadline": elapsed <= self.settings.kill_deadline, "flat": flat}
        self.events.log("incident", "KILL_SWITCH_DONE",
                        f"Kill switch finished in {elapsed:.2f}s ({'within' if result['within_deadline'] else 'EXCEEDED'} "
                        f"{self.settings.kill_deadline:.0f}s limit); all positions closed and orders cancelled: {flat}", None, result)
        return result

    async def _resync_open_orders(self):
        """Ask the broker about every order we think is open (used while the kill switch waits for the account to go flat)."""
        async def one(row):
            bo = await asyncio.wait_for(self.broker.get_order(row["client_order_id"]), self.settings.order_timeout)
            if bo is not None:
                self.gateway.sync_from_broker(bo)
        await asyncio.gather(*(one(r) for r in self.db.open_orders()), return_exceptions=True)

    def reset_kill(self):
        self.state.killed = False
        self.events.log("incident", "KILL_RESET", "Kill switch reset. Strategies stay halted until resumed individually.")

    # ----------------------------------------------------------- crash demo
    async def simulate_crash(self, downtime=3.0):
        """Behave like the server just died and restarted: lose memory, miss fills, then reconcile."""
        self.state.paused, self.state.pause_reason = True, "simulated crash"
        self.events.log("incident", "CRASH", f"SIMULATED CRASH: process memory lost, fills missed for {downtime:.0f}s")
        self.broker.unsubscribe_fills(self.gateway.on_fill)
        self.broker.unsubscribe_ticks(self.on_tick)
        for t in list(self._tasks):
            t.cancel()
        self._instances.clear()
        self.candles.reset()
        await asyncio.sleep(downtime)
        self.broker.subscribe_ticks(self.on_tick)
        self.broker.subscribe_fills(self.gateway.on_fill)
        return await reconcile(self, "post-crash restart")

    # -------------------------------------------------------- background
    async def _pnl_loop(self):
        while True:
            await asyncio.sleep(1.0)
            try:
                subs = self.db.all_subs()
                pts = {s["id"]: self.ledger.pnl(s["id"])["total_net"] for s in subs}
                self.pnl_history.append({"ts": self.clock(), "total": round(sum(pts.values()), 2), "subs": pts})
                del self.pnl_history[:-900]
            except Exception:
                pass

    async def _sync_loop(self):
        """Safety net: periodically re-check orders we think are open (catches any fill we missed)."""
        while True:
            await asyncio.sleep(self.settings.sync_interval)
            try:
                for row in self.db.open_orders():
                    if self.clock() - row["updated_ts"] > 2:
                        bo = await asyncio.wait_for(self.broker.get_order(row["client_order_id"]), self.settings.order_timeout)
                        if bo is not None:
                            self.gateway.sync_from_broker(bo)
            except Exception:
                pass

    # ---------------------------------------------------------- read models
    def sub_view(self, sub):
        strat = REGISTRY.get(sub["strategy_key"])
        pnl = self.ledger.pnl(sub["id"])
        lim = self.risk.effective_limits(sub, pnl["day_net"])
        now = self.clock()
        recent = sum(1 for t in self.risk._allowed[sub["id"]] if now - t <= 60)
        return {
            "id": sub["id"], "strategy_key": sub["strategy_key"], "name": strat.name if strat else sub["strategy_key"],
            "demo": bool(strat and strat.demo), "symbols": strat.symbols if strat else [],
            "status": sub["status"], "halt_reason": sub["halt_reason"], "params": sub["params"],
            "limits": {"max_daily_loss": sub["max_daily_loss"], "max_position": sub["max_position"],
                       "max_orders_per_min": sub["max_orders_per_min"]},
            "effective": {"max_position": lim["max_position"], "max_orders_per_min": lim["max_orders"],
                          "shield_tier": lim["tier"], "loss_used_pct": round(lim["loss_frac"] * 100, 1)},
            "orders_last_min": recent,
            "positions": [{"symbol": p["symbol"], "qty": p["qty"], "avg_price": round(p["avg_price"], 2)}
                          for p in self.db.sub_positions(sub["id"])],
            "pnl": pnl, "open_orders": len(self.db.open_orders(sub["id"])),
        }

    def snapshot(self, user_id):
        subs = [self.sub_view(s) for s in self.db.user_subs(user_id)]
        return {"ts": self.clock(), "killed": self.state.killed, "paused": self.state.paused,
                "pause_reason": self.state.pause_reason, "prices": dict(self.prices), "subs": subs,
                "total_net": round(sum(s["pnl"]["total_net"] for s in subs), 2),
                "account_positions": self.db.net_positions(), "intervals": list(self.candles.specs)}
