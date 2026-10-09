"""Candle engine: raw ticks -> candles of any interval.

Interval specs:  "1m" "5m" "15m" "1h"  (time based)   "50t" "100t"  (tick based)
"""
import re
from collections import deque
from .models import Candle

_SPEC = re.compile(r"^(\d+)([smht])$")
_UNIT = {"s": 1, "m": 60, "h": 3600}


def parse_spec(spec: str):
    """Return ('time', seconds) or ('ticks', n). Raises ValueError for bad specs."""
    m = _SPEC.match(spec.strip().lower())
    if not m:
        raise ValueError(f"Bad interval '{spec}'. Use e.g. 1m, 5m, 15m, 1h, 30s or 50t (50 ticks).")
    n, unit = int(m.group(1)), m.group(2)
    if n <= 0:
        raise ValueError("Interval must be positive.")
    if unit == "t":
        if n < 2 or n > 10000:
            raise ValueError("Tick candles must be between 2 and 10000 ticks.")
        return "ticks", n
    seconds = n * _UNIT[unit]
    if seconds < 5 or seconds > 86400:
        raise ValueError("Time candles must be between 5 seconds and 24 hours.")
    return "time", seconds


class CandleBuilder:
    def __init__(self, symbol, spec):
        self.symbol, self.spec = symbol, spec.strip().lower()
        self.kind, self.size = parse_spec(self.spec)
        self.current = None

    def on_tick(self, tick):
        """Feed one tick. Returns the *closed* candle if this tick closed one, else None."""
        closed = None
        if self.kind == "time":
            bucket = int(tick.ts // self.size) * self.size
            if self.current is not None and bucket != self.current.start_ts:
                closed, self.current = self.current, None
            if self.current is None:
                self.current = self._new(bucket, tick)
            else:
                self._add(tick)
        else:  # tick based
            if self.current is None:
                self.current = self._new(tick.ts, tick)
            else:
                self._add(tick)
            if self.current.ticks >= self.size:
                closed, self.current = self.current, None
        return closed

    def _new(self, start, t):
        return Candle(self.symbol, self.spec, start, t.price, t.price, t.price, t.price, t.size, 1)

    def _add(self, t):
        c = self.current
        c.high, c.low, c.close = max(c.high, t.price), min(c.low, t.price), t.price
        c.volume += t.size
        c.ticks += 1


class CandleEngine:
    def __init__(self, symbols, intervals=("1m", "5m"), history=500):
        self.symbols = list(symbols)
        self.specs = []
        self._builders = {}
        self._history = {}
        self._maxlen = history
        for s in intervals:
            self.add_interval(s)

    def add_interval(self, spec):
        spec = spec.strip().lower()
        parse_spec(spec)                       # validates
        if spec in self.specs:
            return spec
        self.specs.append(spec)
        for sym in self.symbols:
            self._builders[(sym, spec)] = CandleBuilder(sym, spec)
            self._history[(sym, spec)] = deque(maxlen=self._maxlen)
        return spec

    def on_tick(self, tick):
        closed = []
        for spec in self.specs:
            b = self._builders.get((tick.symbol, spec))
            if b is None:
                continue
            c = b.on_tick(tick)
            if c:
                self._history[(tick.symbol, spec)].append(c)
                closed.append(c)
        return closed

    def history(self, symbol, spec, n=None):
        h = list(self._history.get((symbol, spec.lower()), []))
        return h if n is None else h[-n:]

    def current(self, symbol, spec):
        b = self._builders.get((symbol, spec.lower()))
        return b.current if b else None

    def reset(self):
        for k, b in self._builders.items():
            b.current = None
            self._history[k].clear()
