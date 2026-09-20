"""冒烟测试：认证 -> WS 翻译(mock+TTS) -> 多池计费 -> 支付回调 -> 返利 -> 查单。"""
import asyncio
import base64
import hashlib
import json
import time
import uuid

import httpx
import websockets

BASE = "http://127.0.0.1:8765"
KEY = "testkey"


def sign(params, key=KEY):
    items = sorted((k, str(v)) for k, v in params.items()
                   if k not in ("sign", "sign_type") and str(v) != "")
    raw = "&".join(f"{k}={v}" for k, v in items) + key
    return hashlib.md5(raw.encode()).hexdigest()


async def ws_run(token, stream="mate", want_audio=0, seconds=2.5):
    """连接翻译 WS，发 seconds 秒假音频，收集事件。"""
    url = (f"ws://127.0.0.1:8765/ws/translate?token={token}&stream={stream}"
           f"&target=zh&audio={want_audio}")
    evs = []
    async with websockets.connect(url) as ws:
        pcm = b"\x01\x02" * 1600
        n = int(seconds * 10)
        for _ in range(n):
            await ws.send(json.dumps({"type": "audio",
                                      "pcm": base64.b64encode(pcm).decode()}))
            await asyncio.sleep(0.04)
        t0 = time.time()
        while time.time() - t0 < 6:
            try:
                ev = json.loads(await asyncio.wait_for(ws.recv(), 6))
            except asyncio.TimeoutError:
                break
            evs.append(ev)
        await ws.send(json.dumps({"type": "finish"}))
        await asyncio.sleep(0.3)
    return evs


async def main():
    cli = httpx.Client(timeout=10)
    uid = uuid.uuid4().hex[:8]

    # 1. 注册 + 登录 + 游客
    r = cli.post(f"{BASE}/api/auth/register",
                 json={"username": f"user_{uid}", "password": "pass123"})
    d = r.json()
    assert d["code"] == 1 and d["trial_seconds"] == 300, d
    token_a, ref_a = d["token"], d["ref_code"]
    print(f"[1] 注册 ok user_{uid} ref={ref_a} trial={d['trial_seconds']}s")

    r = cli.post(f"{BASE}/api/auth/login",
                 json={"username": f"user_{uid}", "password": "wrong"})
    assert r.status_code == 401
    r = cli.post(f"{BASE}/api/auth/login",
                 json={"username": f"user_{uid}", "password": "pass123"})
    d = r.json()
    assert d["code"] == 1
    token_a = d["token"]
    print("[1b] 登录 ok（错误密码被拒）")

    r = cli.post(f"{BASE}/api/auth/guest", json={"device_id": f"guest-{uid}"})
    assert r.json()["code"] == 1
    print("[1c] 游客 ok")

    h = {"Authorization": f"Bearer {token_a}"}
    meta = cli.get(f"{BASE}/api/meta").json()
    assert meta["code"] == 1 and meta["voices"], meta
    print(f"[1d] meta ok voices={meta['voices']}")

    # 2. WS 翻译 mock（want_audio=1 应收到 audio 事件）
    evs = await ws_run(token_a, "mate", want_audio=1, seconds=2.5)
    types = [e["type"] for e in evs]
    segs = [e for e in evs if e["type"] == "seg"]
    auds = [e for e in evs if e["type"] == "audio"]
    assert "ready" in types and len(segs) >= 1 and len(auds) >= 1, types
    print(f"[2] WS ok segs={len(segs)} audio_chunks={len(auds)} "
          f"speakers={set(s.get('speaker') for s in segs)}")

    # 3. 计费：免费额度先扣（trial 减少，credit/money 不变）
    await asyncio.sleep(2.0)
    me = cli.get(f"{BASE}/api/me", headers=h).json()
    print(f"[3] trial {300} -> {me['trial_seconds']}（应减少，其余不动）")
    assert me["trial_seconds"] < 300
    assert me["credit_seconds"] == 0 and me["money_cents"] == 0

    # 4. 充余额 CNY10 -> money_cents +1000
    r = cli.post(f"{BASE}/api/pay/create",
                 json={"product_code": "money_10", "pay_type": "alipay"},
                 headers=h)
    d = r.json()
    assert d["code"] == 1 and d.get("page_url"), d
    otn = d["out_trade_no"]
    notify = {"pid": "testpid", "trade_no": "ZP1", "out_trade_no": otn,
              "type": "alipay", "name": "余额充值 CNY10", "money": "10.00",
              "param": "1", "trade_status": "TRADE_SUCCESS"}
    notify["sign"] = sign(notify)
    notify["sign_type"] = "MD5"
    r = cli.get(f"{BASE}/api/pay/notify", params=notify)
    assert r.text == "success", r.text
    me = cli.get(f"{BASE}/api/me", headers=h).json()
    assert me["money_cents"] == 1000, me
    print(f"[4] 余额充值到账 CNY{me['money_yuan']} ≈{me['money_minutes']}分钟")

    # 5. 推广返利：B 用 A 的推广码注册并充值 -> A 余额 +10%
    r = cli.post(f"{BASE}/api/auth/register",
                 json={"username": f"userb_{uid}", "password": "pass123",
                       "ref": ref_a})
    tok_b = r.json()["token"]
    hb = {"Authorization": f"Bearer {tok_b}"}
    r = cli.post(f"{BASE}/api/pay/create",
                 json={"product_code": "money_10", "pay_type": "wxpay"},
                 headers=hb)
    otn_b = r.json()["out_trade_no"]
    notify = {"pid": "testpid", "trade_no": "ZP2", "out_trade_no": otn_b,
              "type": "wxpay", "name": "余额充值 CNY10", "money": "10.00",
              "trade_status": "TRADE_SUCCESS"}
    notify["sign"] = sign(notify)
    notify["sign_type"] = "MD5"
    r = cli.get(f"{BASE}/api/pay/notify", params=notify)
    assert r.text == "success"
    me_a = cli.get(f"{BASE}/api/me", headers=h).json()
    stats = cli.get(f"{BASE}/api/referral/stats", headers=h).json()
    print(f"[5] A 余额 {me_a['money_cents']}分(=1000+100返利) "
          f"invited={stats['invited']} earned={stats['earned_yuan']}")
    assert me_a["money_cents"] == 1100 and stats["invited"] == 1

    # 6. 订阅商品
    r = cli.post(f"{BASE}/api/pay/create",
                 json={"product_code": "sub_month", "pay_type": "alipay"},
                 headers=hb)
    otn_c = r.json()["out_trade_no"]
    notify = {"pid": "testpid", "trade_no": "ZP3", "out_trade_no": otn_c,
              "type": "alipay", "name": "包月订阅（不限量）", "money": "59.00",
              "trade_status": "TRADE_SUCCESS"}
    notify["sign"] = sign(notify)
    notify["sign_type"] = "MD5"
    cli.get(f"{BASE}/api/pay/notify", params=notify)
    me_b = cli.get(f"{BASE}/api/me", headers=hb).json()
    assert me_b["sub_active"], me_b
    print("[6] B 订阅到账")

    # 7. 伪造签名被拒 + 查单
    bad = dict(notify, sign="deadbeef" * 4)
    r = cli.get(f"{BASE}/api/pay/notify", params=bad)
    assert r.status_code == 400
    st = cli.get(f"{BASE}/api/pay/status",
                 params={"out_trade_no": otn_c}, headers=hb).json()
    assert st["status"] == "paid"
    print("[7] 伪造回调被拒 + 查单 paid")

    # 8. admin
    r = cli.post(f"{BASE}/api/admin/grant",
                 json={"username": f"user_{uid}", "fen": 500, "sub_days": 7},
                 headers={"X-Admin-Token": "admintest"})
    d = r.json()
    # A 余额 = 1000(自充) + 100(B充10返10%) + 590(B订阅59返10%) + 500(admin) = 2190
    assert d["code"] == 1 and d["money_cents"] == 2190 and d["sub_active"], d
    print("[8] admin 发放 ok")

    print("\n=== 全部冒烟测试通过 ===")


asyncio.run(main())
