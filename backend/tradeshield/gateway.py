"""Idempotent Order Gateway - the ONLY path from a strategy to the broker.

Flow:  Signal -> risk check -> persist order (unique client_order_id) -> send to broker
       -> (timeout? ask the broker whether it already has that id, never blindly resend)
       -> fills arrive -> apply each fill exactly once (fill_id is unique) -> update sub-ledger.
"""
import asyncio
import time
import uuid

from .broker.base import BrokerRejected
from .ledger import apply_fill_to_position, compute_charges
from .models import OrderRequest, TERMINAL

NET_ERRORS = (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)


class OrderGateway:
    def __init__(self, db, broker, risk, events, settings, state, clock=time.time):
        self.db, self.broker, self.risk, self.events = db, broker, risk, events
        self.cfg, self.state, self.clock = settings, state, clock

    # ------------------------------------------------------------- submit
    async def submit(self, sub_id, signal, force=False, is_close=False, coid=None):
        decision = self.risk.evaluate(sub_id, signal, force=force)
        coid = coid or "TS-" + uuid.uuid4().hex[:16]
        if self.db.get_order(coid):                     # same logical order submitted twice => process once
            self.events.log("decision", "DUPLICATE", f"{coid} already exists - ignored duplicate submission", sub_id)
            return self.db.get_order(coid)
        self.db.insert_order({
            "client_order_id": coid, "sub_id": sub_id, "symbol": signal.symbol, "side": signal.side,
            "qty": signal.qty, "order_type": signal.order_type, "limit_price": signal.limit_price,
            "status": "NEW" if decision.allowed else "BLOCKED", "reason": decision.message,
            "ts": self.clock(), "is_close": is_close})
        self.events.log("decision", "ALLOW" if decision.allowed else "BLOCK",
                        f"{signal.side} {signal.qty} {signal.symbol} - {decision.code}: {decision.message}",
                        sub_id, {"code": decision.code, "client_order_id": coid, "strategy_reason": signal.reason})
        if decision.allowed:
            await self._send(OrderRequest(coid, signal.symbol, signal.side, signal.qty,
                                          signal.order_type, signal.limit_price))
        return self.db.get_order(coid)

    async def _send(self, req):
        coid, last_err = req.client_order_id, ""
        for attempt in range(1, self.cfg.max_send_attempts + 1):
            self.db.update_order(coid, status="SENT", attempts=attempt)
            try:
                bo = await asyncio.wait_for(self.broker.place_order(req), self.cfg.order_timeout)
                self.sync_from_broker(bo)
                return
            except BrokerRejected as e:
                self.db.update_order(coid, status="REJECTED", reason=f"broker rejected: {e}")
                return
            except NET_ERRORS as e:
                last_err = f"{type(e).__name__}: {e}"
                self.db.update_order(coid, status="UNKNOWN", reason=f"no acknowledgement ({last_err})")
                # Outcome unknown. Ask the broker about THIS id before deciding to resend.
                try:
                    bo = await asyncio.wait_for(self.broker.get_order(coid), self.cfg.order_timeout)
                    if bo is not None:
                        self.events.log("incident", "ORDER_FOUND",
                                        f"{coid}: ack was lost but the broker has the order - adopted, NOT resent",
                                        self.db.get_order(coid)["sub_id"])
                        self.sync_from_broker(bo)
                        return
                except NET_ERRORS:
                    pass
                await asyncio.sleep(self.cfg.retry_backoff * attempt)      # broker doesn't know it: safe retry, SAME id
        self.db.update_order(coid, status="FAILED", reason=f"gave up after {self.cfg.max_send_attempts} attempts: {last_err}")
        self.events.log("incident", "ORDER_FAILED", f"{coid} failed after {self.cfg.max_send_attempts} attempts ({last_err})",
                        self.db.get_order(coid)["sub_id"])

    # --------------------------------------------------------------- fills
    def apply_fill(self, f):
        """Apply one fill exactly once (idempotent on fill_id). Returns True if it was new."""
        if self.db.fill_exists(f.fill_id):
            return False
        order = self.db.get_order(f.client_order_id)
        if order is None:
            self.events.log("incident", "ORPHAN_FILL", f"fill {f.fill_id} for unknown order {f.client_order_id}")
            return False
        pos = self.db.get_position(order["sub_id"], f.symbol)
        new_qty, new_avg, realized = apply_fill_to_position(pos["qty"], pos["avg_price"], f.side, f.qty, f.price)
        charges = f.charges if f.charges is not None else compute_charges(f.side, f.qty, f.price, self.cfg)
        filled = order["filled_qty"] + f.qty
        avg = (order["avg_price"] * order["filled_qty"] + f.price * f.qty) / filled
        status = order["status"] if order["status"] == "CANCELLED" else ("FILLED" if filled >= order["qty"] else "PARTIAL")
        self.db.record_fill(
            {"fill_id": f.fill_id, "client_order_id": f.client_order_id, "sub_id": order["sub_id"],
             "symbol": f.symbol, "side": f.side, "qty": f.qty, "price": f.price, "charges": charges,
             "realized_pnl": realized, "ts": self.clock()},
            {"sub_id": order["sub_id"], "symbol": f.symbol, "qty": new_qty, "avg_price": new_avg},
            {"filled_qty": filled, "avg_price": avg, "status": status})
        return True

    def sync_from_broker(self, bo):
        """Make our record of an order match the broker's. Safe to call any number of times."""
        for f in bo.fills:
            self.apply_fill(f)
        row = self.db.get_order(bo.client_order_id)
        if row is None:
            return
        if bo.status == "REJECTED":
            self.db.update_order(bo.client_order_id, status="REJECTED", reason=f"broker rejected: {bo.reject_reason}")
        elif bo.status == "CANCELLED":
            self.db.update_order(bo.client_order_id, status="CANCELLED")
        elif row["status"] not in TERMINAL and row["filled_qty"] == 0 and bo.status in ("OPEN", "PARTIAL"):
            self.db.update_order(bo.client_order_id, status="OPEN")

    def on_fill(self, f):
        """Callback for fills pushed by the broker."""
        self.apply_fill(f)

    # -------------------------------------------------------------- cancel
    async def cancel(self, coid):
        for _ in range(3):
            try:
                bo = await asyncio.wait_for(self.broker.cancel_order(coid), self.cfg.order_timeout)
                if bo is not None:
                    self.sync_from_broker(bo)
                else:
                    self.db.update_order(coid, status="CANCELLED", reason="never reached the broker")
                return True
            except NET_ERRORS:
                await asyncio.sleep(0.1)
        return False
