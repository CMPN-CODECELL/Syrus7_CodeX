"""SQLite persistence (stdlib only).

All SQL lives in this file, so moving to PostgreSQL later means changing only this module.
The database is the platform's source of truth for orders, fills and per-strategy positions.
"""
import hashlib
import json
import secrets
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
  pw_hash TEXT NOT NULL, salt TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tokens(
  token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS subscriptions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, strategy_key TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ACTIVE', halt_reason TEXT DEFAULT '',
  max_daily_loss REAL NOT NULL, max_position INTEGER NOT NULL, max_orders_per_min INTEGER NOT NULL,
  shield_tier INTEGER NOT NULL DEFAULT 0, params TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL,
  UNIQUE(user_id, strategy_key));
CREATE TABLE IF NOT EXISTS orders(
  client_order_id TEXT PRIMARY KEY, sub_id INTEGER NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
  qty INTEGER NOT NULL, order_type TEXT NOT NULL, limit_price REAL, status TEXT NOT NULL,
  filled_qty INTEGER NOT NULL DEFAULT 0, avg_price REAL NOT NULL DEFAULT 0, reason TEXT DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 0, is_close INTEGER NOT NULL DEFAULT 0,
  created_ts REAL NOT NULL, updated_ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS fills(
  fill_id TEXT PRIMARY KEY, client_order_id TEXT NOT NULL, sub_id INTEGER NOT NULL, symbol TEXT NOT NULL,
  side TEXT NOT NULL, qty INTEGER NOT NULL, price REAL NOT NULL, charges REAL NOT NULL,
  realized_pnl REAL NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS positions(
  sub_id INTEGER NOT NULL, symbol TEXT NOT NULL, qty INTEGER NOT NULL, avg_price REAL NOT NULL,
  PRIMARY KEY(sub_id, symbol));
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, category TEXT NOT NULL, kind TEXT NOT NULL,
  sub_id INTEGER, message TEXT NOT NULL, data TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS intervals(spec TEXT PRIMARY KEY);
CREATE INDEX IF NOT EXISTS idx_orders_sub ON orders(sub_id, status);
CREATE INDEX IF NOT EXISTS idx_fills_sub ON fills(sub_id, ts);
"""

SUB_FIELDS = {"status", "halt_reason", "max_daily_loss", "max_position",
              "max_orders_per_min", "shield_tier", "params"}
ORDER_FIELDS = {"status", "filled_qty", "avg_price", "reason", "attempts", "updated_ts"}


class Database:
    def __init__(self, path=":memory:"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            if path != ":memory:":
                self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)

    # ---------------------------------------------------------------- helpers
    def _q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def _one(self, sql, args=()):
        rows = self._q(sql, args)
        return rows[0] if rows else None

    def _x(self, sql, args=()):
        with self.lock, self.conn:
            return self.conn.execute(sql, args)

    @staticmethod
    def _sub(row):
        if row:
            row["params"] = json.loads(row.get("params") or "{}")
        return row

    # ------------------------------------------------------------------ users
    @staticmethod
    def _hash(password, salt):
        return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()

    def create_user(self, username, password):
        salt = secrets.token_hex(8)
        try:
            cur = self._x("INSERT INTO users(username,pw_hash,salt,created) VALUES(?,?,?,?)",
                          (username, self._hash(password, salt), salt, time.time()))
        except sqlite3.IntegrityError:
            raise ValueError("Username already taken")
        return cur.lastrowid

    def verify_user(self, username, password):
        u = self._one("SELECT * FROM users WHERE username=?", (username,))
        if u and secrets.compare_digest(u["pw_hash"], self._hash(password, u["salt"])):
            return u["id"]
        return None

    def create_token(self, user_id):
        t = secrets.token_hex(24)
        self._x("INSERT INTO tokens(token,user_id,created) VALUES(?,?,?)", (t, user_id, time.time()))
        return t

    def user_for_token(self, token):
        return self._one("SELECT u.id, u.username FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?",
                         (token,))

    # ---------------------------------------------------------- subscriptions
    def create_sub(self, user_id, strategy_key, limits, params=None):
        existing = self._one("SELECT * FROM subscriptions WHERE user_id=? AND strategy_key=?",
                             (user_id, strategy_key))
        if existing:
            self._x("UPDATE subscriptions SET status='ACTIVE', halt_reason='' WHERE id=?", (existing["id"],))
            return existing["id"]
        cur = self._x(
            "INSERT INTO subscriptions(user_id,strategy_key,max_daily_loss,max_position,max_orders_per_min,params,created)"
            " VALUES(?,?,?,?,?,?,?)",
            (user_id, strategy_key, limits["max_daily_loss"], limits["max_position"],
             limits["max_orders_per_min"], json.dumps(params or {}), time.time()))
        return cur.lastrowid

    def get_sub(self, sub_id):
        return self._sub(self._one("SELECT * FROM subscriptions WHERE id=?", (sub_id,)))

    def user_subs(self, user_id):
        return [self._sub(r) for r in self._q("SELECT * FROM subscriptions WHERE user_id=? ORDER BY id", (user_id,))]

    def all_subs(self):
        return [self._sub(r) for r in self._q("SELECT * FROM subscriptions ORDER BY id")]

    def active_subs(self):
        return [self._sub(r) for r in self._q("SELECT * FROM subscriptions WHERE status='ACTIVE'")]

    def update_sub(self, sub_id, **fields):
        fields = {k: v for k, v in fields.items() if k in SUB_FIELDS}
        if "params" in fields and not isinstance(fields["params"], str):
            fields["params"] = json.dumps(fields["params"])
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE subscriptions SET {cols} WHERE id=?", (*fields.values(), sub_id))

    # ----------------------------------------------------------------- orders
    def insert_order(self, o):
        self._x("INSERT INTO orders(client_order_id,sub_id,symbol,side,qty,order_type,limit_price,status,filled_qty,"
                "avg_price,reason,attempts,is_close,created_ts,updated_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (o["client_order_id"], o["sub_id"], o["symbol"], o["side"], o["qty"], o["order_type"],
                 o.get("limit_price"), o["status"], 0, 0.0, o.get("reason", ""), 0,
                 int(o.get("is_close", 0)), o["ts"], o["ts"]))

    def get_order(self, coid):
        return self._one("SELECT * FROM orders WHERE client_order_id=?", (coid,))

    def update_order(self, coid, **fields):
        fields = {k: v for k, v in fields.items() if k in ORDER_FIELDS}
        fields.setdefault("updated_ts", time.time())
        cols = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE orders SET {cols} WHERE client_order_id=?", (*fields.values(), coid))

    def open_orders(self, sub_id=None):
        sql = "SELECT * FROM orders WHERE status IN ('NEW','SENT','UNKNOWN','OPEN','PARTIAL')"
        args = ()
        if sub_id is not None:
            sql, args = sql + " AND sub_id=?", (sub_id,)
        return self._q(sql + " ORDER BY created_ts", args)

    def pending_qty(self, sub_id, symbol, side):
        """Shares still unfilled on live orders - they count towards position limits (worst case)."""
        r = self._one("SELECT COALESCE(SUM(qty - filled_qty),0) AS q FROM orders WHERE sub_id=? AND symbol=? AND side=? "
                      "AND status IN ('NEW','SENT','UNKNOWN','OPEN','PARTIAL')", (sub_id, symbol, side))
        return r["q"]

    def list_orders(self, user_id=None, limit=100):
        if user_id is None:
            return self._q("SELECT * FROM orders ORDER BY created_ts DESC LIMIT ?", (limit,))
        return self._q("SELECT o.* FROM orders o JOIN subscriptions s ON s.id=o.sub_id "
                       "WHERE s.user_id=? ORDER BY o.created_ts DESC LIMIT ?", (user_id, limit))

    def count_orders(self):
        return self._one("SELECT COUNT(*) AS n FROM orders")["n"]

    # ------------------------------------------------------------ fills / pos
    def fill_exists(self, fill_id):
        return self._one("SELECT 1 AS x FROM fills WHERE fill_id=?", (fill_id,)) is not None

    def record_fill(self, fill, position, order_update):
        """Insert fill + upsert position + update order in ONE transaction (all or nothing)."""
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO fills(fill_id,client_order_id,sub_id,symbol,side,qty,price,charges,realized_pnl,ts)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (fill["fill_id"], fill["client_order_id"], fill["sub_id"], fill["symbol"], fill["side"],
                 fill["qty"], fill["price"], fill["charges"], fill["realized_pnl"], fill["ts"]))
            self.conn.execute(
                "INSERT INTO positions(sub_id,symbol,qty,avg_price) VALUES(?,?,?,?) "
                "ON CONFLICT(sub_id,symbol) DO UPDATE SET qty=excluded.qty, avg_price=excluded.avg_price",
                (position["sub_id"], position["symbol"], position["qty"], position["avg_price"]))
            self.conn.execute(
                "UPDATE orders SET filled_qty=?, avg_price=?, status=?, updated_ts=? WHERE client_order_id=?",
                (order_update["filled_qty"], order_update["avg_price"], order_update["status"],
                 time.time(), fill["client_order_id"]))

    def set_position(self, sub_id, symbol, qty, avg_price):
        self._x("INSERT INTO positions(sub_id,symbol,qty,avg_price) VALUES(?,?,?,?) "
                "ON CONFLICT(sub_id,symbol) DO UPDATE SET qty=excluded.qty, avg_price=excluded.avg_price",
                (sub_id, symbol, qty, avg_price))

    def get_position(self, sub_id, symbol):
        return self._one("SELECT * FROM positions WHERE sub_id=? AND symbol=?", (sub_id, symbol)) or \
            {"sub_id": sub_id, "symbol": symbol, "qty": 0, "avg_price": 0.0}

    def sub_positions(self, sub_id):
        return self._q("SELECT * FROM positions WHERE sub_id=? AND qty!=0", (sub_id,))

    def net_positions(self):
        """Account-level net quantity per symbol = sum over every strategy sub-ledger."""
        rows = self._q("SELECT symbol, SUM(qty) AS qty FROM positions GROUP BY symbol")
        return {r["symbol"]: r["qty"] for r in rows}

    def realized_net(self, sub_id, since_ts=None):
        sql = "SELECT COALESCE(SUM(realized_pnl - charges),0) AS v, COALESCE(SUM(charges),0) AS c FROM fills WHERE sub_id=?"
        args = [sub_id]
        if since_ts is not None:
            sql, args = sql + " AND ts>=?", [sub_id, since_ts]
        return self._one(sql, args)

    def list_fills(self, user_id=None, limit=100):
        if user_id is None:
            return self._q("SELECT * FROM fills ORDER BY ts DESC LIMIT ?", (limit,))
        return self._q("SELECT f.* FROM fills f JOIN subscriptions s ON s.id=f.sub_id "
                       "WHERE s.user_id=? ORDER BY f.ts DESC LIMIT ?", (user_id, limit))

    # -------------------------------------------------------------- intervals
    def add_interval(self, spec):
        self._x("INSERT OR IGNORE INTO intervals(spec) VALUES(?)", (spec,))

    def list_intervals(self):
        return [r["spec"] for r in self._q("SELECT spec FROM intervals ORDER BY rowid")]

    # ----------------------------------------------------------------- events
    def add_event(self, ts, category, kind, sub_id, message, data):
        cur = self._x("INSERT INTO events(ts,category,kind,sub_id,message,data) VALUES(?,?,?,?,?,?)",
                      (ts, category, kind, sub_id, message, json.dumps(data or {})))
        return cur.lastrowid

    def list_events(self, limit=100, category=None):
        if category:
            rows = self._q("SELECT * FROM events WHERE category=? ORDER BY id DESC LIMIT ?", (category, limit))
        else:
            rows = self._q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["data"] = json.loads(r["data"] or "{}")
        return rows
