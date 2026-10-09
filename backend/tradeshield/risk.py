"""Platform-owned risk engine. Strategies can NOT bypass it: they only produce Signals, and every
order - no matter what the strategy does - is evaluated here before it can reach the broker.

Checks (in order): kill switch -> reconciling -> strategy halted -> daily loss -> position size -> orders/min.
Adaptive Risk Shield: the more of the daily-loss budget a strategy has burnt, the tighter its limits get.
"""
import time
from collections import defaultdict, deque

from .ledger import apply_fill_to_position, compute_charges, day_start
from .models import Decision

# (fraction of daily-loss budget used, position multiplier, orders/min multiplier)
SHIELD_TIERS = [(0.50, 0.50, 0.50), (0.75, 0.25, 0.25)]
RUNAWAY_BLOCKS_PER_MIN = 20          # this many blocked orders in a minute => strategy is runaway => halt it


class RiskEngine:
    def __init__(self, db, ledger, events, state, clock=time.time, shield_enabled=True):
        self.db, self.ledger, self.events, self.state, self.clock = db, ledger, events, state, clock
        self.shield_enabled = shield_enabled
        self._allowed = defaultdict(deque)     # sub_id -> timestamps of allowed orders
        self._blocked = defaultdict(deque)     # sub_id -> timestamps of blocked orders
        self.on_loss_halt = None               # callback(sub_id): engine uses it to flatten the strategy

    # ------------------------------------------------------------ halting
    def halt(self, sub_id, status, reason):
        sub = self.db.get_sub(sub_id)
        if sub and sub["status"] == "ACTIVE":
            self.db.update_sub(sub_id, status=status, halt_reason=reason)
            self.events.log("incident", status, f"Strategy #{sub_id} ({sub['strategy_key']}) halted: {reason}", sub_id)

    # ------------------------------------------------- adaptive risk shield
    def tier_for(self, loss_frac):
        tier = 0
        if self.shield_enabled:
            for i, (threshold, _, _) in enumerate(SHIELD_TIERS):
                if loss_frac >= threshold:
                    tier = i + 1
        return tier

    def compute_limits(self, sub, day_pnl):
        """PURE: the limits that apply at a given day P&L. No side effects (used by the What-If Simulator)."""
        loss_frac = max(0.0, -day_pnl) / sub["max_daily_loss"] if sub["max_daily_loss"] > 0 else 0.0
        tier = self.tier_for(loss_frac)
        pm, rm = (SHIELD_TIERS[tier - 1][1], SHIELD_TIERS[tier - 1][2]) if tier else (1.0, 1.0)
        return {"max_position": max(1, int(sub["max_position"] * pm)),
                "max_orders": max(1, int(sub["max_orders_per_min"] * rm)),
                "tier": tier, "loss_frac": loss_frac}

    def effective_limits(self, sub, day_pnl):
        limits = self.compute_limits(sub, day_pnl)
        tier, loss_frac = limits["tier"], limits["loss_frac"]
        if tier != sub["shield_tier"]:
            self.db.update_sub(sub["id"], shield_tier=tier)
            verb = "tightened" if tier > sub["shield_tier"] else "relaxed"
            self.events.log(
                "incident", "SHIELD", f"Adaptive Risk Shield {verb} strategy #{sub['id']} ({sub['strategy_key']}): "
                f"tier {sub['shield_tier']}->{tier}; position limit {sub['max_position']}->{limits['max_position']}, "
                f"orders/min {sub['max_orders_per_min']}->{limits['max_orders']} "
                f"(loss Rs{max(0, -day_pnl):.0f} = {loss_frac * 100:.0f}% of Rs{sub['max_daily_loss']:.0f} budget)",
                sub["id"], {"tier": tier})
        return limits

    # ------------------------------------------------------------ evaluate
    def evaluate(self, sub_id, signal, force=False):
        if force:
            return Decision(True, "FORCED", "platform-initiated order (kill switch)")
        sub = self.db.get_sub(sub_id)
        if sub is None:
            return Decision(False, "NO_SUBSCRIPTION", "subscription not found")
        if self.state.killed:
            return self._block(sub_id, "KILL_SWITCH", "kill switch is active - all trading stopped")
        if self.state.paused:
            return self._block(sub_id, "RECONCILING", f"platform paused: {self.state.pause_reason}")
        if sub["status"] != "ACTIVE":
            return self._block(sub_id, "STRATEGY_HALTED", f"strategy is {sub['status']} ({sub['halt_reason'] or 'stopped'})")

        day_pnl = self.ledger.pnl(sub_id)["day_net"]
        lim = self.effective_limits(sub, day_pnl)

        if day_pnl <= -sub["max_daily_loss"]:
            self.halt(sub_id, "HALTED_RISK", f"daily loss limit hit (Rs{-day_pnl:.0f} >= Rs{sub['max_daily_loss']:.0f}); closing its positions")
            if self.on_loss_halt:
                self.on_loss_halt(sub_id)
            return self._block(sub_id, "DAILY_LOSS", f"daily loss Rs{-day_pnl:.0f} reached limit Rs{sub['max_daily_loss']:.0f}")

        pos = self.db.get_position(sub_id, signal.symbol)["qty"]
        # Worst case: assume every live (unfilled) order also fills. A runaway strategy therefore cannot
        # blow through the limit by sending many orders before the first fill comes back.
        if signal.side == "BUY":
            pending = self.db.pending_qty(sub_id, signal.symbol, "BUY")
            projected = pos + pending + signal.qty
            breach = projected > lim["max_position"]
        else:
            pending = self.db.pending_qty(sub_id, signal.symbol, "SELL")
            projected = pos - pending - signal.qty
            breach = projected < -lim["max_position"]
        if breach:
            tag = f" (shield tier {lim['tier']})" if lim["tier"] else ""
            note = f" incl. {pending} pending" if pending else ""
            return self._block(sub_id, "POSITION_LIMIT",
                               f"position would be {projected}{note}, max allowed {lim['max_position']}{tag}")

        now = self.clock()
        window = self._allowed[sub_id]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= lim["max_orders"]:
            tag = f" (shield tier {lim['tier']})" if lim["tier"] else ""
            return self._block(sub_id, "RATE_LIMIT", f"{len(window)} orders in the last minute, max {lim['max_orders']}{tag}")

        window.append(now)
        return Decision(True, "ALLOWED", f"position {pos}->{projected} (max {lim['max_position']}), "
                        f"orders {len(window)}/{lim['max_orders']} per min, day P&L Rs{day_pnl:.0f} "
                        f"(loss limit Rs{sub['max_daily_loss']:.0f})")

    def _block(self, sub_id, code, message):
        now = self.clock()
        q = self._blocked[sub_id]
        q.append(now)
        while q and now - q[0] > 60:
            q.popleft()
        if code in ("POSITION_LIMIT", "RATE_LIMIT") and len(q) >= RUNAWAY_BLOCKS_PER_MIN:
            self.halt(sub_id, "HALTED_RISK", f"runaway strategy detected ({len(q)} blocked orders in 60s)")
        return Decision(False, code, message)


    # ============================================================ WHAT-IF SIMULATOR
    def simulate(self, sub_id, signal, price=None, shocks=(-5, -2, -1, 1, 2, 5)):
        """Preview the risk impact of an order WITHOUT placing it and WITHOUT changing any state
        (no orders, no events, no rate-limit counters, no halts, no shield changes).

        Runs every check independently (so you see ALL that would fail) and returns the same verdict
        the real `evaluate()` would give, plus the effect on position, P&L, charges and the shield,
        and a price-shock stress table."""
        sub = self.db.get_sub(sub_id)
        if sub is None:
            raise ValueError("Subscription not found")
        last = price or self.ledger.prices.get(signal.symbol)
        if not last:
            raise ValueError(f"No market price yet for {signal.symbol}")
        now = self.clock()
        pnl = self.ledger.pnl(sub_id)
        day_pnl = pnl["day_net"]
        lim = self.compute_limits(sub, day_pnl)
        pos_row = self.db.get_position(sub_id, signal.symbol)
        pos, side, qty = pos_row["qty"], signal.side, signal.qty
        signed = qty if side == "BUY" else -qty

        if side == "BUY":
            pending = self.db.pending_qty(sub_id, signal.symbol, "BUY")
            projected = pos + pending + qty
            pos_ok = projected <= lim["max_position"]
        else:
            pending = self.db.pending_qty(sub_id, signal.symbol, "SELL")
            projected = pos - pending - qty
            pos_ok = projected >= -lim["max_position"]
        recent = sum(1 for t in self._allowed[sub_id] if now - t <= 60)
        tag = f" (shield tier {lim['tier']})" if lim["tier"] else ""
        note = f" incl. {pending} pending" if pending else ""
        loss_now = max(0.0, -day_pnl)

        checks = [
            {"code": "KILL_SWITCH", "label": "Kill switch", "ok": not self.state.killed,
             "detail": "kill switch is active - all trading stopped" if self.state.killed else "not active"},
            {"code": "RECONCILING", "label": "Platform state", "ok": not self.state.paused,
             "detail": f"platform paused: {self.state.pause_reason}" if self.state.paused else "running normally"},
            {"code": "STRATEGY_HALTED", "label": "Strategy status", "ok": sub["status"] == "ACTIVE",
             "detail": f"strategy is {sub['status']} ({sub['halt_reason'] or 'stopped'})" if sub["status"] != "ACTIVE" else "active"},
            {"code": "DAILY_LOSS", "label": "Daily loss budget", "ok": day_pnl > -sub["max_daily_loss"],
             "detail": f"loss Rs{loss_now:.0f} of Rs{sub['max_daily_loss']:.0f} limit", "used": round(loss_now, 2),
             "limit": sub["max_daily_loss"]},
            {"code": "POSITION_LIMIT", "label": "Position size", "ok": pos_ok,
             "detail": f"position would be {projected}{note}, max allowed {lim['max_position']}{tag}",
             "used": abs(projected), "limit": lim["max_position"]},
            {"code": "RATE_LIMIT", "label": "Orders per minute", "ok": recent < lim["max_orders"],
             "detail": f"{recent} orders in the last minute, max {lim['max_orders']}{tag}",
             "used": recent, "limit": lim["max_orders"]},
        ]
        first_fail = next((c for c in checks if not c["ok"]), None)
        verdict = {"allowed": first_fail is None, "code": first_fail["code"] if first_fail else "ALLOWED",
                   "message": first_fail["detail"] if first_fail else "all risk checks pass"}

        # ---- impact if the order fills at the current price
        new_qty, new_avg, realized_gross = apply_fill_to_position(pos, pos_row["avg_price"], side, qty, last)
        charges = compute_charges(side, qty, last, self.ledger.cfg)
        realized_today = self.db.realized_net(sub_id, day_start(now))["v"]
        realized_after = realized_today + realized_gross - charges
        other_unreal = sum(p["qty"] * (self.ledger.prices.get(p["symbol"], p["avg_price"]) - p["avg_price"])
                           for p in self.db.sub_positions(sub_id) if p["symbol"] != signal.symbol)

        def day_after(px):
            return realized_after + new_qty * (px - new_avg) + other_unreal

        base_after = day_after(last)
        frac_after = max(0.0, -base_after) / sub["max_daily_loss"] if sub["max_daily_loss"] > 0 else 0.0
        stress = []
        for sh in shocks:
            px = last * (1 + sh / 100.0)
            d = day_after(px)
            f = max(0.0, -d) / sub["max_daily_loss"] if sub["max_daily_loss"] > 0 else 0.0
            stress.append({"move_pct": sh, "price": round(px, 2), "day_pnl": round(d, 2),
                           "loss_used_pct": round(f * 100, 1), "shield_tier": self.tier_for(f),
                           "breach": d <= -sub["max_daily_loss"]})
        budget_left = max(0.0, sub["max_daily_loss"] + base_after)
        adverse = None
        if new_qty != 0:
            adverse = {"pct": round(budget_left / abs(new_qty) / last * 100, 2), "direction": "down" if new_qty > 0 else "up"}

        acct_before = self.db.net_positions().get(signal.symbol, 0)
        return {
            "symbol": signal.symbol, "side": side, "qty": qty, "price": round(last, 2),
            "verdict": verdict, "checks": checks,
            "limits": {"configured": {"max_daily_loss": sub["max_daily_loss"], "max_position": sub["max_position"],
                                      "max_orders_per_min": sub["max_orders_per_min"]},
                       "effective": {"max_position": lim["max_position"], "max_orders_per_min": lim["max_orders"],
                                     "shield_tier": lim["tier"]}},
            "impact": {"position_before": pos, "pending_qty": pending, "position_after": new_qty,
                       "worst_case_exposure": projected, "avg_price_after": round(new_avg, 2),
                       "notional": round(qty * last, 2), "est_charges": charges,
                       "realized_gross": round(realized_gross, 2),
                       "account_net_before": acct_before, "account_net_after": acct_before + signed,
                       "day_pnl_now": round(day_pnl, 2), "day_pnl_after": round(base_after, 2),
                       "loss_used_pct_now": round(lim["loss_frac"] * 100, 1), "loss_used_pct_after": round(frac_after * 100, 1),
                       "shield_tier_now": lim["tier"], "shield_tier_after": self.tier_for(frac_after)},
            "stress": stress, "adverse_move_to_limit": adverse,
        }
