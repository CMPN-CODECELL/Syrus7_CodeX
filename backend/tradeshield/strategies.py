"""Example strategies. A strategy only READS candles/positions and returns Signals.
It never touches the broker - the platform's risk engine and gateway stand in between.
(Profitability is irrelevant for this problem; these exist to exercise the platform.)
"""
import datetime as dt
import statistics
from .models import Signal, Flatten

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def ist_now(epoch):
    """Wall-clock time in India (the exchange's clock) for an epoch timestamp."""
    return dt.datetime.fromtimestamp(epoch, IST)


class Ctx:
    """What a strategy is allowed to see."""
    def __init__(self, engine, sub):
        self._e, self.sub, self.params = engine, sub, sub["params"]

    def candles(self, symbol, interval, n=None):
        return self._e.candles.history(symbol, interval, n)

    def position(self, symbol):
        return self._e.db.get_position(self.sub["id"], symbol)["qty"]

    def price(self, symbol):
        return self._e.prices.get(symbol)

    def open_price(self, symbol):
        """Today's open for the symbol (from the exchange feed; falls back to the first price seen today)."""
        return self._e.open_price(symbol)

    def avg_price(self, symbol):
        return self._e.db.get_position(self.sub["id"], symbol)["avg_price"]

    def has_open_orders(self):
        return bool(self._e.db.open_orders(self.sub["id"]))

    def now(self):
        return ist_now(self._e.clock())


class Strategy:
    key, name, description = "", "", ""
    symbols = []
    demo = False
    default_limits = {"max_daily_loss": 5000.0, "max_position": 30, "max_orders_per_min": 10}
    default_params = {}
    wants_ticks = False      # True => the platform also calls on_tick() for every price update
    wants_time = False       # True => the platform also calls on_time() about once a second

    def required_intervals(self, params):
        return []

    def on_candle(self, candle, ctx):
        return []

    def on_tick(self, tick, ctx):
        return []

    def on_time(self, now, ctx):
        """`now` is an IST datetime. May return Signals or a Flatten()."""
        return []


def _ema(values, n):
    k, e = 2 / (n + 1), values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


class TrendBreakout(Strategy):
    key, name = "trend_breakout", "Trend Breakout"
    description = "5-min EMA trend + 1-min breakout entry (uses 1m and 5m candles). Goes long or short."
    symbols = ["TCS"]
    default_params = {"qty": 10}

    def required_intervals(self, params):
        return ["1m", "5m"]

    def on_candle(self, c, ctx):
        if c.interval != "1m":
            return []
        five = [x.close for x in ctx.candles(c.symbol, "5m")]
        one = ctx.candles(c.symbol, "1m")
        if len(five) < 4 or len(one) < 6:
            return []
        trend = _ema(five, 2) - _ema(five, 4)
        prev = one[-6:-1]
        pos, qty = ctx.position(c.symbol), int(ctx.params.get("qty", 10))
        if pos == 0 and trend > 0 and c.close > max(x.high for x in prev):
            return [Signal(c.symbol, "BUY", qty, "5m uptrend + 1m breakout above 5-bar high")]
        if pos == 0 and trend < 0 and c.close < min(x.low for x in prev):
            return [Signal(c.symbol, "SELL", qty, "5m downtrend + 1m breakdown below 5-bar low")]
        if pos > 0 and trend < 0:
            return [Signal(c.symbol, "SELL", pos, "exit long: 5m trend turned down")]
        if pos < 0 and trend > 0:
            return [Signal(c.symbol, "BUY", -pos, "exit short: 5m trend turned up")]
        return []


class MeanReversion(Strategy):
    key, name = "mean_reversion", "Mean Reversion"
    description = "Fades stretched moves (z-score). Candle interval is configurable (default 1m). Same stock as Trend Breakout, so it is often on the opposite side."
    symbols = ["TCS"]
    default_params = {"interval": "1m", "qty": 10, "lookback": 10}

    def required_intervals(self, params):
        return [params.get("interval", "1m")]

    def on_candle(self, c, ctx):
        n = int(ctx.params.get("lookback", 10))
        closes = [x.close for x in ctx.candles(c.symbol, c.interval, n)]
        if len(closes) < n:
            return []
        sd = statistics.pstdev(closes)
        if sd == 0:
            return []
        z, pos, qty = (c.close - statistics.mean(closes)) / sd, ctx.position(c.symbol), int(ctx.params.get("qty", 10))
        if pos == 0 and z > 1.5:
            return [Signal(c.symbol, "SELL", qty, f"z-score {z:.2f} > 1.5: price stretched up, fade it")]
        if pos == 0 and z < -1.5:
            return [Signal(c.symbol, "BUY", qty, f"z-score {z:.2f} < -1.5: price stretched down, fade it")]
        if pos > 0 and z > -0.3:
            return [Signal(c.symbol, "SELL", pos, "exit long: back to the mean")]
        if pos < 0 and z < 0.3:
            return [Signal(c.symbol, "BUY", -pos, "exit short: back to the mean")]
        return []


class TickMomentum(Strategy):
    key, name = "tick_momentum", "Tick Momentum"
    description = "Three rising/falling candles in a row. Defaults to 40-tick candles (tick-based interval)."
    symbols = ["INFY"]
    default_params = {"interval": "40t", "qty": 10, "hold": 3}

    def __init__(self):
        self.held = 0

    def required_intervals(self, params):
        return [params.get("interval", "40t")]

    def on_candle(self, c, ctx):
        h = [x.close for x in ctx.candles(c.symbol, c.interval, 3)]
        pos, qty = ctx.position(c.symbol), int(ctx.params.get("qty", 10))
        if pos != 0:
            self.held += 1
            if self.held >= int(ctx.params.get("hold", 3)):
                self.held = 0
                return [Signal(c.symbol, "SELL" if pos > 0 else "BUY", abs(pos), "time exit after holding N candles")]
            return []
        self.held = 0
        if len(h) < 3:
            return []
        if h[0] < h[1] < h[2]:
            return [Signal(c.symbol, "BUY", qty, "3 rising candles")]
        if h[0] > h[1] > h[2]:
            return [Signal(c.symbol, "SELL", qty, "3 falling candles")]
        return []


class RunawayTest(Strategy):
    key, name = "runaway_test", "Runaway (stress test)"
    description = "DELIBERATELY BROKEN: spams 40 buy orders on every candle. Use it to prove that platform limits still hold."
    symbols = ["RELIANCE"]
    demo = True
    default_limits = {"max_daily_loss": 3000.0, "max_position": 20, "max_orders_per_min": 5}
    default_params = {"interval": "1m"}

    def required_intervals(self, params):
        return [params.get("interval", "1m")]

    def on_candle(self, c, ctx):
        return [Signal(c.symbol, "BUY", 10, "runaway loop") for _ in range(40)]


def _hhmm(text, default):
    try:
        h, m = str(text).split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        h, m = default.split(":")
        return int(h) * 60 + int(m)


class TimedEntryExit(Strategy):
    """ORGANISER STRATEGY 1 - time based entry and exit.
    09:15 IST: enter (default BUY 10 RELIANCE, INTRADAY, market order).
    15:15 IST: square off the full position with an opposite order and cancel any of its orders still open.
    Entry is only taken inside a short window after 09:15 (default 5 min) so a late restart does not enter at a random time."""
    key, name = "timed_entry_exit", "Time-Based Entry/Exit"
    description = "Buys at 09:15 IST, squares off at 15:15 IST (cancels its open orders first). No indicators, pure clock."
    symbols = ["RELIANCE"]
    wants_time = True
    default_params = {"qty": 10, "side": "BUY", "entry_time": "09:15", "exit_time": "15:15", "entry_window_min": 5}

    def __init__(self):
        self.day, self.entered, self.exited = None, False, False

    def on_time(self, now, ctx):
        today = now.date().isoformat()
        if today != self.day:
            self.day, self.entered, self.exited = today, False, False
        mins, p = now.hour * 60 + now.minute, ctx.params
        entry, exit_ = _hhmm(p.get("entry_time"), "09:15"), _hhmm(p.get("exit_time"), "15:15")
        sym = self.symbols[0]
        if not self.entered and entry <= mins < min(exit_, entry + int(p.get("entry_window_min", 5))):
            self.entered = True
            side = "SELL" if str(p.get("side", "BUY")).upper() == "SELL" else "BUY"
            return [Signal(sym, side, int(p.get("qty", 10)), f"{p.get('entry_time', '09:15')} IST time-based entry")]
        if self.entered and not self.exited and mins >= exit_:
            self.exited = True
            return [Flatten(f"{p.get('exit_time', '15:15')} IST time-based exit: cancel open orders and square off")]
        return []


class OpenBreakout(Strategy):
    """ORGANISER STRATEGY 2 - 1% breakout from today's open, with the platform managing a 5% target and 5% stop-loss.
    LTP >= open*1.01 -> BUY.  LTP <= open*0.99 -> SELL.  One entry per day.
    After the entry fills (entry price P = average fill price): long exits at P*1.05 (target) or P*0.95 (stop);
    short exits at P*0.95 (target) or P*1.05 (stop). The exit is an opposite order of the same quantity.
    No broker-side target/stop is used. Any position still open at 15:15 IST is squared off."""
    key, name = "open_breakout", "1% Breakout from Open"
    description = "Buys +1% / sells -1% from the day's open, exits at +/-5% from entry (target/stop-loss managed by the platform)."
    symbols = ["INFY"]
    wants_ticks = True
    wants_time = True
    default_params = {"qty": 10, "trigger_pct": 1.0, "target_pct": 5.0, "stop_pct": 5.0, "exit_time": "15:15"}

    def __init__(self):
        self.day, self.entered, self.exited, self.last_action = None, False, False, 0.0

    def _roll(self, ctx):
        today = ctx.now().date().isoformat()
        if today != self.day:
            self.day, self.entered, self.exited, self.last_action = today, False, False, 0.0

    def on_tick(self, tick, ctx):
        self._roll(ctx)
        sym, p = tick.symbol, ctx.params
        pos, ltp = ctx.position(sym), tick.price
        now = ctx.now().timestamp()
        if pos == 0:
            opn = ctx.open_price(sym)
            if self.entered or not opn or opn <= 0:
                return []
            t = float(p.get("trigger_pct", 1.0)) / 100.0
            qty = int(p.get("qty", 10))
            if ltp >= opn * (1 + t):
                self.entered, self.last_action = True, now
                return [Signal(sym, "BUY", qty, f"LTP {ltp:.2f} >= open {opn:.2f} +{t * 100:g}%: breakout up")]
            if ltp <= opn * (1 - t):
                self.entered, self.last_action = True, now
                return [Signal(sym, "SELL", qty, f"LTP {ltp:.2f} <= open {opn:.2f} -{t * 100:g}%: breakdown")]
            return []
        if ctx.has_open_orders() or now - self.last_action < 3:      # an entry/exit order is still in flight
            return []
        entry = ctx.avg_price(sym)
        tgt, stp = float(p.get("target_pct", 5.0)) / 100.0, float(p.get("stop_pct", 5.0)) / 100.0
        exit_side, why = ("SELL" if pos > 0 else "BUY"), None
        if pos > 0:
            if ltp >= entry * (1 + tgt):
                why = f"target hit: LTP {ltp:.2f} >= {entry * (1 + tgt):.2f} (entry {entry:.2f} +{tgt * 100:g}%)"
            elif ltp <= entry * (1 - stp):
                why = f"stop-loss hit: LTP {ltp:.2f} <= {entry * (1 - stp):.2f} (entry {entry:.2f} -{stp * 100:g}%)"
        else:
            if ltp <= entry * (1 - tgt):
                why = f"target hit: LTP {ltp:.2f} <= {entry * (1 - tgt):.2f} (short entry {entry:.2f} -{tgt * 100:g}%)"
            elif ltp >= entry * (1 + stp):
                why = f"stop-loss hit: LTP {ltp:.2f} >= {entry * (1 + stp):.2f} (short entry {entry:.2f} +{stp * 100:g}%)"
        if why:
            self.last_action = now
            return [Signal(sym, exit_side, abs(pos), why)]
        return []

    def on_time(self, now, ctx):
        self._roll(ctx)
        if not self.exited and now.hour * 60 + now.minute >= _hhmm(ctx.params.get("exit_time"), "15:15"):
            if any(ctx.position(s) != 0 for s in self.symbols) or ctx.has_open_orders():
                self.exited = True
                return [Flatten("15:15 IST: square off before market close")]
        return []


REGISTRY = {cls.key: cls for cls in (TrendBreakout, MeanReversion, TickMomentum, TimedEntryExit, OpenBreakout, RunawayTest)}


def catalog():
    return [{"key": c.key, "name": c.name, "description": c.description, "symbols": c.symbols, "demo": c.demo,
             "default_limits": c.default_limits, "default_params": c.default_params}
            for c in REGISTRY.values()]
