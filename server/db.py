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
CREATE TABLE IF NOT EXISTS terms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL,             -- 目标语言：zh=队友英语->中文，en=自己中文->英语
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(target, src)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_logs(user_id);
"""

# 老库增量迁移：列不存在才加
_USER_COLUMNS = {
    "banned": "INTEGER NOT NULL DEFAULT 0",
    "last_active_at": "INTEGER NOT NULL DEFAULT 0",
    "note": "TEXT NOT NULL DEFAULT ''",
}

PAGE_MAX = 200


class DB:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            # 游客 device_id 部分唯一索引：杜绝 SELECT-then-INSERT 竞态产生重复游客
            # （端点跑在线程池里可并发）；老库已有重复数据时先去重（每个 device_id
            # 保留最小 id，不做合并）再建索引
            try:
                self._conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_guest_device "
                    "ON users(device_id) WHERE username IS NULL")
                self._conn.commit()
            except sqlite3.IntegrityError:
                self._conn.execute(
                    "DELETE FROM users WHERE username IS NULL AND id NOT IN ("
                    "SELECT MIN(id) FROM users WHERE username IS NULL "
                    "GROUP BY device_id)")
                self._conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_guest_device "
                    "ON users(device_id) WHERE username IS NULL")
                self._conn.commit()
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(users)")}
            for name, ddl in _USER_COLUMNS.items():
                if name not in cols:
                    self._conn.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
            self._conn.commit()

    def _q(self, sql: str, args=(), one=False):
        with self._lock:
            cur = self._conn.execute(sql, args)
            rows = cur.fetchall()
        return (rows[0] if rows else None) if one else rows

    def _x(self, sql: str, args=()):
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
        return cur

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

    def _rotate_guest_token(self, row) -> dict:
        """已存在的游客：轮换 token，防止凭 device_id 冒领旧 token。"""
        token = secrets.token_hex(24)
        self._x("UPDATE users SET token=? WHERE id=?", (token, row["id"]))
        return dict(self._q("SELECT * FROM users WHERE id=?",
                            (row["id"],), one=True))

    def create_guest(self, device_id: str, trial_seconds: int,
                     trial_days: int) -> tuple[dict, bool]:
        """返回 (user, created)：created=True 表示新建了账号，
        False 表示已有游客轮换了 token（不占每日新建配额）。"""
        row = self._q("SELECT * FROM users WHERE device_id=? AND username IS NULL",
                      (device_id,), one=True)
        if row:
            return self._rotate_guest_token(row), False
        token = secrets.token_hex(24)
        now = int(time.time())
        trial_exp = now + trial_days * 86400 if trial_seconds > 0 else 0
        try:
            cur = self._x(
                "INSERT INTO users(device_id,token,trial_seconds,trial_expire_at,"
                "ref_code,created_at) VALUES(?,?,?,?,?,?)",
                (device_id, token, trial_seconds, trial_exp,
                 secrets.token_hex(4), now),
            )
            uid = cur.lastrowid
        except sqlite3.IntegrityError:
            # 并发同 device_id：唯一索引兜底，改走已有游客轮换 token
            row = self._q("SELECT * FROM users WHERE device_id=? AND username IS NULL",
                          (device_id,), one=True)
            if row:
                return self._rotate_guest_token(row), False
            raise   # 非唯一索引冲突（token/ref_code 撞了），原样抛出
        # 按 id 回读（不按 token：并发请求可能已把 token 轮换掉）
        return dict(self._q("SELECT * FROM users WHERE id=?", (uid,), one=True)), True

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

    def set_trial(self, user_id: int, seconds: int, days: int):
        """直接设定免费额度并重置有效期（管理发放用）。"""
        exp = int(time.time()) + days * 86400 if seconds > 0 else 0
        self._x("UPDATE users SET trial_seconds=?, trial_expire_at=? WHERE id=?",
                (seconds, exp, user_id))

    def _deduct(self, col: str, user_id: int, amount: int) -> int:
        """原子扣减：扣 min(amount, 剩余)，返回实际扣掉的量。
        先尝试全额扣（WHERE col >= amount），失败则在同一把锁内查余额并清零，
        全程持锁，杜绝同账号多会话双花。"""
        if amount <= 0:
            return 0
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE users SET {col} = {col} - ? WHERE id=? AND {col} >= ?",
                (amount, user_id, amount))
            if cur.rowcount == 1:
                self._conn.commit()
                return amount
            row = self._conn.execute(
                f"SELECT {col} FROM users WHERE id=?", (user_id,)).fetchone()
            avail = int(row[0]) if row and row[0] else 0
            if avail > 0:
                cur = self._conn.execute(
                    f"UPDATE users SET {col} = 0 WHERE id=? AND {col} > 0",
                    (user_id,))
                self._conn.commit()
                return avail if cur.rowcount == 1 else 0
            self._conn.commit()
            return 0

    def deduct_trial(self, user_id: int, seconds: int) -> int:
        """扣免费额度：过期池子不扣（防止过期后白嫖），且过期残留顺手清零自清理。
        与 _deduct 同样全程持锁防双花；credit/money 扣减不变。"""
        if seconds <= 0:
            return 0
        now = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET trial_seconds = trial_seconds - ? "
                "WHERE id=? AND trial_seconds >= ? AND trial_expire_at > ?",
                (seconds, user_id, seconds, now))
            if cur.rowcount == 1:
                self._conn.commit()
                return seconds
            # 部分扣：只查未过期的剩余（过期视为 0，不扣）
            row = self._conn.execute(
                "SELECT trial_seconds FROM users WHERE id=? AND trial_expire_at > ?",
                (user_id, now)).fetchone()
            avail = int(row[0]) if row and row[0] else 0
            if avail > 0:
                cur = self._conn.execute(
                    "UPDATE users SET trial_seconds = 0 WHERE id=? "
                    "AND trial_seconds > 0 AND trial_expire_at > ?",
                    (user_id, now))
                self._conn.commit()
                return avail if cur.rowcount == 1 else 0
            # 已过期：清零残留池子（自清理），本池子不扣
            self._conn.execute(
                "UPDATE users SET trial_seconds=0 WHERE id=? AND trial_expire_at <= ?",
                (user_id, now))
            self._conn.commit()
            return 0

    def deduct_credits(self, user_id: int, seconds: int) -> int:
        return self._deduct("credit_seconds", user_id, seconds)

    def deduct_money(self, user_id: int, fen: int) -> int:
        return self._deduct("money_cents", user_id, fen)

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

    def close_order(self, out_trade_no) -> bool:
        return self._x("UPDATE orders SET status='closed' WHERE out_trade_no=? "
                       "AND status='pending'", (out_trade_no,)).rowcount > 0

    # ---------- 用量 ----------
    def log_usage(self, user_id: int, stream: str, seconds: int,
                  pool: str, fen: int = 0):
        self._x(
            "INSERT INTO usage_logs(user_id,stream,seconds,pool,fen,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (user_id, stream, seconds, pool, fen, int(time.time())),
        )

    def touch_user(self, user_id: int):
        self._x("UPDATE users SET last_active_at=? WHERE id=?",
                (int(time.time()), user_id))

    # ---------- 热词表 ----------
    def terms_count(self) -> int:
        return self._q("SELECT COUNT(*) c FROM terms", one=True)["c"]

    def terms_for(self, target: str) -> dict:
        rows = self._q("SELECT src, dst FROM terms WHERE target=? ORDER BY id",
                       (target,))
        return {r["src"]: r["dst"] for r in rows}

    def terms_list(self, target: str = "", q: str = "") -> list[dict]:
        sql, args = "SELECT * FROM terms WHERE 1=1", []
        if target:
            sql += " AND target=?"
            args.append(target)
        if q:
            sql += " AND (src LIKE ? OR dst LIKE ?)"
            args += [f"%{q}%", f"%{q}%"]
        return [dict(r) for r in self._q(sql + " ORDER BY target, id", args)]

    def terms_upsert(self, target: str, pairs: dict) -> int:
        now = int(time.time())
        with self._lock:
            self._conn.executemany(
                "INSERT INTO terms(target,src,dst,created_at) VALUES(?,?,?,?) "
                "ON CONFLICT(target,src) DO UPDATE SET dst=excluded.dst",
                [(target, s, d, now) for s, d in pairs.items()])
            self._conn.commit()
        return len(pairs)

    def terms_delete(self, ids: list[int]) -> int:
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        return self._x(f"DELETE FROM terms WHERE id IN ({marks})", ids).rowcount

    def terms_clear(self, target: str = "") -> int:
        if target:
            return self._x("DELETE FROM terms WHERE target=?", (target,)).rowcount
        return self._x("DELETE FROM terms").rowcount

    # ---------- 运行时设置 ----------
    def settings_all(self) -> dict:
        return {r["key"]: r["value"] for r in self._q("SELECT * FROM settings")}

    def setting_set(self, key: str, value: str):
        self._x("INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # ---------- 管理后台查询 ----------
    @staticmethod
    def _page(page: int, size: int) -> tuple[int, int]:
        size = max(1, min(int(size or 20), PAGE_MAX))
        return size, max(0, (max(1, int(page or 1)) - 1) * size)

    def admin_users(self, q: str = "", page: int = 1, size: int = 20,
                    kind: str = "") -> dict:
        where, args = ["1=1"], []
        if q:
            where.append("(username LIKE ? OR device_id LIKE ? OR ref_code=? "
                         "OR CAST(id AS TEXT)=? OR note LIKE ?)")
            args += [f"%{q}%", f"%{q}%", q, q, f"%{q}%"]
        if kind == "guest":
            where.append("username IS NULL")
        elif kind == "account":
            where.append("username IS NOT NULL")
        elif kind == "banned":
            where.append("banned=1")
        elif kind == "sub":
            where.append("sub_expire_at > ?")
            args.append(int(time.time()))
        w = " AND ".join(where)
        total = self._q(f"SELECT COUNT(*) c FROM users WHERE {w}", args, one=True)["c"]
        size, off = self._page(page, size)
        rows = self._q(
            f"SELECT id,username,device_id,money_cents,credit_seconds,trial_seconds,"
            f"trial_expire_at,sub_expire_at,ref_code,referred_by,ref_earned_fen,"
            f"banned,last_active_at,note,created_at FROM users WHERE {w} "
            f"ORDER BY id DESC LIMIT ? OFFSET ?", args + [size, off])
        return {"total": total, "items": [dict(r) for r in rows]}

    def admin_orders(self, q: str = "", status: str = "", page: int = 1,
                     size: int = 20, user_id: int = 0) -> dict:
        where, args = ["1=1"], []
        if q:
            where.append("(o.out_trade_no LIKE ? OR o.trade_no LIKE ? OR u.username LIKE ?)")
            args += [f"%{q}%", f"%{q}%", f"%{q}%"]
        if status:
            where.append("o.status=?")
            args.append(status)
        if user_id:
            where.append("o.user_id=?")
            args.append(user_id)
        w = " AND ".join(where)
        base = f"FROM orders o LEFT JOIN users u ON u.id=o.user_id WHERE {w}"
        total = self._q(f"SELECT COUNT(*) c {base}", args, one=True)["c"]
        size, off = self._page(page, size)
        rows = self._q(f"SELECT o.*, u.username {base} ORDER BY o.created_at DESC "
                       f"LIMIT ? OFFSET ?", args + [size, off])
        return {"total": total, "items": [dict(r) for r in rows]}

    def admin_usage(self, user_id: int = 0, page: int = 1, size: int = 50) -> dict:
        where, args = "1=1", []
        if user_id:
            where, args = "l.user_id=?", [user_id]
        total = self._q(f"SELECT COUNT(*) c FROM usage_logs l WHERE {where}",
                        args, one=True)["c"]
        size, off = self._page(page, size)
        rows = self._q(
            f"SELECT l.*, u.username FROM usage_logs l LEFT JOIN users u "
            f"ON u.id=l.user_id WHERE {where} ORDER BY l.id DESC LIMIT ? OFFSET ?",
            args + [size, off])
        return {"total": total, "items": [dict(r) for r in rows]}

    def set_user_fields(self, user_id: int, **fields):
        allowed = {"banned", "note", "password_hash", "token"}
        cols = {k: v for k, v in fields.items() if k in allowed}
        if not cols:
            return
        sets = ",".join(f"{k}=?" for k in cols)
        self._x(f"UPDATE users SET {sets} WHERE id=?", [*cols.values(), user_id])

    def stats(self, days: int = 14) -> dict:
        now = int(time.time())
        lt = time.localtime(now)
        today0 = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
        since = today0 - (days - 1) * 86400
        fen = "CAST(ROUND(CAST(money AS REAL)*100) AS INTEGER)"
        one = lambda sql, a=(): self._q(sql, a, one=True)[0] or 0  # noqa: E731
        day = "date({col},'unixepoch','localtime')"

        def series(sql, args):
            return {r[0]: r[1] or 0 for r in self._q(sql, args)}

        rev = series(f"SELECT {day.format(col='paid_at')} d, SUM({fen}) FROM orders "
                     f"WHERE status='paid' AND paid_at>=? GROUP BY d", (since,))
        use = series(f"SELECT {day.format(col='created_at')} d, SUM(seconds) FROM usage_logs "
                     f"WHERE created_at>=? GROUP BY d", (since,))
        reg = series(f"SELECT {day.format(col='created_at')} d, COUNT(*) FROM users "
                     f"WHERE created_at>=? GROUP BY d", (since,))
        labels = [time.strftime("%Y-%m-%d", time.localtime(since + i * 86400 + 3600))
                  for i in range(days)]
        return {
            "users_total": one("SELECT COUNT(*) FROM users"),
            "users_accounts": one("SELECT COUNT(*) FROM users WHERE username IS NOT NULL"),
            "users_today": one("SELECT COUNT(*) FROM users WHERE created_at>=?", (today0,)),
            "active_24h": one("SELECT COUNT(*) FROM users WHERE last_active_at>=?",
                              (now - 86400,)),
            "subs_active": one("SELECT COUNT(*) FROM users WHERE sub_expire_at>?", (now,)),
            "revenue_total_fen": one(f"SELECT SUM({fen}) FROM orders WHERE status='paid'"),
            "revenue_today_fen": one(f"SELECT SUM({fen}) FROM orders WHERE status='paid' "
                                     f"AND paid_at>=?", (today0,)),
            "orders_paid": one("SELECT COUNT(*) FROM orders WHERE status='paid'"),
            "orders_pending": one("SELECT COUNT(*) FROM orders WHERE status='pending'"),
            "usage_today_seconds": one("SELECT SUM(seconds) FROM usage_logs "
                                       "WHERE created_at>=?", (today0,)),
            "usage_total_seconds": one("SELECT SUM(seconds) FROM usage_logs"),
            "balance_liability_fen": one("SELECT SUM(money_cents) FROM users"),
            "series": {
                "labels": labels,
                "revenue_fen": [rev.get(d, 0) for d in labels],
                "usage_seconds": [use.get(d, 0) for d in labels],
                "new_users": [reg.get(d, 0) for d in labels],
            },
        }
