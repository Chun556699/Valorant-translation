"""计费服务器入口：REST(注册/余额/下单/查单/回调) + WS(翻译中转)。

运行：uvicorn app:app --host 0.0.0.0 --port 8765
"""
import logging
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


# ---------------- 鉴权 ----------------
def _auth(req: Request):
    token = req.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    return db.user_by_token(token) if token else None


def _entitlement(user: dict) -> dict:
    return {
        "credit_seconds": user["credit_seconds"],
        "sub_expire_at": user["sub_expire_at"],
        "sub_active": db.sub_active(user),
    }


# ---------------- 账号 ----------------
class RegisterReq(BaseModel):
    device_id: str


@app.post("/api/register")
def register(req: RegisterReq):
    if not req.device_id or len(req.device_id) > 64:
        return JSONResponse({"code": 0, "msg": "device_id 非法"}, status_code=400)
    user = db.get_or_create_user(req.device_id, config.TRIAL_SECONDS)
    return {"code": 1, "token": user["token"], **_entitlement(user)}


@app.get("/api/me")
def me(req: Request):
    user = _auth(req)
    if not user:
        return JSONResponse({"code": 0, "msg": "未授权"}, status_code=401)
    return {"code": 1, **_entitlement(user)}


@app.get("/api/products")
def products():
    return {"code": 1, "products": [
        {"code": k, "name": v["name"], "money": v["money"], "kind": v["kind"],
         "seconds": v.get("seconds"), "days": v.get("days")}
        for k, v in config.PRODUCTS.items()
    ]}


# ---------------- 支付 ----------------
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
    """按商品类型发放权益（幂等：仅 pending->paid 时调用一次）。"""
    product = config.PRODUCTS.get(order["product_code"], {})
    if product.get("kind") == "credits":
        db.add_credits(order["user_id"], int(product["seconds"]))
    elif product.get("kind") == "sub":
        db.extend_sub(order["user_id"], int(product["days"]))


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


# ---------------- 管理（手动加时长/订阅，调试用） ----------------
class GrantReq(BaseModel):
    device_id: str
    seconds: int = 0
    sub_days: int = 0


@app.post("/api/admin/grant")
def admin_grant(req: GrantReq, request: Request):
    if not config.ADMIN_TOKEN or request.headers.get("X-Admin-Token") != config.ADMIN_TOKEN:
        return JSONResponse({"code": 0, "msg": "forbidden"}, status_code=403)
    user = db.get_or_create_user(req.device_id)
    if req.seconds:
        db.add_credits(user["id"], req.seconds)
    if req.sub_days:
        db.extend_sub(user["id"], req.sub_days)
    return {"code": 1, **_entitlement(db.get_or_create_user(req.device_id))}


# ---------------- 翻译中转 WS ----------------
@app.websocket("/ws/translate")
async def ws_translate(ws: WebSocket, token: str = "", stream: str = "mate",
                       target: str = "zh"):
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
    session = RelaySession(ws, user, stream, target, db)
    try:
        await session.run()
    except WebSocketDisconnect:
        pass
    finally:
        await session.close()


@app.on_event("shutdown")
async def _shutdown():
    await http.aclose()
