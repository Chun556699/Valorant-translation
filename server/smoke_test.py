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
    d = r.json()
    assert d["code"] == 1
    tok_g1 = d["token"]
    # 同设备再次请求应轮换 token（防凭 device_id 冒领旧 token）
    r = cli.post(f"{BASE}/api/auth/guest", json={"device_id": f"guest-{uid}"})
    d = r.json()
    assert d["code"] == 1 and d["token"] != tok_g1
    assert cli.get(f"{BASE}/api/me",
                   headers={"Authorization": f"Bearer {tok_g1}"}).status_code == 401
    print("[1c] 游客 ok（token 已轮换，旧 token 失效）")

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

    # 2b. WS 用 Authorization 头鉴权（优先于 ?token=）
    url = f"ws://127.0.0.1:8765/ws/translate?stream=self&target=zh"
    async with websockets.connect(
            url, additional_headers={"Authorization": f"Bearer {token_a}"}) as ws:
        ev = json.loads(await asyncio.wait_for(ws.recv(), 6))
        assert ev["type"] == "ready", ev
        await ws.send(json.dumps({"type": "finish"}))
    print("[2b] WS Authorization 头鉴权 ok")

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

    # 9. 并发双花：同一账号两个 WS 会话，合计扣费不得超过池子总量
    r = cli.post(f"{BASE}/api/auth/register",
                 json={"username": f"duo_{uid}", "password": "pass123"})
    tok_c = r.json()["token"]
    r = cli.post(f"{BASE}/api/admin/grant",
                 json={"username": f"duo_{uid}", "trial_seconds": 10},
                 headers={"X-Admin-Token": "admintest"})
    d = r.json()
    assert d["code"] == 1 and d["trial_seconds"] == 10, d
    evs1, evs2 = await asyncio.gather(ws_run(tok_c, seconds=8),
                                      ws_run(tok_c, seconds=8))
    await asyncio.sleep(1.0)
    me_c = cli.get(f"{BASE}/api/me",
                   headers={"Authorization": f"Bearer {tok_c}"}).json()
    assert me_c["trial_seconds"] >= 0 and me_c["credit_seconds"] >= 0 \
        and me_c["money_cents"] >= 0, me_c
    used = 10 - me_c["trial_seconds"]
    assert used > 0, f"未计费（WS 推流没生效？）me={me_c}"
    assert used <= 10, f"双花！池子 10s 实际扣了 {used}s"
    print(f"[9] 并发双花防护 ok：两会话各推 8s，池子 10s 实际扣 {used}s "
          f"(<=10)，剩余 {me_c['trial_seconds']}s")

    # 10. 限流：连续猛打登录接口应触发 429
    got_429 = False
    for _ in range(15):
        r = cli.post(f"{BASE}/api/auth/login",
                     json={"username": f"rl_{uid}", "password": "xxxxxx"})
        if r.status_code == 429:
            got_429 = True
            break
    assert got_429, "限流未触发 429"
    print("[10] 限流 ok（登录接口连续请求触发 429）")

    # 11. 超速推流：不间断猛发音频(远超 4x 实时)应被 audio_rate_abuse 断开
    r = cli.post(f"{BASE}/api/auth/register",
                 json={"username": f"fast_{uid}", "password": "pass123"})
    tok_d = r.json()["token"]
    url = f"ws://127.0.0.1:8765/ws/translate?token={tok_d}&stream=mate&target=zh"
    abuse = None
    async with websockets.connect(url) as ws:
        pcm = b"\x01\x02" * 16000   # 0.5s/帧
        async def blast():
            try:
                for _ in range(400):
                    await ws.send(json.dumps(
                        {"type": "audio", "pcm": base64.b64encode(pcm).decode()}))
                    await asyncio.sleep(0.01)
            except websockets.ConnectionClosed:
                pass   # 服务端断开即停
        async def watch():
            nonlocal abuse
            while True:
                try:
                    ev = json.loads(await asyncio.wait_for(ws.recv(), 20))
                except asyncio.TimeoutError:
                    break
                except websockets.ConnectionClosed:
                    break
                if ev.get("code") == "audio_rate_abuse":
                    abuse = ev
                    break
        await asyncio.gather(blast(), watch())
    assert abuse, "超速推流未被断开"
    print("[11] 超速推流防护 ok（收到 audio_rate_abuse）")

    # 12. 过期免费额度不扣费且自动清零：trial_days=0 -> 立即过期的池子，
    #     余额正常计费（不得优先消耗过期 trial 白嫖）
    r = cli.post(f"{BASE}/api/auth/register",
                 json={"username": f"exp_{uid}", "password": "pass123"})
    tok_e = r.json()["token"]
    he = {"Authorization": f"Bearer {tok_e}"}
    r = cli.post(f"{BASE}/api/admin/grant",
                 json={"username": f"exp_{uid}", "trial_seconds": 10,
                       "trial_days": 0, "fen": 1000},
                 headers={"X-Admin-Token": "admintest"})
    d = r.json()
    assert d["code"] == 1 and d["trial_seconds"] == 10 \
        and d["money_cents"] == 1000, d
    await ws_run(tok_e, seconds=3)
    await asyncio.sleep(1.0)
    me_e = cli.get(f"{BASE}/api/me", headers=he).json()
    print(f"[12] 过期免费额度 ok：money 1000 -> {me_e['money_cents']}（已扣），"
          f"trial 10 -> {me_e['trial_seconds']}（未消耗且已清零）")
    assert me_e["money_cents"] < 1000, f"余额未计费（钱白送？）me={me_e}"
    assert me_e["trial_seconds"] == 0, f"过期免费额度未清零 me={me_e}"

    # 13. 回调金额格式差异：订单存 "10.00"，回调送 "10.0" 应正常入账
    r = cli.post(f"{BASE}/api/auth/register",
                 json={"username": f"fmt_{uid}", "password": "pass123"})
    tok_f = r.json()["token"]
    hf = {"Authorization": f"Bearer {tok_f}"}
    r = cli.post(f"{BASE}/api/pay/create",
                 json={"product_code": "money_10", "pay_type": "alipay"},
                 headers=hf)
    otn_f = r.json()["out_trade_no"]
    notify = {"pid": "testpid", "trade_no": "ZP4", "out_trade_no": otn_f,
              "type": "alipay", "name": "余额充值 CNY10", "money": "10.0",
              "trade_status": "TRADE_SUCCESS"}
    notify["sign"] = sign(notify)
    notify["sign_type"] = "MD5"
    r = cli.get(f"{BASE}/api/pay/notify", params=notify)
    assert r.text == "success", r.text
    me_f = cli.get(f"{BASE}/api/me", headers=hf).json()
    print(f"[13] 金额格式兼容 ok：'10.0' vs '10.00'，到账 {me_f['money_cents']} 分")
    assert me_f["money_cents"] == 1000, me_f

    # 14. 并发同 device_id 注册游客：唯一索引兜底，不得产生重复游客账号
    dev = f"dup-{uid}"
    async with httpx.AsyncClient(timeout=10) as ac:
        rs = await asyncio.gather(*[
            ac.post(f"{BASE}/api/auth/guest", json={"device_id": dev})
            for _ in range(8)])
    codes = [r.json()["code"] for r in rs]
    refs = [r.json()["ref_code"] for r in rs]   # ref_code 每账号唯一 -> 指纹
    assert all(c == 1 for c in codes), [r.text for r in rs]
    assert len(set(refs)) == 1, f"并发产生了 {len(set(refs))} 个游客账号"
    print("[14] 并发游客去重 ok（8 并发同 device_id -> 同一账号）")

    # 15. 游客新建配额：同 IP 每 24h 最多 GUEST_GRANTS_PER_IP_PER_DAY 个新游客
    cap = 10   # 服务端默认 GUEST_GRANTS_PER_IP_PER_DAY=10（未设环境变量）
    ok = 0
    last = None
    for i in range(cap + 2):
        r = cli.post(f"{BASE}/api/auth/guest",
                     json={"device_id": f"cap-{uid}-{i}"})
        last = r
        if r.status_code == 200:
            ok += 1
    assert last.status_code == 429, f"配额未触发 429：{last.text}"
    assert "guest" in last.json()["msg"], last.text
    assert 0 < ok <= cap, f"成功新建 {ok} 个，超出配额 {cap}"
    print(f"[15] 游客新建配额 ok：成功 {ok} 个后触发 429（上限 {cap}/24h/IP）")

    print("\n=== 全部冒烟测试通过 ===")


asyncio.run(main())
