"""Position & P&L maths. Pure functions + a thin wrapper that reads the database.

Every strategy has its OWN position per symbol (a virtual sub-ledger), so one strategy can be
long while another is short in the same stock. The account position is simply their sum.
"""
import datetime as dt


def apply_fill_to_position(qty, avg, side, fill_qty, price):
    """Return (new_qty, new_avg, realized_gross_pnl). Handles adding, reducing and flipping."""
    signed = fill_qty if side == "BUY" else -fill_qty
    if qty == 0 or (qty > 0) == (signed > 0):                    # opening or adding to a position
        new_qty = qty + signed
        new_avg = (abs(qty) * avg + fill_qty * price) / abs(new_qty)
        return new_qty, new_avg, 0.0
    closing = min(abs(qty), fill_qty)                            # reducing / closing / flipping
    direction = 1 if qty > 0 else -1
    realized = closing * (price - avg) * direction
    new_qty = qty + signed
    if new_qty == 0:
        new_avg = 0.0
    elif (new_qty > 0) != (qty > 0):                             # flipped through zero
        new_avg = price
    else:
        new_avg = avg
    return new_qty, new_avg, realized


def compute_charges(side, qty, price, cfg):
    turnover = qty * price
    brokerage = min(cfg.brokerage_cap, turnover * cfg.brokerage_pct)
    stt = turnover * cfg.stt_sell_pct if side == "SELL" else 0.0
    exchange = turnover * cfg.exchange_pct
    gst = (brokerage + exchange) * cfg.gst_pct
    return round(brokerage + stt + exchange + gst, 4)


def day_start(now):
    d = dt.datetime.fromtimestamp(now)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class Ledger:
    def __init__(self, db, settings, prices, clock):
        self.db, self.cfg, self.prices, self.clock = db, settings, prices, clock

    def unrealized(self, sub_id):
        total = 0.0
        for p in self.db.sub_positions(sub_id):
            last = self.prices.get(p["symbol"], p["avg_price"])
            total += p["qty"] * (last - p["avg_price"])
        return total

    def pnl(self, sub_id):
        """Net P&L after charges. 'day' is used by the daily-loss limit."""
        today = self.db.realized_net(sub_id, day_start(self.clock()))
        total = self.db.realized_net(sub_id)
        unreal = self.unrealized(sub_id)
        return {
            "realized_net": round(total["v"], 2),
            "charges": round(total["c"], 2),
            "unrealized": round(unreal, 2),
            "total_net": round(total["v"] + unreal, 2),
            "day_net": round(today["v"] + unreal, 2),
        }
