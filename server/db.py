"""SQLite 存储层：用户/订单/用量。低并发，单连接 + 锁足够。"""
import sqlite3
import threading
import time
import secrets

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT UNIQUE NOT NULL,
    token TEXT UNIQUE NOT NULL,
    credit_seconds INTEGER NOT NULL DEFAULT 0,
    sub_expire_at INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    out_trade_no TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    product_code TEXT NOT NULL,
    name TEXT NOT NULL,
    money TEXT NOT NULL,
    pay_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending/paid/closed
    trade_no TEXT,
    created_at INTEGER NOT NULL,
    paid_at INTEGER
);
CREATE TABLE IF NOT EXISTS usage_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    stream TEXT NOT NULL,
    seconds INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
"""


class DB:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def _q(self, sql: str, args=(), one=False):
        with self._lock:
            cur = self._conn.execute(sql, args)
            rows = cur.fetchall()
        return (rows[0] if rows else None) if one else rows

    def _x(self, sql: str, args=()):
        with self._lock:
            self._conn.execute(sql, args)
            self._conn.commit()

    # ---------- 用户 ----------
    def get_or_create_user(self, device_id: str, trial_seconds: int = 0) -> dict:
        row = self._q("SELECT * FROM users WHERE device_id=?", (device_id,), one=True)
        if row:
            return dict(row)
        token = secrets.token_hex(24)
        now = int(time.time())
        self._x(
            "INSERT INTO users(device_id, token, credit_seconds, sub_expire_at, created_at)"
            " VALUES(?,?,?,?,?)",
            (device_id, token, trial_seconds, 0, now),
        )
        return dict(self._q("SELECT * FROM users WHERE device_id=?", (device_id,), one=True))

    def user_by_token(self, token: str):
        row = self._q("SELECT * FROM users WHERE token=?", (token,), one=True)
        return dict(row) if row else None

    def add_credits(self, user_id: int, seconds: int):
        self._x(
            "UPDATE users SET credit_seconds = MAX(0, credit_seconds + ?) WHERE id=?",
            (seconds, user_id),
        )

    def extend_sub(self, user_id: int, days: int):
        now = int(time.time())
        user = self._q("SELECT sub_expire_at FROM users WHERE id=?", (user_id,), one=True)
        base = max(now, user["sub_expire_at"] if user else 0)
        self._x(
            "UPDATE users SET sub_expire_at=? WHERE id=?",
            (base + days * 86400, user_id),
        )

    def sub_active(self, user: dict) -> bool:
        return user.get("sub_expire_at", 0) > int(time.time())

    # ---------- 订单 ----------
    def create_order(self, out_trade_no, user_id, product_code, name, money, pay_type):
        self._x(
            "INSERT INTO orders(out_trade_no,user_id,product_code,name,money,pay_type,status,created_at)"
            " VALUES(?,?,?,?,?,?,'pending',?)",
            (out_trade_no, user_id, product_code, name, money, pay_type, int(time.time())),
        )

    def get_order(self, out_trade_no):
        row = self._q("SELECT * FROM orders WHERE out_trade_no=?", (out_trade_no,), one=True)
        return dict(row) if row else None

    def mark_paid(self, out_trade_no, trade_no=""):
        """仅当本调用真正把 pending->paid 时返回订单，否则 None（防重复发货）。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE orders SET status='paid', trade_no=?, paid_at=? "
                "WHERE out_trade_no=? AND status='pending'",
                (trade_no, int(time.time()), out_trade_no),
            )
            self._conn.commit()
            changed = cur.rowcount > 0
        return self.get_order(out_trade_no) if changed else None

    # ---------- 用量 ----------
    def log_usage(self, user_id: int, stream: str, seconds: int):
        self._x(
            "INSERT INTO usage_logs(user_id,stream,seconds,created_at) VALUES(?,?,?,?)",
            (user_id, stream, seconds, int(time.time())),
        )
