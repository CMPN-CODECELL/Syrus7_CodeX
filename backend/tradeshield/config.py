"""Central settings. Everything can be overridden with environment variables or a .env file.

Credentials for the 021 sandbox live in a `.env` file (see ../../.env.example). Never commit that file.
"""
import os
from dataclasses import dataclass, field


def _load_dotenv():
    """Tiny .env loader (no extra dependency). Real environment variables always win."""
    here = os.path.dirname(os.path.abspath(__file__))
    for folder in (os.getcwd(), os.path.join(here, ".."), os.path.join(here, "..", "..")):
        path = os.path.join(folder, ".env")
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                    val = val[1:-1]
                os.environ.setdefault(key, val)


_load_dotenv()


def _f(name, default):
    return float(os.getenv(name, default))


def _default_mode():
    explicit = os.getenv("TS_BROKER")
    if explicit:
        return explicit
    return "021" if os.getenv("O21_USERNAME") else "mock"


_MODE = _default_mode()


@dataclass
class Settings:
    db_path: str = os.getenv("TS_DB_PATH", "tradeshield.db")
    broker_mode: str = _MODE                                    # "021" (real sandbox) or "mock"
    symbols: list = field(default_factory=lambda: [s.strip() for s in os.getenv("TS_SYMBOLS", "TCS,INFY,RELIANCE").split(",") if s.strip()])

    # --- mock exchange pacing: 1 real second == time_scale simulated seconds
    time_scale: float = _f("TS_TIME_SCALE", 10)
    tick_interval: float = _f("TS_TICK_INTERVAL", 0.25)
    mock_state_path: str = os.getenv("TS_MOCK_STATE", "mock_exchange_state.json")

    # --- order handling (the real sandbox is slower than the mock, so the default timeout is longer)
    order_timeout: float = _f("TS_ORDER_TIMEOUT", 4.0 if _MODE == "021" else 2.0)   # seconds to wait for a broker ack
    max_send_attempts: int = int(os.getenv("TS_MAX_SEND_ATTEMPTS", "4"))
    retry_backoff: float = _f("TS_RETRY_BACKOFF", 0.3)
    sync_interval: float = _f("TS_SYNC_INTERVAL", 5.0)           # periodic order re-sync with the broker

    # --- kill switch
    kill_deadline: float = _f("TS_KILL_DEADLINE", 10.0)

    # --- charges model (used when the broker does not report charges itself)
    # TODO at kickoff: replace with the organisers' published charges table.
    brokerage_pct: float = 0.0003
    brokerage_cap: float = 20.0
    stt_sell_pct: float = 0.00025
    exchange_pct: float = 0.0000345
    gst_pct: float = 0.18

    # --- 021 developer sandbox (https://devapi.021.trade). Fill these in .env
    o21_username: str = os.getenv("O21_USERNAME", "")            # your UCC, e.g. HACK1234 (NOT your email)
    o21_password: str = os.getenv("O21_PASSWORD", "")            # the password you chose at sign-up
    o21_base_url: str = os.getenv("O21_BASE_URL", "https://devapi.021.trade/api/developer-api/v1")
    o21_ws_base: str = os.getenv("O21_WS_BASE", "wss://devapi.021.trade/api/developer/websocket")
    o21_exchange: str = os.getenv("O21_EXCHANGE", "NSE")        # request name; NSE cash = websocket code 1
    o21_product: str = os.getenv("O21_PRODUCT", "INTRADAY")
    o21_tokens: str = os.getenv("O21_TOKENS", "")                # optional override, e.g. "TCS=11536,INFY=1594"
    o21_md_mode: str = os.getenv("O21_MD_MODE", "full")          # market socket mode: "full" (has volume + open) or "ltp"
    o21_max_rps: float = _f("O21_MAX_RPS", 10)                   # client-side throttle (requests per second)
    o21_timeout: float = _f("O21_TIMEOUT", 6.0)                  # HTTP timeout per request
    o21_poll_interval: float = _f("O21_POLL_INTERVAL", 2.0)      # fallback fill polling (the orders socket is the fast path)
    o21_map_path: str = os.getenv("O21_MAP_PATH", "o21_order_map.json")        # client_order_id -> 021 orderId
    o21_instruments_cache: str = os.getenv("O21_INSTRUMENTS_CACHE", "o21_instruments.csv")
