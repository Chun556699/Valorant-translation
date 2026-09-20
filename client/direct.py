"""直连模式：客户端直接连 DashScope Realtime，跳过计费服务器。

个人测试用——API Key 填在设置里、存本地，自己承担 token 费用。
接口与 net.RelayClient 完全一致（start/stop/send_audio/commit/finish/
on_event/on_state/on_audio/ready），可无缝替换。
"""
import asyncio
import base64
import json
import logging
import threading

import websockets

log = logging.getLogger("direct")

# 百炼业务空间专属域名（实时 WS 走 /api-ws/v1/realtime，与 REST /api/v1 对应）
DEFAULT_WS_URL = ("wss://llm-5gwthr14damb7ihz.cn-beijing.maas.aliyuncs.com"
                  "/api-ws/v1/realtime")
DEFAULT_MODEL = "qwen3.8-livetranslate-flash-realtime"


def _deep_find_speaker(obj):
    keys = ("speaker", "speaker_id", "speaker_label", "speaker_idx",
            "spk", "spk_id")
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if v is not None and str(v) != "":
                return str(v)
        for v in obj.values():
            f = _deep_find_speaker(v)
            if f:
                return f
    elif isinstance(obj, list):
        for v in obj:
            f = _deep_find_speaker(v)
            if f:
                return f
    return None


def _session_config(target: str, voice: str, want_audio: bool,
                    stream: str = "mate", phrases: dict | None = None) -> dict:
    # mate 流连续翻译多说话人 -> speaker_detection；
    # self 流按住说话 -> Manual 模式（turn_detection=null），松开时客户端
    # commit 作为整句提交，否则模型会在中途断句导致"只翻译了第一个分句"
    turn = (None if stream == "self" else {
        "type": "speaker_detection",
        "threshold": 0.5,
        "silence_duration_ms": 1000,
    })
    translation = {"language": target}
    if phrases:
        translation["corpus"] = {"phrases": phrases}   # 术语热词
    return {
        "type": "session.update",
        "session": {
            "output_modalities": ["text", "audio"] if want_audio else ["text"],
            "audio": {
                "input": {
                    "format": {"type": "pcm", "sample_rate": 16000},
                    "turn_detection": turn,
                },
                "output": {"format": {"type": "pcm", "sample_rate": 24000},
                           **({"voice": voice} if want_audio and voice else {})},
            },
            "input_audio_transcription": {"model": "qwen3-asr-flash-realtime"},
            "translation": translation,
        },
    }


class DashScopeDirect:
    """直连 DashScope 的翻译会话。与 RelayClient 接口一致。"""

    def __init__(self, api_key: str, ws_url: str, model: str,
                 target: str, on_event, on_state=None,
                 voice: str = "", want_audio: bool = False, on_audio=None,
                 stream: str = "mate", phrases: dict | None = None):
        self.url = f"{ws_url.rstrip('/')}?model={model}"
        self.api_key = api_key
        self.target = target
        self.voice = voice
        self.want_audio = want_audio
        self.stream = stream
        self.phrases = phrases or {}
        self.on_event = on_event
        self.on_state = on_state or (lambda s: None)
        self.on_audio = on_audio
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws = None
        self._thread = None
        self._stop = threading.Event()
        self.ready = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._thread_main, daemon=True,
                                        name=f"direct-{self.stream}")
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
                async with websockets.connect(
                        self.url,
                        additional_headers={
                            "Authorization": f"Bearer {self.api_key}"},
                        ping_interval=20, max_size=8 * 1024 * 1024) as ws:
                    self._ws = ws
                    await ws.send(json.dumps(
                        _session_config(self.target, self.voice,
                                        self.want_audio, self.stream,
                                        self.phrases)))
                    # 等 session.updated
                    async for raw in ws:
                        ev = json.loads(raw)
                        t = ev.get("type")
                        if t == "session.updated":
                            break
                        if t == "error" or ev.get("code"):
                            raise RuntimeError(json.dumps(ev, ensure_ascii=False))
                    self.ready.set()
                    self.on_state("ready")
                    self.on_event({"type": "ready", "stream": self.stream})
                    backoff = 1
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        try:
                            ev = json.loads(raw)
                        except ValueError:
                            continue
                        self._handle_event(ev)
            except Exception as e:
                log.info("[%s] 直连断开: %s", self.stream, e)
                self.on_event({"type": "error", "code": "upstream",
                               "msg": str(e)[:300]})
            self._ws = None
            self.ready.clear()
            if self._stop.is_set():
                break
            self.on_state("reconnecting")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15)

    # ---- 事件归一化（与服务器 relay 输出格式一致） ----
    def _handle_event(self, ev: dict):
        et = ev.get("type", "")
        if et in ("session.created", "session.updated",
                  "input_audio_buffer.committed", "response.created",
                  "input_audio_buffer.speech_started",
                  "input_audio_buffer.speech_stopped"):
            return
        if et == "response.done":
            self.on_event({"type": "turn_done",
                           "speaker": _deep_find_speaker(ev)})
            return
        if et == "error" or ev.get("code"):
            self.on_event({"type": "error", "code": "upstream",
                           "msg": json.dumps(ev, ensure_ascii=False)[:300]})
            return
        if et == "response.audio.delta" and self.on_audio:
            try:
                self.on_audio(base64.b64decode(ev.get("delta", "")))
            except Exception:
                pass
            return
        if et.startswith("conversation.item.input_audio_transcription"):
            speaker = _deep_find_speaker(ev)
            text = ev.get("transcript") or ev.get("text") or ""
            stash = ev.get("stash", "") or ""
            delta = ev.get("delta", "") or ""
            final = et.endswith(".completed")
            if delta and not text:
                self.on_event({"type": "seg", "speaker": speaker,
                               "src_delta": delta, "final": False})
            else:
                self.on_event({"type": "seg", "speaker": speaker,
                               "src": text + ("" if final else stash),
                               "src_final": final})
            return
        if et.startswith("response.text.") or \
                et.startswith("response.audio_transcript."):
            speaker = _deep_find_speaker(ev)
            if et.endswith(".delta"):
                self.on_event({"type": "seg", "speaker": speaker,
                               "dst_delta": ev.get("delta", ""),
                               "final": False})
                return
            text = ev.get("text") or ev.get("transcript") or ""
            stash = ev.get("stash", "") or ""
            final = et.endswith(".done")
            self.on_event({"type": "seg", "speaker": speaker,
                           "dst": text + ("" if final else stash),
                           "dst_final": final})
            return
        if et == "session.finished":
            self.on_event({"type": "closed", "reason": "finished"})
            return
        spk = _deep_find_speaker(ev)
        if spk:
            self.on_event({"type": "speaker", "speaker": spk, "event": et})

    # ---- 发送 ----
    def _send(self, obj: dict):
        if self._loop and self._ws:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._ws.send(json.dumps(obj)), self._loop)
            except RuntimeError:
                pass

    def send_audio(self, pcm16k: bytes):
        if self.ready.is_set():
            self._send({"type": "input_audio_buffer.append",
                        "audio": base64.b64encode(pcm16k).decode()})

    def commit(self):
        self._send({"type": "input_audio_buffer.commit"})

    def clear(self):
        self._send({"type": "input_audio_buffer.clear"})

    def finish(self):
        self._send({"type": "session.finish"})
