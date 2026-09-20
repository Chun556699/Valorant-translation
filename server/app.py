"""计费服务器入口：REST(认证/余额/下单/查单/回调/推广) + WS(翻译中转)。

运行：uvicorn app:app --host 0.0.0.0 --port 8765
"""
import hashlib
import logging
import secrets
import time
import uuid
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse, JSONResponse
from pydantic import BaseModel

import config
from db import DB
from epay import EPayClient
from relay import RelaySession

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("app")

app = FastAPI(title="VTranslate Billing Server")
db = DB(config.DB_PATH)
epay = EPayClient(config.ZPAY_BASE_URL, config.ZPAY_PID, config.ZPAY_KEY)
http = httpx.AsyncClient()


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
    return db.user_by_token(token) if token else None


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
            "trial_seconds": config.TRIAL_SECONDS}


# ---------------- 认证 ----------------
class RegisterReq(BaseModel):
    username: str
    password: str
    ref: str = ""          # 推广码（可选）
    device_id: str = ""


@app.post("/api/auth/register")
def register(req: RegisterReq):
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
def login(req: LoginReq):
    user = db.user_by_username(req.username.strip())
    if not user or not user.get("password_hash") \
            or not _check_pwd(req.password, user["password_hash"]):
        return JSONResponse({"code": 0, "msg": "用户名或密码错误"}, status_code=401)
    # 每次登录换新 token
    db._x("UPDATE users SET token=? WHERE id=?", (secrets.token_hex(24), user["id"]))
    user = db.user_by_id(user["id"])
    return {"code": 1, "token": user["token"], **_entitlement(user)}


class GuestReq(BaseModel):
    device_id: str


@app.post("/api/auth/guest")
def guest(req: GuestReq):
    """游客模式：设备码自动注册，保留向后兼容。"""
    if not req.device_id or len(req.device_id) > 64:
        return JSONResponse({"code": 0, "msg": "device_id 非法"}, status_code=400)
    user = db.create_guest(req.device_id, config.TRIAL_SECONDS, config.TRIAL_DAYS)
    return {"code": 1, "token": user["token"], **_entitlement(user)}


# 兼容老客户端的注册入口
@app.post("/api/register")
def register_legacy(req: GuestReq):
    return guest(req)


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

    clientip = request.client.host if request.client else "127.0.0.1"
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
    log.info("pay notify: %s", params)

    if not epay.verify_notify(params):
        log.warning("回调验签失败: %s", params)
        return PlainTextResponse("sign error", status_code=400)
    if params.get("trade_status") != "TRADE_SUCCESS":
        return PlainTextResponse("success")  # 非成功状态也应答，避免反复回调

    out_trade_no = params.get("out_trade_no", "")
    order = db.get_order(out_trade_no)
    if not order:
        return PlainTextResponse("order not found", status_code=404)
    # 金额防篡改校验
    if str(order["money"]) != str(params.get("money", "")):
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


# ---------------- 管理（手动发放，调试用） ----------------
class GrantReq(BaseModel):
    username: str = ""
    device_id: str = ""
    seconds: int = 0        # 时长卡秒数
    fen: int = 0            # 余额分
    sub_days: int = 0


@app.post("/api/admin/grant")
def admin_grant(req: GrantReq, request: Request):
    if not config.ADMIN_TOKEN or request.headers.get("X-Admin-Token") != config.ADMIN_TOKEN:
        return JSONResponse({"code": 0, "msg": "forbidden"}, status_code=403)
    user = (db.user_by_username(req.username) if req.username
            else db.create_guest(req.device_id or uuid.uuid4().hex, 0, 0))
    if not user:
        return JSONResponse({"code": 0, "msg": "用户不存在"}, status_code=404)
    if req.seconds:
        db.add_credits(user["id"], req.seconds)
    if req.fen:
        db.add_money(user["id"], req.fen)
    if req.sub_days:
        db.extend_sub(user["id"], req.sub_days)
    return {"code": 1, **_entitlement(db.user_by_id(user["id"]))}


# ---------------- 翻译中转 WS ----------------
@app.websocket("/ws/translate")
async def ws_translate(ws: WebSocket, token: str = "", stream: str = "mate",
                       target: str = "zh", voice: str = "", audio: int = 0):
    await ws.accept()
    user = db.user_by_token(token)
    if not user:
        await ws.send_text('{"type":"error","code":"unauthorized","msg":"token 无效"}')
        await ws.close()
        return
    if stream not in ("mate", "self"):
        stream = "mate"
    if target not in ("zh", "en", "ja", "ko", "ru", "fr", "de", "es", "pt"):
        target = "zh"
    if voice not in config.VOICES:
        voice = config.VOICES[0] if config.VOICES else "Cherry"
    session = RelaySession(ws, user, stream, target, db,
                           voice=voice, want_audio=bool(audio))
    try:
        await session.run()
    except WebSocketDisconnect:
        pass
    finally:
        await session.close()


@app.on_event("shutdown")
async def _shutdown():
    await http.aclose()
