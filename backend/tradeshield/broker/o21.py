"""Adapter for the REAL 021 Trade developer sandbox (https://devapi.021.trade).

What the 021 API looks like (from the organisers' API guide):
  * login with UCC + password -> bearer token (one live token per account, valid until 5 AM IST)
  * prices are integer PAISE, quantity sign is the side (+ buy / - sell), instruments are identified by a numeric `token`
  * there is NO client order id: the exchange gives us an `orderId` after the order is accepted
  * live prices: binary market websocket.  Order/fill events: binary orders websocket (not replayed after a reconnect)
  * the sandbox misbehaves on purpose: 429 rate limits, random 500/503, slow responses

How this adapter keeps the platform's guarantees on top of that:
  * IDEMPOTENCY WITHOUT A CLIENT ID: before sending, `client_order_id` is saved in a small JSON map. After the
    order is accepted the 021 `orderId` is stored next to it. If an acknowledgement is lost (timeout / 5xx), we do
    NOT resend blindly: we look through GET /orders for an unmapped order that matches (token, side, qty, time).
    Only if none exists is it safe to resend.
  * FILLS: the orders websocket is only a "something changed" trigger. The truth always comes from REST
    (GET /orders and GET /orders/{id}/trades), and each trade becomes one Fill with a unique id, so replays never double count.
  * RATE LIMITS / OUTAGES: a client-side throttle, Retry-After handling, automatic re-login on 401, retries for
    read calls. Order placement is never blindly retried here (the gateway decides, after asking us whether the order exists).

Everything that touches the network goes through `Transport` / `websockets`, so the logic is unit-tested offline
(tests/test_o21.py) against a fake 021 server.
"""
import asyncio
import csv
import gzip
import io
import json
import logging
import os
import re
import struct
import time

from .base import Broker, BrokerRejected
from ..models import Tick, Fill, BrokerOrder, OrderRequest

log = logging.getLogger("tradeshield.o21")

OPEN_STATUSES = {"received", "placed", "pending", "frozen", "sentformodification",
                 "sentforcancellation", "acceptedforamo"}
EXCH_CODE_NSE_CASH = 1
PACKET_LEN = {1: 220, 2: 234, 3: 36, 4: 262, 5: 262, 6: 262}          # TC 3 (full) length by exchange code
_TRANSIENT = re.compile(
    r"internal server|temporar|try again|retry|time[d ]?-?out|unavailable|bad gateway|upstream|connection|"
    r"overload|busy|something went wrong|service error|rate.?limit|too many|throttl", re.I)


class O21NotFound(Exception):
    """HTTP 404 from 021 (unknown order id)."""


# ===================================================================== transport
class HttpResponse:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.body = body or b""

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8")) if self.body else None
        except (ValueError, UnicodeDecodeError):
            return None

    @property
    def text(self):
        return self.body.decode("utf-8", "replace")


class HttpxTransport:
    """Production transport (httpx). Network problems become builtin exceptions the gateway understands."""

    def __init__(self, base_url, timeout):
        import httpx
        self._httpx = httpx
        self.client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)

    async def request(self, method, path, json_body=None, params=None, headers=None):
        try:
            r = await self.client.request(method, path, json=json_body, params=params, headers=headers)
        except self._httpx.TimeoutException as e:
            raise asyncio.TimeoutError(str(e) or "request timed out")
        except self._httpx.TransportError as e:
            raise ConnectionError(f"{type(e).__name__}: {e}")
        return HttpResponse(r.status_code, dict(r.headers), r.content)

    async def close(self):
        await self.client.aclose()


# ===================================================================== REST client
class O21Client:
    """Auth + throttling + error classification for the 021 REST API."""

    def __init__(self, transport, username, password, max_rps=10.0, clock=time.time, incident=None):
        self.t, self.user, self.pw, self.clock = transport, username, password, clock
        self.min_gap = 1.0 / max(0.5, max_rps)
        self.token = ""
        self._last = 0.0
        self._pause_until = 0.0
        self._gate = asyncio.Lock()
        self._login_lock = asyncio.Lock()
        self.incident = incident or (lambda kind, msg: None)

    async def _throttle(self):
        async with self._gate:
            wait = max(self._last + self.min_gap, self._pause_until) - self.clock()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = self.clock()

    def _backoff(self, resp, default):
        try:
            secs = float(resp.headers.get("retry-after", default))
        except ValueError:
            secs = default
        self._pause_until = max(self._pause_until, self.clock() + min(max(secs, 0.2), 5.0))

    @staticmethod
    def _error_text(resp):
        body = resp.json()
        if isinstance(body, dict) and body.get("error"):
            return str(body["error"])
        return resp.text[:200]

    async def login(self):
        async with self._login_lock:
            await self._throttle()
            r = await self.t.request("POST", "/auth/token", json_body={"username": self.user, "password": self.pw})
            body = r.json()
            if r.status == 200 and isinstance(body, dict) and body.get("success") and body.get("data"):
                self.token = body["data"]["accessToken"]
                return body["data"]
            if r.status in (400, 401, 403):
                raise BrokerRejected(f"021 login refused (HTTP {r.status}): {self._error_text(r)}. "
                                     "Use your UCC (HACK....) as the username, not your email.")
            raise ConnectionError(f"021 login failed: HTTP {r.status} {self._error_text(r)}")

    async def call(self, method, path, json_body=None, params=None, idempotent=True, raw=False):
        """Return the payload (envelope unwrapped) or raw bytes.

        Raises: BrokerRejected (definitive refusal), O21NotFound (404), ConnectionError / TimeoutError (outcome unknown).
        `idempotent=False` (order placement) means: never retry inside here.
        """
        max_tries = 3 if idempotent else 1
        tries, relogged, last = 0, False, None
        while tries < max_tries:
            await self._throttle()
            used_token = self.token
            try:
                r = await self.t.request(method, path, json_body=json_body, params=params,
                                         headers={"Authorization": f"Bearer {used_token}"})
            except (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError) as e:
                tries += 1
                last = e
                if tries >= max_tries:
                    raise
                await asyncio.sleep(0.2 * tries)
                continue
            if r.status == 401 and not relogged:                 # token expired or revoked: log in again (not a retry)
                relogged = True
                if self.token == used_token:
                    self.incident("O21_RELOGIN", "021 access token expired or revoked - logging in again")
                    await self.login()
                continue
            if r.status == 200:
                if raw:
                    return r.body
                body = r.json()
                if isinstance(body, dict) and "success" in body:
                    if not body["success"]:
                        raise BrokerRejected(str(body.get("error") or "rejected"))
                    return body.get("data")
                return body
            text = self._error_text(r)
            transient = r.status in (429, 502, 503, 504) or (r.status == 500 and (not text or bool(_TRANSIENT.search(text))))
            if transient:
                tries += 1
                self._backoff(r, 2.0 if r.status == 429 else 1.0)
                last = ConnectionError(f"021 HTTP {r.status} {text}".strip())
                self.incident("O21_RETRY", f"021 {method} {path} -> HTTP {r.status} ({text or 'no body'})")
                continue
            if r.status == 404:
                raise O21NotFound(text)
            raise BrokerRejected(text if r.status == 500 else f"HTTP {r.status}: {text}")   # 400/401/403/422 or a risk rejection
        raise last or ConnectionError("021 request failed")


# ===================================================================== order map
class OrderMap:
    """client_order_id -> {order_id, token, symbol, side, qty, price, sent_at}. Survives restarts (JSON file)."""

    def __init__(self, path):
        self.path, self.d = path, {}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    self.d = json.load(fh)
            except (OSError, ValueError):
                self.d = {}

    def get(self, coid):
        return self.d.get(coid)

    def put(self, coid, entry):
        self.d[coid] = entry
        self.save()
        return entry

    def mapped_ids(self):
        return {e["order_id"] for e in self.d.values() if e.get("order_id")}

    def coid_for(self, order_id):
        for k, e in self.d.items():
            if e.get("order_id") == order_id:
                return k
        return None

    def save(self):
        if not self.path:
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.d, fh)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("could not save order map: %s", e)


# ===================================================================== binary parsing (pure functions)
def parse_market_frame(data: bytes):
    """Parse one market-socket frame into dicts {exch, token, ltp, open, volume, last_qty} (prices in paise).

    A frame holds several packets back to back. TC 1 = LTP (12 bytes), TC 3 = full packet (length depends on the
    exchange), TC 10 = heartbeat (2 bytes). An unknown packet ends parsing (its length is unknown).
    """
    out, i, n = [], 0, len(data)
    while i + 2 <= n:
        tc = struct.unpack_from(">H", data, i)[0]
        if tc == 10:
            i += 2
        elif tc == 1:
            if i + 12 > n:
                break
            exch, token, ltp = struct.unpack_from(">HII", data, i + 2)
            out.append({"exch": exch, "token": token, "ltp": ltp, "open": 0, "volume": None, "last_qty": 0})
            i += 12
            if i < n and data[i] in (0x6F, 0x63):          # ltpo/ltpc snapshot extra: 'o'/'c' + 4 bytes
                i += 5
        elif tc == 3:
            if i + 4 > n:
                break
            exch = struct.unpack_from(">H", data, i + 2)[0]
            length = PACKET_LEN.get(exch)
            if not length or i + length > n:
                break
            p = data[i:i + length]
            if exch == 1:        # NSE cash
                token, ltp, lq = struct.unpack_from(">III", p, 4)
                vol = struct.unpack_from(">Q", p, 20)[0]
                opn = struct.unpack_from(">I", p, 48)[0]
                out.append({"exch": 1, "token": token, "ltp": ltp, "open": opn, "volume": vol, "last_qty": lq})
            elif exch in (4, 5, 6):   # BSE
                token, ltp = struct.unpack_from(">II", p, 4)
                lq = struct.unpack_from(">Q", p, 12)[0]
                vol = struct.unpack_from(">Q", p, 24)[0]
                opn = struct.unpack_from(">I", p, 52)[0]
                out.append({"exch": exch, "token": token, "ltp": ltp, "open": opn, "volume": vol, "last_qty": lq})
            elif exch == 2:      # NSE F&O
                token, ltp, lq = struct.unpack_from(">III", p, 6)
                vol = struct.unpack_from(">Q", p, 22)[0]
                opn = struct.unpack_from(">I", p, 50)[0]
                out.append({"exch": 2, "token": token, "ltp": ltp, "open": opn, "volume": vol, "last_qty": lq})
            i += length
        else:
            break
    return out


def parse_order_event(data: bytes):
    """Orders-socket packet -> (status_code, order_id) or None. TC 4 = NSE (46+ bytes), TC 8 = BSE (32+ bytes)."""
    if len(data) < 4:
        return None
    tc, status = struct.unpack_from(">HH", data, 0)
    if tc == 4 and len(data) >= 38:
        return status, struct.unpack_from(">I", data, 34)[0]
    if tc == 8 and len(data) >= 24:
        return status, struct.unpack_from(">I", data, 20)[0]
    return None


# ===================================================================== the broker
class O21Broker(Broker):
    def __init__(self, settings, transport=None, clock=time.time):
        self.cfg, self.clock = settings, clock
        self.symbols = list(settings.symbols)
        self.transport = transport or HttpxTransport(settings.o21_base_url, settings.o21_timeout)
        self.events = None
        self.api = O21Client(self.transport, settings.o21_username, settings.o21_password,
                             settings.o21_max_rps, clock, incident=self._incident)
        self.map = OrderMap(settings.o21_map_path)
        self.sym2tok, self.tok2sym, self.tick_size = {}, {}, {}
        self.opens = {}
        self._tick_cbs, self._fill_cbs, self._tasks = [], [], []
        self._seen_fills, self._traded, self._locks, self._last_vol = set(), {}, {}, {}
        self._kick = asyncio.Event()
        self._ws_connect = None                         # tests can inject a fake websocket connector

    # ------------------------------------------------------------ plumbing
    def attach_events(self, events):
        self.events = events

    def _incident(self, kind, msg):
        log.warning("%s: %s", kind, msg)
        if self.events:
            try:
                self.events.log("incident", kind, msg)
            except Exception:
                pass

    async def start(self):
        last = None
        for attempt in range(1, 4):
            try:
                await self.api.login()
                break
            except BrokerRejected as e:
                raise RuntimeError(str(e))
            except (ConnectionError, TimeoutError, asyncio.TimeoutError, OSError) as e:
                last = e
                await asyncio.sleep(attempt)
        else:
            raise RuntimeError(f"Cannot reach the 021 sandbox ({last!r}). Check your internet connection.")
        await self.load_instruments()
        log.info("021 broker ready. Instrument tokens: %s", self.sym2tok)
        self._tasks = [asyncio.create_task(self._market_loop()), asyncio.create_task(self._orders_loop()),
                       asyncio.create_task(self._poll_loop())]

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        self._tasks = []
        try:
            await self.transport.close()
        except Exception:
            pass

    # ------------------------------------------------------------ instruments
    @staticmethod
    def _read_csv(raw):
        return list(csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))))

    async def load_instruments(self):
        wanted = {s.upper() for s in self.symbols}
        overrides = {}
        for part in filter(None, (self.cfg.o21_tokens or "").split(",")):
            k, _, v = part.partition("=")
            if v.strip().isdigit():
                overrides[k.strip().upper()] = int(v)
        rows, cache = None, self.cfg.o21_instruments_cache
        if cache and os.path.exists(cache) and time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(cache))) == time.strftime("%Y-%m-%d"):
            with open(cache, "rb") as fh:
                rows = self._read_csv(fh.read())
        if rows is None:
            try:
                raw = await self.api.call("GET", "/instruments", raw=True)
                if raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                rows = self._read_csv(raw)
                if cache:
                    with open(cache, "wb") as fh:
                        fh.write(raw)
            except Exception as e:
                if cache and os.path.exists(cache):                 # tokens rarely change: a stale cache beats nothing
                    self._incident("O21_INSTRUMENTS", f"could not download instruments ({e!r}); using the cached file")
                    with open(cache, "rb") as fh:
                        rows = self._read_csv(fh.read())
                elif overrides:
                    rows = []
                else:
                    raise RuntimeError(f"Could not download the 021 instrument list: {e!r}")
        for row in rows:
            sym = (row.get("symbol") or "").upper()
            if row.get("exchange") == "NSECM" and row.get("instrument_type") == "STK" and sym in wanted and sym not in self.sym2tok:
                self.sym2tok[sym] = int(row["token"])
                self.tick_size[sym] = max(1, int(row.get("ticksize") or 1))
        for sym, tok in overrides.items():
            self.sym2tok[sym] = tok
            self.tick_size.setdefault(sym, 5)
        missing = sorted(wanted - set(self.sym2tok))
        if missing:
            raise RuntimeError(f"Could not find NSE tokens for {missing}. Fix TS_SYMBOLS, or set O21_TOKENS=SYMBOL=token,...")
        self.tok2sym = {t: s for s, t in self.sym2tok.items()}

    # ------------------------------------------------------------ orders
    def _lock(self, coid):
        return self._locks.setdefault(coid, asyncio.Lock())

    def _order_payload(self, e):
        return {"exchange": self.cfg.o21_exchange, "token": e["token"], "qty": e["qty"] if e["side"] == "BUY" else -e["qty"],
                "price": e["price"], "book": "RL", "product": self.cfg.o21_product, "validity": "Day"}

    async def place_order(self, req: OrderRequest) -> BrokerOrder:
        coid = req.client_order_id
        async with self._lock(coid):
            e = self.map.get(coid)
            if e and e.get("rejected"):
                raise BrokerRejected(e["rejected"])
            if e and e.get("order_id"):                                  # already placed: same id => same order
                return await self._view(coid)
            if e and await self._adopt(coid):                            # an earlier attempt may have got through
                return await self._view(coid)
            if req.symbol not in self.sym2tok:
                raise BrokerRejected(f"unknown symbol {req.symbol}")
            price = 0
            if req.order_type == "LIMIT" and req.limit_price:
                ts = self.tick_size.get(req.symbol, 5)
                price = int(round(req.limit_price * 100 / ts)) * ts
            e = self.map.put(coid, {"order_id": None, "token": self.sym2tok[req.symbol], "symbol": req.symbol,
                                    "side": req.side, "qty": req.qty, "price": price, "sent_at": self.clock()})
            try:
                data = await self.api.call("POST", "/orders", json_body=self._order_payload(e), idempotent=False)
            except BrokerRejected as ex:
                e["rejected"] = str(ex)
                self.map.save()
                raise
            oid = int((data or {}).get("orderId") or 0)
            if not oid:
                raise ConnectionError("021 accepted the order but returned no orderId")
            e["order_id"] = oid
            self.map.save()
            self._kick.set()
            return BrokerOrder(coid, req.symbol, req.side, req.qty, req.order_type, req.limit_price, status="OPEN")

    async def _adopt(self, coid):
        """Look for an order we sent but never got an id for. True if found (and now mapped)."""
        e = self.map.get(coid)
        if e is None:
            return False
        if e.get("order_id"):
            return True
        taken, best = self.map.mapped_ids(), None
        for o in await self.api.call("GET", "/orders") or []:
            oid = o.get("orderId")
            if oid in taken or o.get("token") != e["token"] or o.get("time", 0) < e["sent_at"] - 5:
                continue
            if o.get("placedBy", "User") != "User":
                continue
            rem, traded = int(o.get("qtyRemaining", 0)), abs(int(o.get("qtyTraded", 0)))
            if abs(rem) + traded != e["qty"]:
                continue
            if rem and (rem > 0) != (e["side"] == "BUY"):
                continue
            if e["price"] and int(o.get("price", 0)) != e["price"]:
                continue
            if best is None or (o.get("time", 0), oid) < (best.get("time", 0), best["orderId"]):
                best = o
        if best is None:
            return False
        e["order_id"] = best["orderId"]
        self.map.save()
        self._incident("ORDER_ADOPTED", f"{coid}: acknowledgement was lost but 021 order #{best['orderId']} matches - adopted, NOT resent")
        return True

    def _resolve(self, coid):
        """Map entry for a client id; also understands 'O21-<orderId>' ids used for orders we did not create."""
        e = self.map.get(coid)
        if e is None and coid.startswith("O21-") and coid[4:].isdigit():
            e = {"order_id": int(coid[4:]), "token": None, "symbol": None, "side": None, "qty": 0, "price": 0, "sent_at": 0}
        return e

    async def _view(self, coid):
        e = self._resolve(coid)
        arr = await self.api.call("GET", f"/orders/{e['order_id']}")
        if not arr:
            raise O21NotFound(f"order {e['order_id']}")
        return await self._to_broker_order(coid, e, arr[0])

    async def _to_broker_order(self, coid, e, o, with_fills=True):
        sym = e.get("symbol") or self.tok2sym.get(o.get("token"), str(o.get("token")))
        rem = int(o.get("qtyRemaining", 0))
        side = e.get("side") or ("BUY" if rem > 0 else "SELL")
        traded = abs(int(o.get("qtyTraded", 0)))
        qty = e.get("qty") or (abs(rem) + traded)
        s = str(o.get("status", "")).lower()
        status = ("FILLED" if s == "executed" else "REJECTED" if s == "rejected" else "CANCELLED" if s == "cancelled"
                  else "PARTIAL" if traded > 0 else "OPEN")
        bo = BrokerOrder(coid, sym, side, qty, "LIMIT" if o.get("price") else "MARKET",
                         (o.get("price") or 0) / 100 or None, status=status, filled_qty=traded)
        bo.reject_reason = o.get("reason", "") or ""
        if with_fills and traded > 0:
            bo.fills = await self._fetch_fills(coid, e, e.get("order_id") or o.get("orderId"), sym, side)
            if bo.fills:
                tot = sum(f.qty for f in bo.fills)
                bo.filled_qty = tot
                bo.avg_price = round(sum(f.qty * f.price for f in bo.fills) / tot, 4)
        return bo

    async def _fetch_fills(self, coid, e, oid, sym=None, side=None):
        sym = sym or e.get("symbol") or ""
        side = side or e.get("side")
        out = []
        for i, t in enumerate(await self.api.call("GET", f"/orders/{oid}/trades") or []):
            q = int(t.get("quantity", 0))
            tid = t.get("tradeId", f"{t.get('tradeTime', 0)}-{i}")
            out.append(Fill(f"{oid}-{tid}", coid, sym or t.get("symbol", ""), side or ("BUY" if q > 0 else "SELL"),
                            abs(q), int(t.get("price", 0)) / 100.0, float(t.get("tradeTime", 0)), None))
        return out

    async def get_order(self, coid):
        e = self._resolve(coid)
        if e is None:
            return None
        if e.get("rejected"):
            bo = BrokerOrder(coid, e["symbol"], e["side"], e["qty"], status="REJECTED")
            bo.reject_reason = e["rejected"]
            return bo
        if not e.get("order_id") and not await self._adopt(coid):
            return None
        try:
            return await self._view(coid)
        except O21NotFound:
            return None

    async def cancel_order(self, coid):
        e = self._resolve(coid)
        if e is None or e.get("rejected"):
            return None
        if not e.get("order_id") and not await self._adopt(coid):
            return None
        try:
            await self.api.call("DELETE", f"/orders/{e['order_id']}", json_body={
                "exchange": self.cfg.o21_exchange, "token": e["token"] or 0, "product": self.cfg.o21_product})
        except BrokerRejected:
            pass                                          # probably already filled/cancelled: read the truth below
        except O21NotFound:
            return None
        bo = None
        for _ in range(16):                               # cancellation is asynchronous: wait until it is final
            bo = await self._view(coid)
            if bo.status in ("CANCELLED", "FILLED", "REJECTED"):
                break
            await asyncio.sleep(0.25)
        return bo

    async def get_open_orders(self):
        out = []
        for o in await self.api.call("GET", "/orders") or []:
            if str(o.get("status", "")).lower() not in OPEN_STATUSES or o.get("token") not in self.tok2sym:
                continue
            coid = self.map.coid_for(o["orderId"]) or f"O21-{o['orderId']}"
            e = self.map.get(coid) or {"order_id": o["orderId"]}
            out.append(await self._to_broker_order(coid, e, o, with_fills=False))
        return out

    async def get_positions(self):
        pos = {}
        for p in await self.api.call("GET", "/portfolio/positions") or []:
            sym = self.tok2sym.get(p.get("token"))
            if sym and p.get("exchange") == "NSECM" and int(p.get("netQuantity", 0)):
                pos[sym] = pos.get(sym, 0) + int(p["netQuantity"])
        return {k: v for k, v in pos.items() if v}

    # ------------------------------------------------------------ callbacks
    def subscribe_ticks(self, cb): self._tick_cbs.append(cb)
    def unsubscribe_ticks(self, cb): self._tick_cbs.remove(cb) if cb in self._tick_cbs else None
    def subscribe_fills(self, cb): self._fill_cbs.append(cb)
    def unsubscribe_fills(self, cb): self._fill_cbs.remove(cb) if cb in self._fill_cbs else None

    # ------------------------------------------------------------ market data
    def on_market_frame(self, data: bytes):
        for p in parse_market_frame(data):
            sym = self.tok2sym.get(p["token"]) if p["exch"] == EXCH_CODE_NSE_CASH else None
            if not sym or p["ltp"] <= 0:
                continue
            size = 1
            if p["volume"] is not None:
                prev = self._last_vol.get(sym)
                size = max(1, p["volume"] - prev) if prev is not None and p["volume"] > prev else max(1, p["last_qty"])
                self._last_vol[sym] = p["volume"]
            opn = p["open"] / 100.0 if p["open"] else None
            if opn:
                self.opens[sym] = opn
            tick = Tick(sym, p["ltp"] / 100.0, self.clock(), size, opn)
            for cb in list(self._tick_cbs):
                try:
                    cb(tick)
                except Exception:
                    log.exception("tick callback failed")

    async def _ephemeral_key(self):
        return (await self.api.call("GET", "/websocket/ephemeral-key"))["token"]

    def _connect(self, url):
        if self._ws_connect:
            return self._ws_connect(url)
        import websockets
        return websockets.connect(url, ping_interval=None, max_size=2 ** 20)

    async def _market_loop(self):
        delay = 1.0
        while True:
            try:
                key = await self._ephemeral_key()
                async with self._connect(f"{self.cfg.o21_ws_base}/market?token={key}") as ws:
                    await ws.send(json.dumps({"Task": "subscribe", "Mode": self.cfg.o21_md_mode,
                                              "Instruments": [[EXCH_CODE_NSE_CASH, t] for t in self.sym2tok.values()]}))
                    delay = 1.0
                    self._incident("O21_FEED", "021 market data connected")
                    async for msg in ws:
                        if isinstance(msg, (bytes, bytearray)):
                            self.on_market_frame(bytes(msg))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._incident("O21_FEED", f"market data disconnected ({e!r}); reconnecting in {delay:.0f}s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _orders_loop(self):
        delay = 1.0
        while True:
            try:
                key = await self._ephemeral_key()
                async with self._connect(f"{self.cfg.o21_ws_base}/orders?token={key}") as ws:
                    delay = 1.0
                    self._kick.set()                              # events while disconnected are NOT replayed: catch up now
                    async for msg in ws:
                        if isinstance(msg, (bytes, bytearray)) and parse_order_event(bytes(msg)):
                            self._kick.set()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._incident("O21_FEED", f"order updates disconnected ({e!r}); reconnecting in {delay:.0f}s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    # ------------------------------------------------------------ fills
    async def sync_fills(self):
        """One pass: find orders whose traded quantity grew and emit their new trades as Fills (each only once)."""
        for o in await self.api.call("GET", "/orders") or []:
            oid = o.get("orderId")
            coid = self.map.coid_for(oid)
            if coid is None:
                continue
            if abs(int(o.get("qtyTraded", 0))) <= self._traded.get(oid, 0):
                continue
            fills = await self._fetch_fills(coid, self.map.get(coid), oid)
            for f in fills:
                if f.fill_id not in self._seen_fills:
                    self._seen_fills.add(f.fill_id)
                    for cb in list(self._fill_cbs):
                        try:
                            cb(f)
                        except Exception:
                            log.exception("fill callback failed")
            self._traded[oid] = sum(f.qty for f in fills)

    async def _poll_loop(self):
        while True:
            try:
                await asyncio.wait_for(self._kick.wait(), self.cfg.o21_poll_interval)
            except asyncio.TimeoutError:
                pass
            self._kick.clear()
            try:
                await self.sync_fills()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("fill sync failed: %r", e)
