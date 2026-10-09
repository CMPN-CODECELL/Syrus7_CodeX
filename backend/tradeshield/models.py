"""Plain data classes shared by every module."""
from dataclasses import dataclass, field, asdict
from typing import Optional

TERMINAL = {"FILLED", "CANCELLED", "REJECTED", "BLOCKED", "FAILED", "EXPIRED"}
NON_TERMINAL = {"NEW", "SENT", "UNKNOWN", "OPEN", "PARTIAL"}


@dataclass
class Tick:
    symbol: str
    price: float
    ts: float            # (simulated or exchange) epoch seconds
    size: int = 1
    open: Optional[float] = None     # today's open price if the feed provides it


@dataclass
class Candle:
    symbol: str
    interval: str        # "1m", "5m", "15m", "1h", "50t" ...
    start_ts: float
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    ticks: int = 0

    def to_dict(self):
        return asdict(self)


@dataclass
class Signal:
    """What a strategy is allowed to produce: an *intent*, never a real order."""
    symbol: str
    side: str            # "BUY" | "SELL"
    qty: int
    reason: str = ""
    order_type: str = "MARKET"
    limit_price: Optional[float] = None


@dataclass
class Flatten:
    """Returned by a strategy to say "square me off": the PLATFORM cancels its open orders and closes its positions."""
    reason: str = ""


@dataclass
class OrderRequest:
    client_order_id: str
    symbol: str
    side: str
    qty: int
    order_type: str = "MARKET"
    limit_price: Optional[float] = None


@dataclass
class Fill:
    fill_id: str
    client_order_id: str
    symbol: str
    side: str
    qty: int
    price: float
    ts: float = 0.0
    charges: Optional[float] = None      # None => platform computes charges


@dataclass
class BrokerOrder:
    client_order_id: str
    symbol: str
    side: str
    qty: int
    order_type: str = "MARKET"
    limit_price: Optional[float] = None
    status: str = "OPEN"                 # OPEN | PARTIAL | FILLED | CANCELLED | REJECTED
    filled_qty: int = 0
    avg_price: float = 0.0
    reject_reason: str = ""
    fills: list = field(default_factory=list)


@dataclass
class Decision:
    allowed: bool
    code: str
    message: str


@dataclass
class State:
    """Global platform flags shared by risk engine, gateway and engine."""
    killed: bool = False
    paused: bool = False          # True while reconciling after a restart
    pause_reason: str = ""
