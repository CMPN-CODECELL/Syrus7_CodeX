"""Tests for the REAL-021 adapter and the organisers' two strategies.

They run OFFLINE (standard library only) against `Fake021`, a small imitation of the 021 REST API that can be told to
fail in the ways the hackathon sandbox does: lost acknowledgements, 429, 503, random 500, revoked tokens, async cancels.

    python -m unittest discover -s tests -v
"""
import asyncio
import datetime as dt
import gzip
import json
import struct
import time
import unittest

from tradeshield.broker.base import BrokerRejected
from tradeshield.broker.mock import MockExchange
from tradeshield.broker.o21 import (HttpResponse, O21Broker, parse_market_frame, parse_order_event)
from tradeshield.config import Settings
from tradeshield.engine import Engine
from tradeshield.models import OrderRequest, Signal, Tick
from tradeshield.recovery import reconcile
from tradeshield.strategies import IST

TOKENS = {"TCS": 11536, "INFY": 1594, "RELIANCE": 2885}


def env(data=None, ok=True, error=None):
    return {"data": data, "success": ok, "error": error}


class Fake021:
    """Imitates the 021 REST API closely enough to test the adapter. `script` lists behaviours for the next POST /orders."""

    def __init__(self, auto_fill=False):
        self.password, self.token, self.logins = "pw", None, 0
        self.orders, self.trades, self.positions = [], {}, {}
        self.script = []                  # e.g. ["timeout_after", "503", "500_risk", "500_transient", "timeout_before", "429"]
        self.post_calls, self.auto_fill, self.fill_price = 0, auto_fill, 140000
        self.cancel_pending, self._trade_id = {}, 5000
        self.get_failures = []            # statuses returned (once each) for the next GET calls, e.g. [503]

    # ---------------------------------------------------------------- helpers
    def resp(self, status, body=None, headers=None):
        return HttpResponse(status, headers or {}, json.dumps(body).encode() if body is not None else b"")

    def csv(self):
        rows = ["token,exchange,underlying_token,underlying_exchange,min_lot_size,board_lot_quantity,freeze_quantity,"
                "lower_circuit,upper_circuit,expiry,option_type,strike_price,symbol,instrument_type,isin,ticksize"]
        for sym, tok in TOKENS.items():
            rows.append(f"{tok},NSECM,{tok},NSECM,0,1,100000,1,999999,0,,0,{sym},STK,IN000,5")
        rows.append("999,NSEFO,999,NSECM,0,50,1000,1,9999,1,CALL,100,TCS,OPTSTK,,5")
        return "\n".join(rows).encode()

    def find(self, oid):
        return next((o for o in self.orders if o["orderId"] == oid), None)

    def fill(self, oid, qty, price=None):
        o = self.find(oid)
        signed = qty if o["qtyRemaining"] > 0 else -qty
        o["qtyRemaining"] -= signed
        o["qtyTraded"] += signed
        o["status"] = "Executed" if o["qtyRemaining"] == 0 else "Pending"
        self._trade_id += 1
        self.trades.setdefault(oid, []).append({"tradeId": self._trade_id, "tradeTime": int(time.time()), "quantity": signed,
                                                "token": o["token"], "exchange": "NSECM", "product": "INTRADAY",
                                                "price": price or self.fill_price, "symbol": "X"})
        self.positions[o["token"]] = self.positions.get(o["token"], 0) + signed

    def add_order(self, body):
        oid = 1000 + len(self.orders) + 1
        o = {"orderId": oid, "time": int(time.time()), "lastActivity": int(time.time()), "token": body["token"],
             "exchange": "NSECM", "product": body["product"], "qtyRemaining": body["qty"], "qtyTraded": 0, "discQty": 0,
             "price": body["price"], "triggerPrice": 0, "book": "RL", "validity": "Day", "status": "Placed", "reason": "",
             "placedBy": "User", "amo": False}
        self.orders.insert(0, o)
        if self.auto_fill:
            self.fill(oid, abs(body["qty"]))
        return oid

    def _tick_cancels(self, oid=None):
        for k in list(self.cancel_pending):
            if oid in (None, k):
                self.cancel_pending[k] -= 1
                if self.cancel_pending[k] <= 0:
                    del self.cancel_pending[k]
                    o = self.find(k)
                    o["status"], o["qtyRemaining"] = "Cancelled", 0

    # ---------------------------------------------------------------- the API
    async def request(self, method, path, json_body=None, params=None, headers=None):
        if path == "/auth/token":
            if json_body.get("password") != self.password:
                return self.resp(401, env(None, False, "Invalid username or password."))
            self.logins += 1
            self.token = f"TOK{self.logins}"
            return self.resp(200, env({"accessToken": self.token, "tokenType": "Bearer", "expiresIn": 39600}))
        if (headers or {}).get("Authorization") != f"Bearer {self.token}":
            return self.resp(401, env(None, False, "Invalid or expired access token."))
        if self.get_failures and method == "GET":
            return self.resp(self.get_failures.pop(0), env(None, False, "Service temporarily unavailable"))
        if path == "/instruments":
            return HttpResponse(200, {}, gzip.compress(self.csv()))
        if path == "/websocket/ephemeral-key":
            return self.resp(200, env({"token": "EPH"}))
        if method == "POST" and path == "/orders":
            self.post_calls += 1
            step = self.script.pop(0) if self.script else "ok"
            if step == "timeout_before":
                raise asyncio.TimeoutError("lost before reaching 021")
            if step == "503":
                return self.resp(503, env(None, False, "Service temporarily unavailable"))
            if step == "429":
                return self.resp(429, env(None, False, "Too many requests"), {"Retry-After": "0.2"})
            if step == "500_transient":
                return self.resp(500, env(None, False, "Internal Server Error"))
            if step == "500_risk":
                return self.resp(500, env(None, False, "Insufficient funds"))
            oid = self.add_order(json_body)
            if step == "timeout_after":
                raise asyncio.TimeoutError("order accepted but the reply was lost")
            return self.resp(200, env({"type": "SUCCESS", "message": "Order placed successfully", "orderId": str(oid)}))
        if method == "GET" and path == "/orders":
            self._tick_cancels()
            return self.resp(200, [dict(o) for o in self.orders])
        if path.startswith("/orders/"):
            parts = path.split("/")
            oid = int(parts[2])
            o = self.find(oid)
            if o is None:
                return self.resp(404, env(None, False, "order not found"))
            if len(parts) == 4 and parts[3] == "trades":
                return self.resp(200, list(self.trades.get(oid, [])))
            if method == "GET":
                self._tick_cancels(oid)
                return self.resp(200, [dict(self.find(oid))])
            if method == "DELETE":
                if o["status"] in ("Executed", "Cancelled", "Rejected"):
                    return self.resp(200, env(None, False, "order is not open"))
                o["status"] = "SentForCancellation"
                self.cancel_pending[oid] = 2
                return self.resp(200, env({"type": "SUCCESS", "message": "cancel requested"}))
        if path == "/portfolio/positions":
            return self.resp(200, [{"exchange": "NSECM", "token": t, "product": "INTRADAY", "netQuantity": q,
                                    "netPrice": 140000} for t, q in self.positions.items() if q])
        return self.resp(400, env(None, False, f"unhandled {method} {path}"))

    async def close(self):
        pass


def make_settings(**over):
    s = Settings(db_path=":memory:", mock_state_path="", broker_mode="021", o21_username="HACK1", o21_password="pw",
                 o21_map_path="", o21_instruments_cache="", o21_max_rps=1000, order_timeout=1.0, retry_backoff=0.01,
                 max_send_attempts=4, kill_deadline=10.0, o21_poll_interval=0.05)
    for k, v in over.items():
        setattr(s, k, v)
    return s


async def make_world(auto_fill=False, **over):
    srv = Fake021(auto_fill)
    s = make_settings(**over)
    br = O21Broker(s, transport=srv)
    await br.api.login()
    await br.load_instruments()
    eng = Engine(s, broker=br)
    br.subscribe_fills(eng.gateway.on_fill)
    eng.state.paused = False
    for sym in TOKENS:
        eng.prices[sym] = 1400.0
    uid = eng.db.create_user("samira", "pw")
    return srv, br, eng, uid


def sub_for(eng, uid, key):
    return eng.subscribe(uid, key)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_reads_instruments_and_maps_tokens(self):
        srv, br, eng, uid = await make_world()
        self.assertEqual(br.sym2tok, TOKENS)                       # NSE cash only: the NSEFO row for TCS is ignored
        self.assertEqual(br.tick_size["TCS"], 5)

    async def test_wrong_password_gives_clear_error(self):
        srv = Fake021()
        br = O21Broker(make_settings(o21_password="nope"), transport=srv)
        with self.assertRaises(RuntimeError) as cm:
            await br.start()
        self.assertIn("UCC", str(cm.exception))

    async def test_order_payload_matches_the_021_api(self):
        srv, br, eng, uid = await make_world()
        sent = []
        orig = srv.request

        async def spy(method, path, json_body=None, params=None, headers=None):
            if method == "POST" and path == "/orders":
                sent.append(json_body)
            return await orig(method, path, json_body, params, headers)
        srv.request = spy
        await br.place_order(OrderRequest("A1", "TCS", "BUY", 10))
        await br.place_order(OrderRequest("A2", "INFY", "SELL", 4))
        self.assertEqual(sent[0], {"exchange": "NSE", "token": 11536, "qty": 10, "price": 0, "book": "RL",
                                   "product": "INTRADAY", "validity": "Day"})
        self.assertEqual(sent[1]["qty"], -4)                       # sell = negative quantity
        self.assertEqual(sent[1]["token"], 1594)

    async def test_limit_price_goes_out_in_paise_on_the_tick(self):
        srv, br, eng, uid = await make_world()
        await br.place_order(OrderRequest("L1", "TCS", "BUY", 1, "LIMIT", 1405.52))
        self.assertEqual(srv.orders[0]["price"], 140550)           # rounded to the 5-paise tick

    async def test_same_client_id_twice_creates_one_order(self):
        srv, br, eng, uid = await make_world()
        await asyncio.gather(br.place_order(OrderRequest("D1", "TCS", "BUY", 5)), br.place_order(OrderRequest("D1", "TCS", "BUY", 5)))
        self.assertEqual(len(srv.orders), 1)

    async def test_fills_become_fills_exactly_once(self):
        srv, br, eng, uid = await make_world()
        got = []
        br.subscribe_fills(got.append)
        await br.place_order(OrderRequest("F1", "TCS", "SELL", 10))
        oid = srv.orders[0]["orderId"]
        srv.fill(oid, 3, 140000)
        srv.fill(oid, 7, 140100)
        await br.sync_fills()
        await br.sync_fills()                                       # replay: nothing new
        self.assertEqual([(f.side, f.qty, f.price) for f in got], [("SELL", 3, 1400.0), ("SELL", 7, 1401.0)])
        self.assertEqual(len({f.fill_id for f in got}), 2)
        bo = await br.get_order("F1")
        self.assertEqual((bo.status, bo.filled_qty), ("FILLED", 10))
        self.assertAlmostEqual(bo.avg_price, 1400.7, 3)

    async def test_partial_fill_status(self):
        srv, br, eng, uid = await make_world()
        await br.place_order(OrderRequest("P1", "TCS", "BUY", 10))
        srv.fill(srv.orders[0]["orderId"], 4)
        bo = await br.get_order("P1")
        self.assertEqual((bo.status, bo.filled_qty), ("PARTIAL", 4))

    async def test_get_order_unknown_id_is_none(self):
        srv, br, eng, uid = await make_world()
        self.assertIsNone(await br.get_order("never-seen"))

    async def test_token_revoked_relogs_in_transparently(self):
        srv, br, eng, uid = await make_world()
        srv.token = "someone-else-logged-in"                        # our token is now invalid
        await br.place_order(OrderRequest("R1", "TCS", "BUY", 1))
        self.assertEqual(srv.logins, 2)
        self.assertEqual(len(srv.orders), 1)

    async def test_read_calls_retry_through_503(self):
        srv, br, eng, uid = await make_world()
        srv.get_failures = [503, 503]
        self.assertEqual(await br.get_positions(), {})              # succeeded on the 3rd try

    async def test_rate_limit_429_is_survived(self):
        srv, br, eng, uid = await make_world()
        srv.script = ["429"]
        with self.assertRaises(ConnectionError):                    # outcome unknown: caller must verify, not blindly resend
            await br.place_order(OrderRequest("T1", "TCS", "BUY", 1))
        t0 = time.monotonic()
        await br.place_order(OrderRequest("T1", "TCS", "BUY", 1))   # retry with the same id; adapter waited out Retry-After
        self.assertEqual(len(srv.orders), 1)
        self.assertGreater(time.monotonic() - t0, 0.05)

    async def test_async_cancel_waits_until_cancelled(self):
        srv, br, eng, uid = await make_world()
        await br.place_order(OrderRequest("C1", "TCS", "BUY", 5))
        bo = await br.cancel_order("C1")
        self.assertEqual(bo.status, "CANCELLED")

    async def test_cancel_of_filled_order_reports_filled(self):
        srv, br, eng, uid = await make_world(auto_fill=True)
        await br.place_order(OrderRequest("C2", "TCS", "BUY", 5))
        bo = await br.cancel_order("C2")
        self.assertEqual(bo.status, "FILLED")

    async def test_positions_only_for_tracked_symbols(self):
        srv, br, eng, uid = await make_world()
        srv.positions = {11536: 7, 1594: -3, 424242: 99}
        self.assertEqual(await br.get_positions(), {"TCS": 7, "INFY": -3})

    async def test_map_survives_restart(self):
        import os
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "map.json")
        srv = Fake021()
        s = make_settings(o21_map_path=path)
        b1 = O21Broker(s, transport=srv)
        await b1.api.login()
        await b1.load_instruments()
        await b1.place_order(OrderRequest("M1", "TCS", "BUY", 2))
        b2 = O21Broker(s, transport=srv)                            # "restart"
        await b2.api.login()
        await b2.load_instruments()
        bo = await b2.get_order("M1")
        self.assertIsNotNone(bo)
        await b2.place_order(OrderRequest("M1", "TCS", "BUY", 2))   # resend after restart: still one order
        self.assertEqual(len(srv.orders), 1)


class GatewayWith021Tests(unittest.IsolatedAsyncioTestCase):
    """The platform's promises, proven against the failure modes of the real sandbox."""

    async def test_lost_ack_does_not_duplicate(self):
        srv, br, eng, uid = await make_world()
        sid = sub_for(eng, uid, "mean_reversion")
        srv.script = ["timeout_after"]                              # 021 took the order, the reply never came back
        row = await eng.gateway.submit(sid, Signal("TCS", "BUY", 5, "t"))
        self.assertEqual(len(srv.orders), 1)
        self.assertEqual(srv.post_calls, 1)                         # adopted, NOT resent
        self.assertIn(row["status"], ("OPEN", "SENT"))
        self.assertTrue(any(e["kind"] == "ORDER_ADOPTED" for e in eng.db.list_events(50)))

    async def test_request_lost_before_reaching_021_is_resent_once(self):
        srv, br, eng, uid = await make_world()
        sid = sub_for(eng, uid, "mean_reversion")
        srv.script = ["timeout_before"]
        await eng.gateway.submit(sid, Signal("TCS", "BUY", 5, "t"))
        self.assertEqual(len(srv.orders), 1)
        self.assertEqual(srv.post_calls, 2)

    async def test_503_then_success_gives_one_order(self):
        srv, br, eng, uid = await make_world()
        sid = sub_for(eng, uid, "mean_reversion")
        srv.script = ["503", "503"]
        await eng.gateway.submit(sid, Signal("TCS", "BUY", 5, "t"))
        self.assertEqual(len(srv.orders), 1)

    async def test_random_500_is_treated_as_unknown_not_as_a_rejection(self):
        srv, br, eng, uid = await make_world()
        sid = sub_for(eng, uid, "mean_reversion")
        srv.script = ["500_transient"]
        row = await eng.gateway.submit(sid, Signal("TCS", "BUY", 5, "t"))
        self.assertEqual(len(srv.orders), 1)
        self.assertNotEqual(row["status"], "REJECTED")

    async def test_500_risk_rejection_is_final_and_recorded(self):
        srv, br, eng, uid = await make_world()
        sid = sub_for(eng, uid, "mean_reversion")
        srv.script = ["500_risk"]
        row = await eng.gateway.submit(sid, Signal("TCS", "BUY", 5, "t"))
        self.assertEqual(row["status"], "REJECTED")
        self.assertIn("Insufficient funds", row["reason"])
        self.assertEqual(srv.post_calls, 1)                         # never retried
        self.assertEqual(len(srv.orders), 0)

    async def test_partial_then_full_fill_updates_strategy_position_and_pnl(self):
        srv, br, eng, uid = await make_world()
        sid = sub_for(eng, uid, "mean_reversion")
        await eng.gateway.submit(sid, Signal("TCS", "BUY", 10, "open"))
        oid = srv.orders[0]["orderId"]
        srv.fill(oid, 4, 140000)
        await br.sync_fills()
        self.assertEqual(eng.db.get_position(sid, "TCS")["qty"], 4)
        self.assertEqual(eng.db.get_order(eng.db.open_orders(sid)[0]["client_order_id"])["status"], "PARTIAL")
        srv.fill(oid, 6, 140000)
        await br.sync_fills()
        self.assertEqual(eng.db.get_position(sid, "TCS")["qty"], 10)
        await eng.gateway.submit(sid, Signal("TCS", "SELL", 10, "close"))
        srv.fill(srv.orders[0]["orderId"], 10, 141000)
        await br.sync_fills()
        pnl = eng.ledger.pnl(sid)
        self.assertEqual(eng.db.get_position(sid, "TCS")["qty"], 0)
        self.assertAlmostEqual(pnl["realized_net"] + pnl["charges"], 10 * 10.0, 2)   # gross 10 shares x Rs10, charges separate
        self.assertGreater(pnl["charges"], 0)

    async def test_two_strategies_opposite_sides_same_stock(self):
        srv, br, eng, uid = await make_world(auto_fill=True)
        a, b = sub_for(eng, uid, "mean_reversion"), sub_for(eng, uid, "trend_breakout")
        await eng.gateway.submit(a, Signal("TCS", "BUY", 10, "long"))
        await eng.gateway.submit(b, Signal("TCS", "SELL", 10, "short"))
        await br.sync_fills()
        self.assertEqual(eng.db.get_position(a, "TCS")["qty"], 10)
        self.assertEqual(eng.db.get_position(b, "TCS")["qty"], -10)
        self.assertEqual(await br.get_positions(), {})              # nets to zero at 021

    async def test_kill_switch_cancels_and_flattens_against_021(self):
        srv, br, eng, uid = await make_world(auto_fill=False)
        a, b = sub_for(eng, uid, "mean_reversion"), sub_for(eng, uid, "tick_momentum")
        await eng.gateway.submit(a, Signal("TCS", "BUY", 10, "x"))
        srv.fill(srv.orders[0]["orderId"], 10)                       # a is long 10
        await br.sync_fills()
        await eng.gateway.submit(b, Signal("INFY", "BUY", 5, "y"))   # b has an open, unfilled order

        async def exchange():                                        # market orders for the close-outs fill quickly
            while True:
                await asyncio.sleep(0.05)
                for o in srv.orders:
                    if o["status"] == "Placed" and o["qtyRemaining"] < 0:
                        srv.fill(o["orderId"], abs(o["qtyRemaining"]))
        task = asyncio.create_task(exchange())
        res = await eng.kill_switch()
        task.cancel()
        self.assertTrue(res["flat"], res)
        self.assertLessEqual(res["elapsed_seconds"], 10)
        self.assertEqual(srv.positions.get(TOKENS["TCS"], 0), 0)
        self.assertEqual({o["status"] for o in srv.orders if o["token"] == TOKENS["INFY"]}, {"Cancelled"})

    async def test_recovery_after_crash_picks_up_missed_fills_and_cancels_orphans(self):
        srv, br, eng, uid = await make_world()
        sid = sub_for(eng, uid, "mean_reversion")
        await eng.gateway.submit(sid, Signal("TCS", "BUY", 10, "x"))
        srv.fill(srv.orders[0]["orderId"], 10)                       # filled while the platform was down (no sync ran)
        srv.add_order({"token": TOKENS["INFY"], "qty": 3, "price": 0, "product": "INTRADAY"})   # an order we know nothing about
        self.assertEqual(eng.db.get_position(sid, "TCS")["qty"], 0)
        rep = await reconcile(eng, "test")
        self.assertEqual(eng.db.get_position(sid, "TCS")["qty"], 10)
        self.assertEqual(rep["fills_recovered"], 1)
        self.assertEqual(rep["orphans_cancelled"], 1)
        self.assertEqual(srv.orders[0]["status"], "Cancelled")       # newest order = the orphan
        self.assertEqual(rep["position_fixes"], [])

    async def test_recovery_fixes_position_mismatch(self):
        srv, br, eng, uid = await make_world()
        srv.positions = {TOKENS["RELIANCE"]: 12}                     # 021 says +12, the platform knows nothing
        rep = await reconcile(eng, "test")
        self.assertEqual(rep["position_fixes"][0]["corrected_to"], 12)
        self.assertEqual(eng.db.net_positions().get("RELIANCE"), 12)

    async def test_recovery_after_crash_mid_send_does_not_duplicate(self):
        srv, br, eng, uid = await make_world()
        sid = sub_for(eng, uid, "mean_reversion")
        # crash after 021 accepted the order but before its orderId was stored: DB says SENT, map has no id
        eng.db.insert_order({"client_order_id": "CRASH1", "sub_id": sid, "symbol": "TCS", "side": "BUY", "qty": 5,
                             "order_type": "MARKET", "limit_price": None, "status": "SENT", "reason": "", "ts": time.time(),
                             "is_close": False})
        br.map.put("CRASH1", {"order_id": None, "token": TOKENS["TCS"], "symbol": "TCS", "side": "BUY", "qty": 5,
                              "price": 0, "sent_at": time.time()})
        srv.add_order({"token": TOKENS["TCS"], "qty": 5, "price": 0, "product": "INTRADAY"})
        srv.fill(srv.orders[0]["orderId"], 5)
        rep = await reconcile(eng, "test")
        self.assertEqual(len(srv.orders), 1)
        self.assertEqual(eng.db.get_position(sid, "TCS")["qty"], 5)
        self.assertEqual(rep["orphans_cancelled"], 0)


def _tick_frame(token, ltp, exch=1):
    return struct.pack(">HHII", 1, exch, token, ltp)


def _full_nse(token, ltp, last_qty, volume, open_):
    p = bytearray(220)
    struct.pack_into(">HHIII", p, 0, 3, 1, token, ltp, last_qty)
    struct.pack_into(">Q", p, 20, volume)
    struct.pack_into(">I", p, 48, open_)
    return bytes(p)


class ParserTests(unittest.IsolatedAsyncioTestCase):
    def test_ltp_packets_and_heartbeat_in_one_frame(self):
        frame = _tick_frame(2885, 140123) + b"\x00\x0a" + _tick_frame(1594, 150050)
        got = parse_market_frame(frame)
        self.assertEqual([(g["token"], g["ltp"]) for g in got], [(2885, 140123), (1594, 150050)])

    def test_ltpo_snapshot_extra_bytes_are_skipped(self):
        frame = _tick_frame(2885, 140000) + b"o" + struct.pack(">I", 139000) + _tick_frame(1594, 150000)
        self.assertEqual([g["token"] for g in parse_market_frame(frame)], [2885, 1594])

    def test_full_packet_gives_ltp_open_volume(self):
        frame = _full_nse(2885, 141400, 25, 9000, 140000) + _tick_frame(1594, 150000)
        got = parse_market_frame(frame)
        self.assertEqual((got[0]["ltp"], got[0]["open"], got[0]["volume"], got[0]["last_qty"]), (141400, 140000, 9000, 25))
        self.assertEqual(got[1]["token"], 1594)                      # parsing stayed aligned after the 220-byte packet

    def test_truncated_frame_does_not_crash(self):
        self.assertEqual(parse_market_frame(_full_nse(1, 2, 3, 4, 5)[:100]), [])
        self.assertEqual(parse_market_frame(b"\x00"), [])
        self.assertEqual(parse_market_frame(b"\x00\x63garbage"), [])

    def test_order_event_nse_and_bse(self):
        nse = bytearray(46)
        struct.pack_into(">HH", nse, 0, 4, 1)
        struct.pack_into(">I", nse, 34, 1042)
        self.assertEqual(parse_order_event(bytes(nse)), (1, 1042))
        bse = bytearray(32)
        struct.pack_into(">HH", bse, 0, 8, 7)
        struct.pack_into(">I", bse, 20, 77)
        self.assertEqual(parse_order_event(bytes(bse)), (7, 77))
        self.assertIsNone(parse_order_event(b"\x00\x0a"))

    async def test_market_frame_becomes_ticks_with_open_and_volume(self):
        srv, br, eng, uid = await make_world()
        ticks = []
        br.subscribe_ticks(ticks.append)
        br.on_market_frame(_full_nse(2885, 141400, 10, 1000, 140000))
        br.on_market_frame(_full_nse(2885, 141500, 10, 1060, 140000))
        br.on_market_frame(_tick_frame(999999, 100))                 # unknown token: ignored
        self.assertEqual([(t.symbol, t.price, t.size, t.open) for t in ticks],
                         [("RELIANCE", 1414.0, 10, 1400.0), ("RELIANCE", 1415.0, 60, 1400.0)])


def ist_epoch(h, m, s=0, day=9):
    return dt.datetime(2026, 10, day, h, m, s, tzinfo=IST).timestamp()


class StrategyHarness(unittest.IsolatedAsyncioTestCase):
    """Engine on the mock exchange with a controllable clock, to test the organisers' strategies."""

    async def asyncSetUp(self):
        self.now = ist_epoch(9, 14, 50)
        s = Settings(db_path=":memory:", mock_state_path="", order_timeout=0.5, retry_backoff=0.02)
        self.ex = MockExchange(["TCS", "INFY", "RELIANCE"], seed=3, tick_interval=0.01)
        self.ex.chaos.partial_fills = False
        self.e = Engine(s, broker=self.ex, clock=lambda: self.now)
        self.ex.subscribe_fills(self.e.gateway.on_fill)
        self.e.state.paused = False
        self.uid = self.e.db.create_user("samira", "pw")

    async def settle(self):
        for _ in range(6):
            await asyncio.sleep(0.01)
            await self.ex.process_fills_once()
        await asyncio.gather(*list(self.e._tasks), return_exceptions=True)
        await self.ex.process_fills_once()

    async def tick(self, sym, price, open_=None):
        self.ex.prices[sym] = price
        self.e.on_tick(Tick(sym, price, self.now, 1, open_))
        await self.settle()

    # ------------------------------------------------------------ strategy 1
    async def test_timed_entry_at_0915_and_exit_at_1515(self):
        sid = self.e.subscribe(self.uid, "timed_entry_exit")
        self.e.prices["RELIANCE"] = self.ex.prices["RELIANCE"]
        self.e._dispatch_time()                                      # 09:14:50 - too early
        await self.settle()
        self.assertEqual(self.e.db.count_orders(), 0)
        self.now = ist_epoch(9, 15, 1)
        self.e._dispatch_time()
        await self.settle()
        self.e._dispatch_time()                                      # called again: must not enter twice
        await self.settle()
        self.assertEqual(self.e.db.get_position(sid, "RELIANCE")["qty"], 10)
        self.assertEqual(self.e.db.count_orders(), 1)
        self.now = ist_epoch(12, 0)
        self.e._dispatch_time()
        await self.settle()
        self.assertEqual(self.e.db.get_position(sid, "RELIANCE")["qty"], 10)    # holds all day
        self.now = ist_epoch(15, 15, 2)
        self.e._dispatch_time()
        await self.settle()
        self.assertEqual(self.e.db.get_position(sid, "RELIANCE")["qty"], 0)
        self.assertEqual(self.e.db.count_orders(), 2)

    async def test_late_start_does_not_enter_after_the_window(self):
        sid = self.e.subscribe(self.uid, "timed_entry_exit")
        self.now = ist_epoch(11, 0)
        self.e._dispatch_time()
        await self.settle()
        self.assertEqual(self.e.db.count_orders(), 0)

    async def test_exit_cancels_open_entry_order_first(self):
        sid = self.e.subscribe(self.uid, "timed_entry_exit")
        self.ex.chaos.fill_probability = 0.0                         # entry stays open (never fills)
        self.now = ist_epoch(9, 15, 1)
        self.e._dispatch_time()
        await self.settle()
        self.assertEqual(len(self.e.db.open_orders(sid)), 1)
        self.now = ist_epoch(15, 15, 1)
        self.e._dispatch_time()
        await self.settle()
        self.assertEqual(self.e.db.open_orders(sid), [])
        self.assertEqual({o["status"] for o in self.e.db.list_orders(self.uid)}, {"CANCELLED"})

    # ------------------------------------------------------------ strategy 2
    async def test_breakout_up_then_target(self):
        sid = self.e.subscribe(self.uid, "open_breakout")
        await self.tick("INFY", 1400.0, open_=1400.0)
        self.assertEqual(self.e.db.count_orders(), 0)
        await self.tick("INFY", 1413.0, open_=1400.0)                # +0.93%: not yet
        self.assertEqual(self.e.db.count_orders(), 0)
        await self.tick("INFY", 1414.0, open_=1400.0)                # +1.00%: BUY
        self.assertEqual(self.e.db.get_position(sid, "INFY")["qty"], 10)
        entry = self.e.db.get_position(sid, "INFY")["avg_price"]
        self.now += 10
        await self.tick("INFY", entry * 1.049, open_=1400.0)         # just under +5%: hold
        self.assertEqual(self.e.db.get_position(sid, "INFY")["qty"], 10)
        await self.tick("INFY", entry * 1.05, open_=1400.0)          # target
        self.assertEqual(self.e.db.get_position(sid, "INFY")["qty"], 0)
        orders = self.e.db.list_orders(self.uid)
        self.assertEqual(sorted((o["side"], o["qty"]) for o in orders), [("BUY", 10), ("SELL", 10)])
        await self.tick("INFY", 1500.0, open_=1400.0)                # one entry per day: no re-entry
        self.assertEqual(self.e.db.count_orders(), 2)

    async def test_breakdown_short_then_stop_loss(self):
        sid = self.e.subscribe(self.uid, "open_breakout")
        await self.tick("INFY", 1400.0, open_=1400.0)
        await self.tick("INFY", 1386.0, open_=1400.0)                # -1%: SELL
        self.assertEqual(self.e.db.get_position(sid, "INFY")["qty"], -10)
        entry = self.e.db.get_position(sid, "INFY")["avg_price"]
        self.now += 10
        await self.tick("INFY", entry * 1.05, open_=1400.0)          # short stop-loss = entry +5%
        self.assertEqual(self.e.db.get_position(sid, "INFY")["qty"], 0)

    async def test_long_stop_loss(self):
        sid = self.e.subscribe(self.uid, "open_breakout")
        await self.tick("INFY", 1400.0, open_=1400.0)
        await self.tick("INFY", 1415.0, open_=1400.0)
        entry = self.e.db.get_position(sid, "INFY")["avg_price"]
        self.now += 10
        await self.tick("INFY", entry * 0.95, open_=1400.0)
        self.assertEqual(self.e.db.get_position(sid, "INFY")["qty"], 0)

    async def test_open_comes_from_first_price_when_feed_has_none(self):
        sid = self.e.subscribe(self.uid, "open_breakout")
        await self.tick("INFY", 1000.0)                              # first price of the day => "open"
        await self.tick("INFY", 1010.0)                              # +1% => BUY
        self.assertEqual(self.e.db.get_position(sid, "INFY")["qty"], 10)

    async def test_breakout_still_obeys_platform_position_limit(self):
        sid = self.e.subscribe(self.uid, "open_breakout")
        self.e.update_limits(sid, 5000, 5, 10)                       # max position 5 but the strategy wants 10
        await self.tick("INFY", 1400.0, open_=1400.0)
        await self.tick("INFY", 1415.0, open_=1400.0)
        self.assertEqual(self.e.db.get_position(sid, "INFY")["qty"], 0)
        self.assertEqual(self.e.db.list_orders(self.uid)[0]["status"], "BLOCKED")

    async def test_breakout_squares_off_at_1515(self):
        sid = self.e.subscribe(self.uid, "open_breakout")
        await self.tick("INFY", 1400.0, open_=1400.0)
        await self.tick("INFY", 1415.0, open_=1400.0)
        self.assertEqual(self.e.db.get_position(sid, "INFY")["qty"], 10)
        self.now = ist_epoch(15, 15, 1)
        self.e._dispatch_time()
        await self.settle()
        self.assertEqual(self.e.db.get_position(sid, "INFY")["qty"], 0)


if __name__ == "__main__":
    unittest.main()
