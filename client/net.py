"""网络层：REST API（httpx 同步）+ 中转 WS 客户端（websockets, asyncio 后台线程）。"""
import asyncio
import base64
import json
import logging
import threading

import httpx
import websockets

log = logging.getLogger("net")


# ---------------- REST ----------------
class Api:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self.token = ""
        self._cli = httpx.Client(timeout=15)

    def _h(self):
        return {"Authorization": f"Bearer {self.token}"}

    # ---- 认证 ----
    def auth_register(self, username, password, ref="", device_id="") -> dict:
        r = self._cli.post(f"{self.base}/api/auth/register", json={
            "username": username, "password": password,
            "ref": ref, "device_id": device_id})
        return r.json()

    def auth_login(self, username, password) -> dict:
        r = self._cli.post(f"{self.base}/api/auth/login",
                           json={"username": username, "password": password})
        return r.json()

    def auth_guest(self, device_id: str) -> dict:
        r = self._cli.post(f"{self.base}/api/auth/guest",
                           json={"device_id": device_id})
        return r.json()

    # ---- 信息 ----
    def meta(self) -> dict:
        r = self._cli.get(f"{self.base}/api/meta")
        return r.json()

    def me(self) -> dict:
        r = self._cli.get(f"{self.base}/api/me", headers=self._h())
        return r.json()

    def products(self) -> dict:
        r = self._cli.get(f"{self.base}/api/products")
        return r.json()

    def ref_stats(self) -> dict:
        r = self._cli.get(f"{self.base}/api/referral/stats", headers=self._h())
        return r.json()

    # ---- 支付 ----
    def pay_create(self, product_code: str, pay_type: str) -> dict:
        r = self._cli.post(f"{self.base}/api/pay/create",
                           json={"product_code": product_code, "pay_type": pay_type},
                           headers=self._h())
        return r.json()

    def pay_status(self, out_trade_no: str) -> dict:
        r = self._cli.get(f"{self.base}/api/pay/status",
                          params={"out_trade_no": out_trade_no}, headers=self._h())
        return r.json()


# ---------------- WS 中转客户端 ----------------
class RelayClient:
    """一条到计费服务器的翻译流连接。后台 asyncio 线程内运行。

    on_event(dict): seg / usage / error / closed / ready
    on_audio(bytes): 译文 TTS PCM(24k mono s16le)，仅 want_audio=True 时有
    """

    def __init__(self, http_base: str, token: str, stream: str, target: str,
                 on_event, on_state=None, voice: str = "", want_audio=False,
                 on_audio=None):
        ws_scheme = "wss" if http_base.startswith("https") else "ws"
        base = http_base.split("://", 1)[-1].rstrip("/")
        self.url = (f"{ws_scheme}://{base}/ws/translate"
                    f"?token={token}&stream={stream}&target={target}"
                    f"&voice={voice}&audio={1 if want_audio else 0}")
        self.stream = stream
        self.on_event = on_event          # fn(dict) 收到下行消息（非UI线程！）
        self.on_state = on_state or (lambda s: None)
        self.on_audio = on_audio          # fn(bytes)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws = None
        self._thread = None
        self._stop = threading.Event()
        self.ready = threading.Event()

    # ---- 生命周期 ----
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._thread_main, daemon=True,
                                        name=f"relay-{self.stream}")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def _thread_main(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run())
        finally:
            self._loop.close()

    async def _run(self):
        backoff = 1
        while not self._stop.is_set():
            self.on_state("connecting")
            try:
                async with websockets.connect(self.url, ping_interval=20,
                                              max_size=8 * 1024 * 1024) as ws:
                    self._ws = ws
                    backoff = 1
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        try:
                            ev = json.loads(raw)
                        except ValueError:
                            continue
                        t = ev.get("type")
                        if t == "ready":
                            self.ready.set()
                            self.on_state("ready")
                        elif t == "closed":
                            self.on_state("closed")
                        elif t == "audio" and self.on_audio:
                            try:
                                self.on_audio(base64.b64decode(ev["pcm"]))
                            except Exception:
                                pass
                            continue
                        self.on_event(ev)
            except Exception as e:
                log.info("[%s] ws 断开: %s", self.stream, e)
            self._ws = None
            self.ready.clear()
            if self._stop.is_set():
                break
            self.on_state("reconnecting")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15)

    # ---- 发送（线程安全） ----
    def _send(self, obj: dict):
        if self._loop and self._ws:
            try:
                asyncio.run_coroutine_threadsafe(self._ws.send(json.dumps(obj)),
                                                 self._loop)
            except RuntimeError:
                pass   # loop 正在关闭/重建

    def send_audio(self, pcm16k: bytes):
        if self.ready.is_set():
            self._send({"type": "audio",
                        "pcm": base64.b64encode(pcm16k).decode()})

    def commit(self):
        self._send({"type": "commit"})

    def finish(self):
        self._send({"type": "finish"})
