"""计费服务器入口：REST(认证/余额/下单/查单/回调/推广) + WS(翻译中转)。

运行：uvicorn app:app --host 0.0.0.0 --port 8765
"""
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import relay
import valorant_terms
from db import DB
from epay import EPayClient
from relay import RelaySession

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("app")

db = DB(config.DB_PATH)
epay = EPayClient(config.ZPAY_BASE_URL, config.ZPAY_PID, config.ZPAY_KEY)
http: httpx.AsyncClient | None = None   # 在 lifespan 启动时创建
ADMIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin")
TARGETS = ("zh", "en", "ja", "ko", "ru", "fr", "de", "es", "pt")


# ---------------- 运行时设置 / 热词 ----------------
def _load_runtime():
    """settings 表覆盖环境变量默认值（管理后台改过的参数重启后依然生效）。"""
    for k, v in db.settings_all().items():
        if k in config.RUNTIME:
            try:
                config.apply_runtime(k, json.loads(v))
            except (ValueError, TypeError):
                log.warning("忽略非法运行时参数 %s=%r", k, v)


def _seed_terms():
    """首次启动把内置游戏术语写入热词表；之后以库为准（管理员删光也不回灌）。"""
    if db.settings_all().get("terms_seeded"):
        return
    if db.terms_count() == 0:
        db.terms_upsert("zh", valorant_terms.TERMS_EN2ZH)
        db.terms_upsert("en", valorant_terms.TERMS_ZH2EN)
    db.setting_set("terms_seeded", "1")


_terms_cache: dict[str, dict] = {}


def _terms_for(target: str) -> dict:
    if target not in _terms_cache:
        _terms_cache[target] = db.terms_for(target)
    return _terms_cache[target]


_load_runtime()
_seed_terms()
relay.terms_provider = _terms_for


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # httpx 客户端随应用生命周期创建/关闭（替代已废弃的 on_event 写法）
    global http
    http = httpx.AsyncClient()
    if config.ADMIN_TOKEN and len(config.ADMIN_TOKEN) < 16:
        log.warning("ADMIN_TOKEN 过短（<16 位），管理后台暴露在公网时极易被爆破")
    if not config.ADMIN_TOKEN:
        log.warning("未设置 ADMIN_TOKEN：管理后台 /admin 已禁用")
    yield
    await http.aclose()


app = FastAPI(title="VTranslate Billing Server", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/admin"):
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'")
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# ---------------- 工具 ----------------
def _hash_pwd(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(),
                            bytes.fromhex(salt), 100_000).hex()
    return f"{salt}${h}"


def _check_pwd(password: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
        return _hash_pwd(password, salt) == stored
    except Exception:
        return False


def _auth(req: Request):
    token = req.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    user = db.user_by_token(token) if token else None
    return None if user and user.get("banned") else user


# ---------------- 限流（内存滑动窗口，按客户端 IP） ----------------
_rl_lock = threading.Lock()
_rl_hits: dict[str, deque] = defaultdict(deque)


def _client_ip(req: Request) -> str:
    """限流按客户端 IP；仅在 TRUST_PROXY 开启时信 X-Forwarded-For 首跳
    （反代场景 req.client 是代理 IP，会导致所有用户共享一个限流桶）。"""
    if config.TRUST_PROXY:
        xff = req.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip() or "unknown"
    return req.client.host if req.client else "unknown"


def _rate_limit(req: Request, limit: int, window: int = 60) -> bool:
    """window 秒内最多 limit 次；超限返回 True。"""
    ip = _client_ip(req)
    key = f"{req.url.path}|{ip}"
    now = time.monotonic()
    with _rl_lock:
        dq = _rl_hits[key]
        while dq and now - dq[0] > window:
            dq.popleft()
        if len(dq) >= limit:
            return True
        dq.append(now)
    return False


def _too_many():
    return JSONResponse({"code": 0, "msg": "请求过于频繁"}, status_code=429)


def _money_eq(a, b) -> bool:
    """金额比较走 Decimal：订单存 '10.00'，回调送 '10.0'/'10' 视为相等，
    防止网关格式差异导致已支付订单永远无法入账。非法数值返回 False。"""
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except InvalidOperation:
        return False


# ---- 游客新建配额（内存滑动窗口 24h，按客户端 IP；与限流同款 dict+deque） ----
_gc_lock = threading.Lock()
_gc_hits: dict[str, deque] = defaultdict(deque)


def _guest_cap_reached(ip: str) -> bool:
    """该 IP 24h 内新建游客数是否已达上限（只查不记）。"""
    now = time.monotonic()
    with _gc_lock:
        dq = _gc_hits[ip]
        while dq and now - dq[0] > 86400:
            dq.popleft()
        return len(dq) >= config.GUEST_GRANTS_PER_IP_PER_DAY


def _guest_slot_take(ip: str):
    """真正新建了游客账号才记账（轮换已有游客 token 不占额度）。"""
    with _gc_lock:
        _gc_hits[ip].append(time.monotonic())


def _entitlement(user: dict) -> dict:
    price = config.PRICE_FEN_PER_MINUTE
    money_min = user["money_cents"] / price if price > 0 else 0
    return {
        "username": user.get("username") or "游客",
        "ref_code": user.get("ref_code", ""),
        "money_cents": user["money_cents"],
        "money_yuan": f"{user['money_cents']/100:.2f}",
        "money_minutes": int(money_min),
        "credit_seconds": user["credit_seconds"],
        "credit_minutes": user["credit_seconds"] // 60,
        "trial_seconds": user["trial_seconds"],
        "trial_expire_at": user["trial_expire_at"],
        "trial_active": db.trial_active(user),
        "sub_expire_at": user["sub_expire_at"],
        "sub_active": db.sub_active(user),
        "price_fen_per_minute": price,
    }


# ---------------- 元信息 ----------------
@app.get("/api/meta")
def meta():
    return {"code": 1, "announcement": config.ANNOUNCEMENT,
            "price_fen_per_minute": config.PRICE_FEN_PER_MINUTE,
            "voices": config.VOICES,
            "targets": list(TARGETS),
            "allow_register": config.ALLOW_REGISTER,
            "allow_guest": config.ALLOW_GUEST,
            "trial_seconds": config.TRIAL_SECONDS}


@app.get("/healthz")
def healthz():
    return {"ok": True, "sessions": len(relay.ACTIVE)}


# ---------------- 认证 ----------------
class RegisterReq(BaseModel):
    username: str
    password: str
    ref: str = ""          # 推广码（可选）
    device_id: str = ""


@app.post("/api/auth/register")
def register(req: RegisterReq, request: Request):
    if _rate_limit(request, 10):
        return _too_many()
    if not config.ALLOW_REGISTER:
        return JSONResponse({"code": 0, "msg": "暂未开放注册"}, status_code=403)
    username = req.username.strip()
    if not (3 <= len(username) <= 32):
        return JSONResponse({"code": 0, "msg": "用户名需 3-32 位"}, status_code=400)
    if len(req.password) < 6:
        return JSONResponse({"code": 0, "msg": "密码至少 6 位"}, status_code=400)
    if db.user_by_username(username):
        return JSONResponse({"code": 0, "msg": "用户名已存在"}, status_code=400)

    referred_by = None
    if req.ref:
        referrer = db.user_by_ref_code(req.ref.strip())
        if referrer:
            referred_by = referrer["id"]

    user = db.create_account(username, _hash_pwd(req.password),
                             config.TRIAL_SECONDS, config.TRIAL_DAYS,
                             referred_by=referred_by,
                             device_id=req.device_id or None)
    if not user:
        return JSONResponse({"code": 0, "msg": "注册失败"}, status_code=500)
    return {"code": 1, "token": user["token"], **_entitlement(user)}


class LoginReq(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
def login(req: LoginReq, request: Request):
    if _rate_limit(request, 10):
        return _too_many()
    user = db.user_by_username(req.username.strip())
    if not user or not user.get("password_hash") \
            or not _check_pwd(req.password, user["password_hash"]):
        return JSONResponse({"code": 0, "msg": "用户名或密码错误"}, status_code=401)
    if user.get("banned"):
        return JSONResponse({"code": 0, "msg": "账号已被封禁"}, status_code=403)
    # 每次登录换新 token
    db._x("UPDATE users SET token=? WHERE id=?", (secrets.token_hex(24), user["id"]))
    user = db.user_by_id(user["id"])
    return {"code": 1, "token": user["token"], **_entitlement(user)}


class GuestReq(BaseModel):
    device_id: str


@app.post("/api/auth/guest")
def guest(req: GuestReq, request: Request):
    """游客模式：设备码自动注册，保留向后兼容。
    每分钟限流放宽到 60：真正防线是每 IP 每 24h 新建游客配额。"""
    if _rate_limit(request, 60):
        return _too_many()
    if not config.ALLOW_GUEST:
        return JSONResponse({"code": 0, "msg": "游客模式已关闭，请注册账号"}, status_code=403)
    if not req.device_id or len(req.device_id) > 64:
        return JSONResponse({"code": 0, "msg": "device_id 非法"}, status_code=400)
    # 先查配额再落库：超限直接拒绝，防止 429 后重试同 device_id 白拿已有账号 token
    ip = _client_ip(request)
    if _guest_cap_reached(ip):
        return JSONResponse(
            {"code": 0,
             "msg": "Too many guest registrations, please register an account"},
            status_code=429)
    user, created = db.create_guest(req.device_id, config.TRIAL_SECONDS,
                                    config.TRIAL_DAYS)
    if created:
        _guest_slot_take(ip)
    return {"code": 1, "token": user["token"], **_entitlement(user)}


# 兼容老客户端的注册入口
@app.post("/api/register")
def register_legacy(req: GuestReq, request: Request):
    return guest(req, request)


@app.get("/api/me")
def me(req: Request):
    user = _auth(req)
    if not user:
        return JSONResponse({"code": 0, "msg": "未授权"}, status_code=401)
    return {"code": 1, **_entitlement(user)}


@app.get("/api/referral/stats")
def ref_stats(req: Request):
    user = _auth(req)
    if not user:
        return JSONResponse({"code": 0, "msg": "未授权"}, status_code=401)
    s = db.ref_stats(user["id"])
    return {"code": 1, "ref_code": user["ref_code"],
            "invited": s["invited"],
            "earned_fen": s["earned_fen"],
            "earned_yuan": f"{s['earned_fen']/100:.2f}",
            "bonus_percent": config.REF_BONUS_PERCENT}


# ---------------- 商品与支付 ----------------
@app.get("/api/products")
def products():
    return {"code": 1, "products": [
        {"code": k, "name": v["name"], "money": v["money"], "kind": v["kind"],
         "fen": v.get("fen"), "seconds": v.get("seconds"), "days": v.get("days")}
        for k, v in config.PRODUCTS.items()
    ]}


class PayCreateReq(BaseModel):
    product_code: str
    pay_type: str = "alipay"   # alipay | wxpay


@app.post("/api/pay/create")
async def pay_create(req: PayCreateReq, request: Request):
    if _rate_limit(request, 20):
        return _too_many()
    user = _auth(request)
    if not user:
        return JSONResponse({"code": 0, "msg": "未授权"}, status_code=401)
    product = config.PRODUCTS.get(req.product_code)
    if not product:
        return JSONResponse({"code": 0, "msg": "商品不存在"}, status_code=400)
    if req.pay_type not in ("alipay", "wxpay"):
        return JSONResponse({"code": 0, "msg": "支付方式不支持"}, status_code=400)

    out_trade_no = "VT" + time.strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:8]
    db.create_order(out_trade_no, user["id"], req.product_code,
                    product["name"], product["money"], req.pay_type)

    clientip = _client_ip(request)
    if clientip == "unknown":
        clientip = "127.0.0.1"
    page_url = epay.build_page_pay_url(
        name=product["name"], money=product["money"], out_trade_no=out_trade_no,
        pay_type=req.pay_type, notify_url=config.ZPAY_NOTIFY_URL,
        return_url=config.ZPAY_RETURN_URL, param=str(user["id"]), clientip=clientip)

    resp = {"code": 1, "out_trade_no": out_trade_no, "page_url": page_url}
    # 优先走 mapi 拿二维码，失败时客户端可退到 page_url 浏览器收银台
    try:
        data = await epay.create_api_pay(
            http, name=product["name"], money=product["money"],
            out_trade_no=out_trade_no, pay_type=req.pay_type,
            notify_url=config.ZPAY_NOTIFY_URL, param=str(user["id"]),
            clientip=clientip)
        if str(data.get("code")) == "1":
            resp.update({k: data[k] for k in ("payurl", "qrcode", "img", "trade_no")
                         if data.get(k)})
        else:
            resp["mapi_msg"] = data.get("msg", "")
    except Exception as e:
        log.warning("mapi 下单失败，退回页面支付: %s", e)
        resp["mapi_msg"] = str(e)
    return resp


def _fulfill_order(order: dict):
    """发货 + 推广返利（仅 pending->paid 时调用一次）。"""
    product = config.PRODUCTS.get(order["product_code"], {})
    kind = product.get("kind")
    if kind == "money":
        db.add_money(order["user_id"], int(product["fen"]))
    elif kind == "card":
        db.add_credits(order["user_id"], int(product["seconds"]))
    elif kind == "sub":
        db.extend_sub(order["user_id"], int(product["days"]))

    # 返利：按实付金额的 REF_BONUS_PERCENT% 返到邀请人余额
    user = db.user_by_id(order["user_id"])
    if user and user.get("referred_by"):
        try:
            paid_fen = int(round(float(order["money"]) * 100))
        except ValueError:
            paid_fen = 0
        bonus = paid_fen * config.REF_BONUS_PERCENT // 100
        if bonus > 0:
            db.add_money(user["referred_by"], bonus)
            db.add_ref_earning(user["referred_by"], bonus)
            log.info("返利 %d 分给邀请人 #%s (订单 %s)",
                     bonus, user["referred_by"], order["out_trade_no"])


@app.api_route("/api/pay/notify", methods=["GET", "POST"])
async def pay_notify(request: Request):
    """z-pay 异步回调：验签 -> trade_status==TRADE_SUCCESS -> 发货。"""
    if request.method == "POST":
        body = await request.body()
        params = dict(parse_qsl(body.decode("utf-8", "ignore"))) if body else {}
        if not params:  # 有些网关 POST 也走 query
            params = dict(request.query_params)
    else:
        params = dict(request.query_params)
    safe = {**params, "sign": "***"} if "sign" in params else params
    log.info("pay notify: %s", safe)

    if not epay.verify_notify(params):
        log.warning("回调验签失败: %s", safe)
        return PlainTextResponse("sign error", status_code=400)
    if params.get("trade_status") != "TRADE_SUCCESS":
        return PlainTextResponse("success")  # 非成功状态也应答，避免反复回调

    out_trade_no = params.get("out_trade_no", "")
    order = db.get_order(out_trade_no)
    if not order:
        return PlainTextResponse("order not found", status_code=404)
    # 金额防篡改校验（Decimal 比较，兼容 '10.0'/'10' 等格式差异）
    if not _money_eq(order["money"], params.get("money", "")):
        log.warning("回调金额不一致 order=%s notify=%s", order["money"], params.get("money"))
        return PlainTextResponse("money mismatch", status_code=400)

    paid = db.mark_paid(out_trade_no, params.get("trade_no", ""))
    if paid:
        _fulfill_order(paid)
    return PlainTextResponse("success")


@app.get("/api/pay/status")
async def pay_status(out_trade_no: str, request: Request):
    user = _auth(request)
    if not user:
        return JSONResponse({"code": 0, "msg": "未授权"}, status_code=401)
    order = db.get_order(out_trade_no)
    if not order or order["user_id"] != user["id"]:
        return JSONResponse({"code": 0, "msg": "订单不存在"}, status_code=404)

    # pending 且超过 20s 时主动向 z-pay 查单兜底（应对回调丢失/被拦）
    if order["status"] == "pending" and int(time.time()) - order["created_at"] > 20:
        try:
            data = await epay.query_order(http, out_trade_no)
            if str(data.get("status")) == "1":
                paid = db.mark_paid(out_trade_no, str(data.get("trade_no", "")))
                if paid:
                    _fulfill_order(paid)
                    order = paid
        except Exception as e:
            log.warning("主动查单失败: %s", e)

    fresh = db.user_by_token(user["token"])
    return {"code": 1, "status": order["status"], **_entitlement(fresh)}


# ---------------- 管理后台 API ----------------
# 鉴权：先用 ADMIN_TOKEN 登录换取短期会话（X-Admin-Session），
# 兼容脚本直接带 X-Admin-Token。未设置 ADMIN_TOKEN 时整个后台禁用。
_admin_sessions: dict[str, float] = {}
_admin_lock = threading.Lock()


def _token_ok(tok: str) -> bool:
    return bool(config.ADMIN_TOKEN and tok) and hmac.compare_digest(
        tok.encode(), config.ADMIN_TOKEN.encode())


def _is_admin(req: Request) -> bool:
    if not config.ADMIN_TOKEN:
        return False
    sess = req.headers.get("X-Admin-Session", "")
    if sess:
        with _admin_lock:
            exp = _admin_sessions.get(sess)
            if exp and exp > time.time():
                return True
            _admin_sessions.pop(sess, None)
        return False
    return _token_ok(req.headers.get("X-Admin-Token", ""))


def _admin_denied():
    return JSONResponse({"code": 0, "msg": "未授权或登录已过期"}, status_code=401)


def _bad(msg: str, status: int = 400):
    return JSONResponse({"code": 0, "msg": msg}, status_code=status)


class AdminLoginReq(BaseModel):
    token: str


@app.post("/api/admin/login")
def admin_login(req: AdminLoginReq, request: Request):
    if _rate_limit(request, 5):
        return _too_many()
    if not config.ADMIN_TOKEN:
        return _bad("管理后台未启用（服务器未设置 ADMIN_TOKEN）", 403)
    if not _token_ok(req.token.strip()):
        log.warning("管理后台登录失败 ip=%s", _client_ip(request))
        return _bad("令牌错误", 401)
    sess = secrets.token_urlsafe(32)
    ttl = config.ADMIN_SESSION_HOURS * 3600
    now = time.time()
    with _admin_lock:
        for k in [k for k, v in _admin_sessions.items() if v <= now]:
            _admin_sessions.pop(k, None)
        _admin_sessions[sess] = now + ttl
    log.info("管理后台登录 ip=%s", _client_ip(request))
    return {"code": 1, "session": sess, "expires_in": ttl}


@app.post("/api/admin/logout")
def admin_logout(request: Request):
    with _admin_lock:
        _admin_sessions.pop(request.headers.get("X-Admin-Session", ""), None)
    return {"code": 1}


def _public_user(u: dict) -> dict:
    return {k: v for k, v in u.items() if k not in ("password_hash", "token")}


@app.get("/api/admin/stats")
def admin_stats(request: Request):
    if not _is_admin(request):
        return _admin_denied()
    s = db.stats()
    s["sessions_live"] = len(relay.ACTIVE)
    s["system"] = {
        "model": config.TRANSLATE_MODEL,
        "mock_upstream": config.MOCK_UPSTREAM,
        "dashscope_key": bool(config.DASHSCOPE_API_KEY),
        "zpay": bool(config.ZPAY_PID and config.ZPAY_KEY and config.ZPAY_NOTIFY_URL),
        "trust_proxy": config.TRUST_PROXY,
        "terms_zh": len(_terms_for("zh")), "terms_en": len(_terms_for("en")),
    }
    return {"code": 1, **s}


@app.get("/api/admin/users")
def admin_users(request: Request, q: str = "", kind: str = "",
                page: int = 1, size: int = 20):
    if not _is_admin(request):
        return _admin_denied()
    return {"code": 1, **db.admin_users(q.strip(), page, size, kind)}


def _live_of(uid: int) -> list:
    return [s for s in list(relay.ACTIVE.values()) if s.user_id == uid]


@app.get("/api/admin/users/{uid}")
def admin_user_detail(uid: int, request: Request):
    if not _is_admin(request):
        return _admin_denied()
    user = db.user_by_id(uid)
    if not user:
        return _bad("用户不存在", 404)
    return {"code": 1, "user": _public_user(user), "entitlement": _entitlement(user),
            "referral": db.ref_stats(uid),
            "orders": db.admin_orders(user_id=uid, size=10)["items"],
            "usage": db.admin_usage(user_id=uid, size=20)["items"],
            "sessions": [s.info() for s in _live_of(uid)]}


class AdjustReq(BaseModel):
    fen: int = 0                 # 余额增减（分，可负）
    seconds: int = 0             # 时长卡增减（秒，可负）
    sub_days: int = 0            # 订阅增减（天，可负）
    trial_seconds: int = -1      # >=0 时直接设定免费额度
    trial_days: int = -1
    note: str | None = None


@app.post("/api/admin/users/{uid}/adjust")
def admin_adjust(uid: int, req: AdjustReq, request: Request):
    if not _is_admin(request):
        return _admin_denied()
    if not db.user_by_id(uid):
        return _bad("用户不存在", 404)
    if abs(req.fen) > 10_000_000 or abs(req.seconds) > 100_000_000 \
            or abs(req.sub_days) > 3650:
        return _bad("数值超出范围")
    if req.fen:
        db.add_money(uid, req.fen)
    if req.seconds:
        db.add_credits(uid, req.seconds)
    if req.sub_days:
        db.extend_sub(uid, req.sub_days)
    if req.trial_seconds >= 0:
        db.set_trial(uid, req.trial_seconds,
                     req.trial_days if req.trial_days >= 0 else config.TRIAL_DAYS)
    if req.note is not None:
        db.set_user_fields(uid, note=req.note.strip()[:200])
    log.info("管理员调整用户 #%s: %s", uid, req.model_dump(exclude_defaults=True))
    return {"code": 1, "entitlement": _entitlement(db.user_by_id(uid))}


async def _kick_user(uid: int, code: str, msg: str) -> int:
    live = _live_of(uid)
    for s in live:
        await s.kick(code, msg)
    return len(live)


class BanReq(BaseModel):
    banned: bool


@app.post("/api/admin/users/{uid}/ban")
async def admin_ban(uid: int, req: BanReq, request: Request):
    if not _is_admin(request):
        return _admin_denied()
    if not db.user_by_id(uid):
        return _bad("用户不存在", 404)
    fields = {"banned": 1 if req.banned else 0}
    if req.banned:
        fields["token"] = secrets.token_hex(24)   # 旧 token 立即失效
    db.set_user_fields(uid, **fields)
    kicked = await _kick_user(uid, "banned", "账号已被封禁") if req.banned else 0
    log.info("管理员%s用户 #%s", "封禁" if req.banned else "解封", uid)
    return {"code": 1, "kicked": kicked}


@app.post("/api/admin/users/{uid}/kick")
async def admin_kick_user(uid: int, request: Request):
    if not _is_admin(request):
        return _admin_denied()
    if not db.user_by_id(uid):
        return _bad("用户不存在", 404)
    db.set_user_fields(uid, token=secrets.token_hex(24))
    return {"code": 1, "kicked": await _kick_user(uid, "kicked", "登录已失效，请重新登录")}


class PasswordReq(BaseModel):
    password: str


@app.post("/api/admin/users/{uid}/password")
def admin_password(uid: int, req: PasswordReq, request: Request):
    if not _is_admin(request):
        return _admin_denied()
    user = db.user_by_id(uid)
    if not user or not user.get("username"):
        return _bad("仅注册账号可重置密码", 404)
    if len(req.password) < 6:
        return _bad("密码至少 6 位")
    db.set_user_fields(uid, password_hash=_hash_pwd(req.password),
                       token=secrets.token_hex(24))
    return {"code": 1}


@app.get("/api/admin/orders")
def admin_orders(request: Request, q: str = "", status: str = "",
                 page: int = 1, size: int = 20):
    if not _is_admin(request):
        return _admin_denied()
    return {"code": 1, **db.admin_orders(q.strip(), status, page, size)}


@app.post("/api/admin/orders/{otn}/fulfill")
def admin_fulfill(otn: str, request: Request):
    """回调丢失且确认已收款时手动补单（仅 pending 生效，天然防重复发货）。"""
    if not _is_admin(request):
        return _admin_denied()
    paid = db.mark_paid(otn, "manual")
    if not paid:
        return _bad("订单不存在或不是待支付状态")
    _fulfill_order(paid)
    log.info("管理员手动补单 %s", otn)
    return {"code": 1}


@app.post("/api/admin/orders/{otn}/sync")
async def admin_sync_order(otn: str, request: Request):
    if not _is_admin(request):
        return _admin_denied()
    order = db.get_order(otn)
    if not order:
        return _bad("订单不存在", 404)
    if order["status"] != "pending":
        return {"code": 1, "status": order["status"]}
    try:
        data = await epay.query_order(http, otn)
    except Exception as e:
        return _bad(f"查单失败: {e}", 502)
    if str(data.get("status")) == "1":
        paid = db.mark_paid(otn, str(data.get("trade_no", "")))
        if paid:
            _fulfill_order(paid)
        return {"code": 1, "status": "paid"}
    return {"code": 1, "status": "pending", "gateway": data.get("msg", "")}


@app.post("/api/admin/orders/{otn}/close")
def admin_close_order(otn: str, request: Request):
    if not _is_admin(request):
        return _admin_denied()
    if not db.close_order(otn):
        return _bad("仅待支付订单可关闭")
    return {"code": 1}


@app.get("/api/admin/usage")
def admin_usage(request: Request, user_id: int = 0, page: int = 1, size: int = 50):
    if not _is_admin(request):
        return _admin_denied()
    return {"code": 1, **db.admin_usage(user_id, page, size)}


@app.get("/api/admin/sessions")
def admin_sessions(request: Request):
    if not _is_admin(request):
        return _admin_denied()
    return {"code": 1, "items": [s.info() for s in list(relay.ACTIVE.values())]}


@app.post("/api/admin/sessions/{sid}/kick")
async def admin_kick_session(sid: int, request: Request):
    if not _is_admin(request):
        return _admin_denied()
    s = relay.ACTIVE.get(sid)
    if not s:
        return _bad("会话不存在或已结束", 404)
    await s.kick()
    return {"code": 1}


# ---- 热词表 ----
TERM_MAX_LEN = 100


class TermsReq(BaseModel):
    target: str
    pairs: dict[str, str]


def _valid_target(t: str) -> bool:
    return t in TARGETS


@app.get("/api/admin/terms")
def admin_terms(request: Request, target: str = "", q: str = ""):
    if not _is_admin(request):
        return _admin_denied()
    return {"code": 1, "items": db.terms_list(target, q.strip()),
            "limit": relay.MAX_PHRASES}


@app.post("/api/admin/terms")
def admin_terms_upsert(req: TermsReq, request: Request):
    if not _is_admin(request):
        return _admin_denied()
    if not _valid_target(req.target):
        return _bad("目标语言不支持")
    pairs = {s.strip(): d.strip() for s, d in req.pairs.items()
             if s.strip() and d.strip()}
    if not pairs:
        return _bad("没有有效词条")
    if any(len(s) > TERM_MAX_LEN or len(d) > TERM_MAX_LEN for s, d in pairs.items()):
        return _bad(f"单个词条不超过 {TERM_MAX_LEN} 字")
    existing = db.terms_for(req.target)
    new_keys = set(pairs) - set(existing)
    if len(existing) + len(new_keys) > relay.MAX_PHRASES:
        return _bad(f"每种目标语言最多 {relay.MAX_PHRASES} 条热词")
    n = db.terms_upsert(req.target, pairs)
    _terms_cache.clear()
    return {"code": 1, "count": n}


class IdsReq(BaseModel):
    ids: list[int]


@app.post("/api/admin/terms/delete")
def admin_terms_delete(req: IdsReq, request: Request):
    if not _is_admin(request):
        return _admin_denied()
    n = db.terms_delete(req.ids[:5000])
    _terms_cache.clear()
    return {"code": 1, "deleted": n}


class TargetReq(BaseModel):
    target: str


@app.post("/api/admin/terms/reset")
def admin_terms_reset(req: TargetReq, request: Request):
    """恢复某目标语言的内置游戏术语（清空后重新写入）。"""
    if not _is_admin(request):
        return _admin_denied()
    builtin = valorant_terms.for_target(req.target)
    if not builtin:
        return _bad("该目标语言没有内置词表")
    db.terms_clear(req.target)
    db.terms_upsert(req.target, builtin)
    _terms_cache.clear()
    return {"code": 1, "count": len(builtin)}


# ---- 运行时参数 ----
@app.get("/api/admin/settings")
def admin_settings(request: Request):
    if not _is_admin(request):
        return _admin_denied()
    return {"code": 1, "values": config.runtime_values(),
            "products": config.PRODUCTS, "voices": config.VOICES}


@app.put("/api/admin/settings")
def admin_settings_put(body: dict, request: Request):
    if not _is_admin(request):
        return _admin_denied()
    # 任一项非法则整体回滚，避免半截生效
    backup = config.runtime_values()
    applied = {}
    try:
        for k, v in body.items():
            applied[k] = config.apply_runtime(k, v)
    except (ValueError, TypeError) as e:
        for k, v in backup.items():
            config.apply_runtime(k, v)
        return _bad(str(e))
    for k, v in applied.items():
        db.setting_set(k, json.dumps(v, ensure_ascii=False))
    log.info("管理员修改运行时参数: %s", list(applied))
    return {"code": 1, "values": config.runtime_values()}


# ---- 兼容脚本：一次性发放 ----
class GrantReq(BaseModel):
    username: str = ""
    device_id: str = ""
    seconds: int = 0        # 时长卡秒数
    fen: int = 0            # 余额分
    sub_days: int = 0
    trial_seconds: int = 0  # 直接设定免费额度（并重置有效期）
    trial_days: int = -1    # 免费额度有效期天数；-1=沿用 config.TRIAL_DAYS


@app.post("/api/admin/grant")
def admin_grant(req: GrantReq, request: Request):
    if not _is_admin(request):
        return JSONResponse({"code": 0, "msg": "forbidden"}, status_code=403)
    user = (db.user_by_username(req.username) if req.username
            else db.create_guest(req.device_id or uuid.uuid4().hex, 0, 0)[0])
    if not user:
        return JSONResponse({"code": 0, "msg": "用户不存在"}, status_code=404)
    if req.seconds:
        db.add_credits(user["id"], req.seconds)
    if req.fen:
        db.add_money(user["id"], req.fen)
    if req.sub_days:
        db.extend_sub(user["id"], req.sub_days)
    if req.trial_seconds:
        db.set_trial(user["id"], req.trial_seconds,
                     req.trial_days if req.trial_days >= 0 else config.TRIAL_DAYS)
    return {"code": 1, **_entitlement(db.user_by_id(user["id"]))}


# ---------------- 翻译中转 WS ----------------
@app.websocket("/ws/translate")
async def ws_translate(ws: WebSocket, token: str = "", stream: str = "mate",
                       target: str = "zh", voice: str = "", audio: int = 0):
    await ws.accept()
    # 优先取 Authorization: Bearer，兼容 ?token= 查询参数（浏览器客户端）
    hdr = ws.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    user = db.user_by_token(hdr or token)
    if not user or user.get("banned"):
        code, msg = ("banned", "账号已被封禁") if user else ("unauthorized", "token 无效")
        await ws.send_text(json.dumps({"type": "error", "code": code, "msg": msg},
                                      ensure_ascii=False))
        await ws.close()
        return
    if stream not in ("mate", "self"):
        stream = "mate"
    if target not in TARGETS:
        target = "zh"
    if voice not in config.VOICES:
        voice = config.VOICES[0] if config.VOICES else "Cherry"
    session = RelaySession(ws, user, stream, target, db,
                           voice=voice, want_audio=bool(audio))
    session.client_ip = ws.client.host if ws.client else ""
    if config.TRUST_PROXY:
        xff = ws.headers.get("X-Forwarded-For", "")
        session.client_ip = xff.split(",")[0].strip() or session.client_ip
    try:
        await session.run()
    except WebSocketDisconnect:
        pass
    finally:
        await session.close()


# ---------------- 管理后台静态页 ----------------
@app.get("/")
def root():
    return RedirectResponse("/admin/")


if os.path.isdir(ADMIN_DIR):
    app.mount("/admin", StaticFiles(directory=ADMIN_DIR, html=True), name="admin")
