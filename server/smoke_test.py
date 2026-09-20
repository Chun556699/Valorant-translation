"""冒烟测试：注册 -> WS 翻译(mock) -> 计费扣点 -> 模拟支付回调 -> 到账。"""
import asyncio
import base64
import hashlib
import json
import time

import httpx
import websockets

BASE = "http://127.0.0.1:8765"


def sign(params, key):
    items = sorted((k, str(v)) for k, v in params.items()
                   if k not in ("sign", "sign_type") and str(v) != "")
    raw = "&".join(f"{k}={v}" for k, v in items) + key
    return hashlib.md5(raw.encode()).hexdigest()


async def main():
    cli = httpx.Client(timeout=10)

    import uuid
    device_id = "smoke-" + uuid.uuid4().hex[:8]
    # 1. 注册
    r = cli.post(f"{BASE}/api/register", json={"device_id": device_id})
    d = r.json()
    assert d["code"] == 1, d
    token = d["token"]
    credits0 = d["credit_seconds"]
    print(f"[1] 注册 ok, 试用余额={credits0}s")

    h = {"Authorization": f"Bearer {token}"}

    # 2. WS 翻译（mock 上游），发 2.5s 假音频
    url = f"ws://127.0.0.1:8765/ws/translate?token={token}&stream=mate&target=zh"
    got_seg, got_ready = 0, False
    async with websockets.connect(url) as ws:
        pcm = b"\x01\x02" * 1600  # 100ms
        for _ in range(25):
            await ws.send(json.dumps({"type": "audio",
                                      "pcm": base64.b64encode(pcm).decode()}))
            await asyncio.sleep(0.05)
        t0 = time.time()
        while time.time() - t0 < 6:
            try:
                ev = json.loads(await asyncio.wait_for(ws.recv(), 6))
            except asyncio.TimeoutError:
                break
            if ev["type"] == "ready":
                got_ready = True
            elif ev["type"] == "seg":
                got_seg += 1
                print(f"[2] seg speaker={ev.get('speaker')} src={ev.get('src')} "
                      f"dst={ev.get('dst')}")
            elif ev["type"] == "usage":
                print(f"[2] usage {ev}")
        await ws.send(json.dumps({"type": "finish"}))
        await asyncio.sleep(0.3)
    assert got_ready and got_seg >= 1, "未收到 mock 翻译"
    print(f"[2] WS 中转 ok, 收到 {got_seg} 条翻译")

    # 3. 确认计费扣点（服务端异步结算，等它落库）
    await asyncio.sleep(2.0)
    me = cli.get(f"{BASE}/api/me", headers=h).json()
    print(f"[3] 余额 {credits0} -> {me['credit_seconds']}（应减少）")
    assert me["credit_seconds"] < credits0

    # 4. 下单（mapi 会失败->page_url 兜底，仍应 code=1）
    r = cli.post(f"{BASE}/api/pay/create",
                 json={"product_code": "credits_30min", "pay_type": "alipay"},
                 headers=h)
    d = r.json()
    assert d["code"] == 1 and d.get("page_url"), d
    otn = d["out_trade_no"]
    print(f"[4] 下单 ok out_trade_no={otn} page_url={d['page_url'][:80]}...")

    # 5. 模拟 z-pay 异步回调（正确签名）-> 验签 -> 发货
    notify = {
        "pid": "testpid", "trade_no": "ZPAY123456", "out_trade_no": otn,
        "type": "alipay", "name": "30分钟翻译点数", "money": "9.90",
        "param": "1", "trade_status": "TRADE_SUCCESS",
    }
    notify["sign"] = sign(notify, "testkey")
    notify["sign_type"] = "MD5"
    r = cli.get(f"{BASE}/api/pay/notify", params=notify)
    assert r.text == "success", r.text
    me = cli.get(f"{BASE}/api/me", headers=h).json()
    print(f"[5] 支付回调后余额={me['credit_seconds']}s（应 +1800）")
    assert me["credit_seconds"] >= credits0 + 1800 - 5

    # 6. 查单接口
    st = cli.get(f"{BASE}/api/pay/status",
                 params={"out_trade_no": otn}, headers=h).json()
    assert st["status"] == "paid"
    print("[6] 订单状态 paid")

    # 7. 坏签名回调必须被拒绝
    bad = dict(notify, sign="deadbeef" * 4)
    r = cli.get(f"{BASE}/api/pay/notify", params=bad)
    assert r.status_code == 400
    print("[7] 伪造回调被拒")

    # 8. 管理接口
    r = cli.post(f"{BASE}/api/admin/grant",
                 json={"device_id": device_id, "sub_days": 30},
                 headers={"X-Admin-Token": "admintest"})
    assert r.json().get("sub_active"), r.json()
    print("[8] admin 发放订阅")

    print("\n=== 全部冒烟测试通过 ===")


asyncio.run(main())
