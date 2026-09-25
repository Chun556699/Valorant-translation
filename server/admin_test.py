"""管理后台 API 冒烟测试（需先以 ADMIN_TOKEN=admintest MOCK_UPSTREAM=1 启动服务）。"""
import asyncio
import json
import uuid

import httpx
import websockets

BASE = "http://127.0.0.1:8765"


async def main():
    cli = httpx.Client(timeout=10)
    uid = uuid.uuid4().hex[:8]

    # 1. 登录：错误令牌 401，正确令牌拿会话；未带会话访问 401
    assert cli.post(f"{BASE}/api/admin/login", json={"token": "nope"}).status_code == 401
    assert cli.get(f"{BASE}/api/admin/stats").status_code == 401
    r = cli.post(f"{BASE}/api/admin/login", json={"token": "admintest"}).json()
    h = {"X-Admin-Session": r["session"]}
    print("[1] 登录 ok")

    # 2. 静态页 + 安全头
    r = cli.get(f"{BASE}/admin/")
    assert r.status_code == 200 and "VTranslate" in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    print("[2] /admin 静态页 + CSP ok")

    # 3. 统计
    s = cli.get(f"{BASE}/api/admin/stats", headers=h).json()
    assert s["code"] == 1 and len(s["series"]["labels"]) == 14, s
    print(f"[3] stats ok users={s['users_total']} terms_zh={s['system']['terms_zh']}")

    # 4. 用户：注册 -> 搜索 -> 详情 -> 调整 -> 备注
    tok = cli.post(f"{BASE}/api/auth/register",
                   json={"username": f"adm_{uid}", "password": "pass123"}).json()["token"]
    lst = cli.get(f"{BASE}/api/admin/users", params={"q": f"adm_{uid}"}, headers=h).json()
    assert lst["total"] == 1
    u = lst["items"][0]
    assert "token" not in u and "password_hash" not in u
    r = cli.post(f"{BASE}/api/admin/users/{u['id']}/adjust",
                 json={"fen": 500, "seconds": 600, "note": "vip"}, headers=h).json()
    assert r["entitlement"]["money_cents"] == 500 and r["entitlement"]["credit_seconds"] == 600
    d = cli.get(f"{BASE}/api/admin/users/{u['id']}", headers=h).json()
    assert d["user"]["note"] == "vip" and "token" not in d["user"]
    r = cli.post(f"{BASE}/api/admin/users/{u['id']}/adjust", json={"fen": -800}, headers=h).json()
    assert r["entitlement"]["money_cents"] == 0
    print("[4] 用户检索/调整/备注 ok")

    # 5. 实时会话 + 封禁踢线
    url = f"ws://127.0.0.1:8765/ws/translate?stream=mate&target=zh"
    async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {tok}"}) as ws:
        assert json.loads(await ws.recv())["type"] == "ready"
        async with httpx.AsyncClient(timeout=10) as ac:   # 持有 WS 时不能用同步请求阻塞事件循环
            live = (await ac.get(f"{BASE}/api/admin/sessions", headers=h)).json()["items"]
            assert any(x["user_id"] == u["id"] for x in live), live
            r = (await ac.post(f"{BASE}/api/admin/users/{u['id']}/ban",
                               json={"banned": True}, headers=h)).json()
        assert r["kicked"] == 1, r
        codes = []
        try:
            while True:
                codes.append(json.loads(await asyncio.wait_for(ws.recv(), 5)).get("code"))
        except (websockets.ConnectionClosed, asyncio.TimeoutError):
            pass
        assert "banned" in codes, codes
    assert cli.get(f"{BASE}/api/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 401
    r = cli.post(f"{BASE}/api/auth/login", json={"username": f"adm_{uid}", "password": "pass123"})
    assert r.status_code == 403
    cli.post(f"{BASE}/api/admin/users/{u['id']}/ban", json={"banned": False}, headers=h)
    r = cli.post(f"{BASE}/api/auth/login", json={"username": f"adm_{uid}", "password": "pass123"})
    assert r.status_code == 200
    print("[5] 实时会话/封禁踢线/解封 ok")

    # 6. 重置密码
    assert cli.post(f"{BASE}/api/admin/users/{u['id']}/password",
                    json={"password": "newpass1"}, headers=h).json()["code"] == 1
    assert cli.post(f"{BASE}/api/auth/login",
                    json={"username": f"adm_{uid}", "password": "newpass1"}).status_code == 200
    print("[6] 重置密码 ok")

    # 7. 订单：下单 -> 手动补单 -> 重复补单被拒 -> 关闭订单
    tok = cli.post(f"{BASE}/api/auth/login",
                   json={"username": f"adm_{uid}", "password": "newpass1"}).json()["token"]
    hu = {"Authorization": f"Bearer {tok}"}
    otn = cli.post(f"{BASE}/api/pay/create", json={"product_code": "card_60min"},
                   headers=hu).json()["out_trade_no"]
    assert cli.post(f"{BASE}/api/admin/orders/{otn}/fulfill", headers=h).json()["code"] == 1
    assert cli.post(f"{BASE}/api/admin/orders/{otn}/fulfill", headers=h).status_code == 400
    me = cli.get(f"{BASE}/api/me", headers=hu).json()
    assert me["credit_seconds"] == 600 + 3600, me
    otn2 = cli.post(f"{BASE}/api/pay/create", json={"product_code": "money_10"},
                    headers=hu).json()["out_trade_no"]
    assert cli.post(f"{BASE}/api/admin/orders/{otn2}/close", headers=h).json()["code"] == 1
    o = cli.get(f"{BASE}/api/admin/orders", params={"q": otn2}, headers=h).json()
    assert o["items"][0]["status"] == "closed"
    print("[7] 手动补单/防重复/关闭订单 ok")

    # 8. 热词：新增 -> 查询 -> 删除 -> 恢复内置
    r = cli.post(f"{BASE}/api/admin/terms",
                 json={"target": "zh", "pairs": {f"term_{uid}": "测试词"}}, headers=h).json()
    assert r["code"] == 1
    items = cli.get(f"{BASE}/api/admin/terms", params={"target": "zh", "q": f"term_{uid}"},
                    headers=h).json()["items"]
    assert len(items) == 1
    cli.post(f"{BASE}/api/admin/terms/delete", json={"ids": [items[0]["id"]]}, headers=h)
    assert not cli.get(f"{BASE}/api/admin/terms", params={"q": f"term_{uid}"},
                       headers=h).json()["items"]
    assert cli.post(f"{BASE}/api/admin/terms", json={"target": "xx", "pairs": {"a": "b"}},
                    headers=h).status_code == 400
    r = cli.post(f"{BASE}/api/admin/terms/reset", json={"target": "en"}, headers=h).json()
    assert r["count"] > 200, r
    print("[8] 热词增删查/恢复内置 ok")

    # 9. 运行时参数：修改公告立即生效；非法值整体回滚
    r = cli.put(f"{BASE}/api/admin/settings",
                json={"announcement": f"hello {uid}", "mate_silence_ms": 700}, headers=h).json()
    assert r["code"] == 1
    assert cli.get(f"{BASE}/api/meta").json()["announcement"] == f"hello {uid}"
    r = cli.put(f"{BASE}/api/admin/settings",
                json={"announcement": "bad", "mate_silence_ms": 5}, headers=h)
    assert r.status_code == 400
    assert cli.get(f"{BASE}/api/meta").json()["announcement"] == f"hello {uid}"
    cli.put(f"{BASE}/api/admin/settings", json={"announcement": ""}, headers=h)
    print("[9] 运行时参数/回滚 ok")

    # 10. 登出后会话失效
    cli.post(f"{BASE}/api/admin/logout", headers=h)
    assert cli.get(f"{BASE}/api/admin/stats", headers=h).status_code == 401
    print("[10] 登出 ok")
    print("\n=== 管理后台测试通过 ===")


asyncio.run(main())
