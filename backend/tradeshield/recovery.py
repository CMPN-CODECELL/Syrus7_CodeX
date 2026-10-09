"""Crash recovery & reconciliation.

After a restart (or on demand) the platform does NOT trust its own memory:
  1. pause trading
  2. for every order we think is still alive, ask the broker for the truth (by client_order_id)
  3. apply any fills we missed (each fill is applied once - fill_id is unique)
  4. cancel orders the broker holds that we don't know about
  5. compare account positions: our sub-ledgers (summed) vs the broker's; fix any mismatch
  6. resume trading
"""
import asyncio

from .models import NON_TERMINAL
from .gateway import NET_ERRORS


async def _retry(coro_fn, attempts=6, delay=0.5):
    last = None
    for _ in range(attempts):
        try:
            return await coro_fn()
        except NET_ERRORS as e:
            last = e
            await asyncio.sleep(delay)
    raise last


async def reconcile(engine, reason="manual"):
    st, db, broker, gw, ev = engine.state, engine.db, engine.broker, engine.gateway, engine.events
    st.paused, st.pause_reason = True, f"reconciling ({reason})"
    report = {"reason": reason, "orders_checked": 0, "fills_recovered": 0, "orders_expired": 0,
              "orphans_cancelled": 0, "position_fixes": []}
    try:
        # 2+3: orders we believe are alive -> ask the broker for the truth, apply missed fills once
        for row in db.open_orders():
            report["orders_checked"] += 1
            coid = row["client_order_id"]
            bo = await _retry(lambda: asyncio.wait_for(broker.get_order(coid), engine.settings.order_timeout))
            if bo is None:
                # Persisted but never seen by the broker => it never reached the market.
                # Do NOT resend after a restart: the signal is stale. Close the record.
                db.update_order(coid, status="EXPIRED", reason="never reached the broker (found during reconciliation)")
                report["orders_expired"] += 1
                continue
            for f in bo.fills:
                if gw.apply_fill(f):
                    report["fills_recovered"] += 1
            gw.sync_from_broker(bo)

        # 4: orders the broker holds that we do not know about
        for bo in await _retry(lambda: asyncio.wait_for(broker.get_open_orders(), engine.settings.order_timeout)):
            if db.get_order(bo.client_order_id) is None:
                await broker.cancel_order(bo.client_order_id)
                report["orphans_cancelled"] += 1
                ev.log("incident", "ORPHAN_ORDER", f"cancelled broker order {bo.client_order_id} unknown to the platform")

        # 5: account positions: sum of our sub-ledgers vs the broker
        ours = db.net_positions()
        theirs = await _retry(lambda: asyncio.wait_for(broker.get_positions(), engine.settings.order_timeout))
        for sym in sorted(set(ours) | set(theirs)):
            a, b = int(ours.get(sym, 0)), int(theirs.get(sym, 0))
            if a != b:
                # put the difference in the 'unattributed' bucket (sub_id 0) so the account matches the broker
                cur = db.get_position(0, sym)
                db.set_position(0, sym, cur["qty"] + (b - a), engine.prices.get(sym, cur["avg_price"]))
                report["position_fixes"].append({"symbol": sym, "platform_said": a, "broker_said": b, "corrected_to": b})
    except Exception as e:
        ev.log("incident", "RECOVERY_FAILED", f"Reconciliation failed ({e!r}); trading stays paused until it succeeds")
        raise                                    # stay paused: never trade on unverified state
    st.paused, st.pause_reason = False, ""       # 6: resume

    changed = report["position_fixes"] or report["fills_recovered"] or report["orders_expired"] or report["orphans_cancelled"]
    ev.log("incident", "RECOVERY", _summary(report) if changed else
           "Reconciliation complete: platform state already matches the broker.", None, report)
    return report


def _summary(r):
    parts = [f"{r['orders_checked']} open orders checked"]
    if r["fills_recovered"]:
        parts.append(f"{r['fills_recovered']} missed fills recovered")
    for fx in r["position_fixes"]:
        parts.append(f"{fx['symbol']}: platform said {fx['platform_said']:+d}, broker said {fx['broker_said']:+d} -> corrected")
    if r["orders_expired"]:
        parts.append(f"{r['orders_expired']} orders never reached broker (closed)")
    if r["orphans_cancelled"]:
        parts.append(f"{r['orphans_cancelled']} orphan orders cancelled")
    return "Reconciliation: " + "; ".join(parts)
