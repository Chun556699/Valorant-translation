"""SQLite 存储层：账号/订单/用量/推广。低并发，单连接 + 锁足够。"""
import sqlite3
import threading
import time
import secrets

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE,             -- NULL=游客账号
    password_hash TEXT,
    device_id TEXT,
    token TEXT UNIQUE NOT NULL,
    money_cents INTEGER NOT NULL DEFAULT 0,      -- 余额（分）
    credit_seconds INTEGER NOT NULL DEFAULT 0,   -- 时长卡/点数（秒）
    trial_seconds INTEGER NOT NULL DEFAULT 0,    -- 免费额度剩余（秒）
    trial_expire_at INTEGER NOT NULL DEFAULT 0,
    sub_expire_at INTEGER NOT NULL DEFAULT 0,
    ref_code TEXT UNIQUE,             -- 推广码
    referred_by INTEGER,              -- 邀请人 user_id
    ref_earned_fen INTEGER NOT NULL DEFAULT 0,   -- 累计返利（分）
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
    pool TEXT NOT NULL,               -- trial/card/money/free
    fen INTEGER NOT NULL DEFAULT 0,   -- 走余额时扣的分
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

    # ---------- 账号 ----------
    def create_account(self, username: str, password_hash: str,
                       trial_seconds: int, trial_days: int,
                       ref_code: str | None = None,
                       referred_by: int | None = None,
                       device_id: str | None = None) -> dict | None:
        token = secrets.token_hex(24)
        rc = ref_code or secrets.token_hex(4)
        now = int(time.time())
        trial_exp = now + trial_days * 86400 if trial_seconds > 0 else 0
        try:
            self._x(
                "INSERT INTO users(username,password_hash,device_id,token,"
                "trial_seconds,trial_expire_at,ref_code,referred_by,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (username, password_hash, device_id, token,
                 trial_seconds, trial_exp, rc, referred_by, now),
            )
        except sqlite3.IntegrityError:
            return None
        return dict(self._q("SELECT * FROM users WHERE username=?",
                            (username,), one=True))

    def create_guest(self, device_id: str, trial_seconds: int,
                     trial_days: int) -> dict:
        row = self._q("SELECT * FROM users WHERE device_id=? AND username IS NULL",
                      (device_id,), one=True)
        if row:
            return dict(row)
        token = secrets.token_hex(24)
        now = int(time.time())
        trial_exp = now + trial_days * 86400 if trial_seconds > 0 else 0
        self._x(
            "INSERT INTO users(device_id,token,trial_seconds,trial_expire_at,"
            "ref_code,created_at) VALUES(?,?,?,?,?,?)",
            (device_id, token, trial_seconds, trial_exp,
             secrets.token_hex(4), now),
        )
        return dict(self._q("SELECT * FROM users WHERE token=?", (token,), one=True))

    def user_by_username(self, username: str):
        row = self._q("SELECT * FROM users WHERE username=?", (username,), one=True)
        return dict(row) if row else None

    def user_by_token(self, token: str):
        row = self._q("SELECT * FROM users WHERE token=?", (token,), one=True)
        return dict(row) if row else None

    def user_by_ref_code(self, ref_code: str):
        row = self._q("SELECT * FROM users WHERE ref_code=?", (ref_code,), one=True)
        return dict(row) if row else None

    def user_by_id(self, uid: int):
        row = self._q("SELECT * FROM users WHERE id=?", (uid,), one=True)
        return dict(row) if row else None

    # ---------- 权益池 ----------
    def sub_active(self, user: dict) -> bool:
        return user.get("sub_expire_at", 0) > int(time.time())

    def trial_active(self, user: dict) -> bool:
        return (user.get("trial_seconds", 0) > 0
                and user.get("trial_expire_at", 0) > int(time.time()))

    def add_money(self, user_id: int, fen: int):
        self._x("UPDATE users SET money_cents = MAX(0, money_cents + ?) WHERE id=?",
                (fen, user_id))

    def add_credits(self, user_id: int, seconds: int):
        self._x("UPDATE users SET credit_seconds = MAX(0, credit_seconds + ?) WHERE id=?",
                (seconds, user_id))

    def add_trial(self, user_id: int, seconds: int):
        self._x("UPDATE users SET trial_seconds = MAX(0, trial_seconds + ?) WHERE id=?",
                (seconds, user_id))

    def extend_sub(self, user_id: int, days: int):
        now = int(time.time())
        user = self.user_by_id(user_id)
        base = max(now, user["sub_expire_at"] if user else 0)
        self._x("UPDATE users SET sub_expire_at=? WHERE id=?",
                (base + days * 86400, user_id))

    def add_ref_earning(self, user_id: int, fen: int):
        self._x("UPDATE users SET ref_earned_fen = ref_earned_fen + ? WHERE id=?",
                (fen, user_id))

    def ref_stats(self, user_id: int) -> dict:
        invited = self._q(
            "SELECT COUNT(*) c FROM users WHERE referred_by=?", (user_id,), one=True)["c"]
        user = self.user_by_id(user_id)
        return {"invited": invited, "earned_fen": user["ref_earned_fen"]}

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
    def log_usage(self, user_id: int, stream: str, seconds: int,
                  pool: str, fen: int = 0):
        self._x(
            "INSERT INTO usage_logs(user_id,stream,seconds,pool,fen,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (user_id, stream, seconds, pool, fen, int(time.time())),
        )
