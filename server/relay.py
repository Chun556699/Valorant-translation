"""翻译中转会话：客户端 WS <-> DashScope Realtime WS，顺带按时长计费。

下行（服务器 -> 客户端）归一化消息：
  {"type":"ready"}
  {"type":"seg","seq":n,"speaker":"S1","src":"...","dst":"...",
   "src_final":bool,"dst_final":bool}
  {"type":"usage","used_seconds":x,"credit_seconds":y,"sub":bool}
  {"type":"error","code":"...","msg":"..."}
  {"type":"closed","reason":"..."}

上行（客户端 -> 服务器）：
  {"type":"audio","pcm":"<base64 16k mono s16le>"}
  {"type":"commit"}          # Manual 模式/松键时提交缓冲（可选，容错）
  {"type":"finish"}          # 结束会话
"""
import asyncio
import base64
import json
import logging
import time

import websockets
from starlette.websockets import WebSocketState

import config

log = logging.getLogger("relay")

FLUSH_EVERY_SECONDS = 15      # 每累计这么多可计费时长就扣一次点


def _deep_find_speaker(obj) -> str | None:
    """在事件 JSON 里尽力找说话人标识（3.8 speaker_detection 输出字段名以防变动，做宽松匹配）。"""
    keys = ("speaker", "speaker_id", "speaker_label", "speaker_idx", "spk", "spk_id")
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if v is not None and str(v) != "":
                return str(v)
        for v in obj.values():
            found = _deep_find_speaker(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _deep_find_speaker(v)
            if found:
                return found
    return None


def _session_config(stream: str, target: str) -> dict:
    """构造 session.update。3.8 用 audio.input.turn_detection=speaker_detection；
    self(按住说话) 流同样用 speaker_detection，松开时客户端补静音触发断句。"""
    session = {
        "output_modalities": ["text"],
        "audio": {
            "input": {
                "format": {"type": "pcm", "sample_rate": 16000},
                "turn_detection": {
                    "type": "speaker_detection",
                    "threshold": 0.5,
                    "silence_duration_ms": 1000,
                },
            },
            "output": {"format": {"type": "pcm", "sample_rate": 24000}},
        },
        "input_audio_transcription": {"model": "qwen3-asr-flash-realtime"},
        "translation": {"language": target},
    }
    session.update(config.DASH_SESSION_EXTRA)
    return {"type": "session.update", "session": session}


class RelaySession:
    def __init__(self, client_ws, user: dict, stream: str, target: str, db):
        self.ws = client_ws            # starlette WebSocket (已 accept)
        self.user = user
        self.stream = stream           # "mate" | "self"
        self.target = target           # "zh" | "en" ...
        self.db = db

        self.upstream = None           # websockets 连接或 None(mock)
        self.seq = 0
        self.bill_acc = 0.0            # 未结算的可计费秒数
        self.billed_total = 0
        self.sub = db.sub_active(user)
        self.user_id = user["id"]
        self.credit_seconds = user["credit_seconds"]

        # mate 流：用上游 speech_started/stopped 事件计量；self 流按输入量计量
        self._speech_t0: float | None = None
        self._saw_speech_event = False
        self._first_audio_at: float | None = None
        self._appended_seconds = 0.0
        self._closed = False
        self._flushed = False

    # ---------- 下行辅助 ----------
    async def _send(self, obj: dict):
        if self.ws.client_state == WebSocketState.CONNECTED:
            try:
                await self.ws.send_text(json.dumps(obj, ensure_ascii=False))
            except Exception:
                pass

    async def _err(self, code: str, msg: str):
        await self._send({"type": "error", "code": code, "msg": msg})

    # ---------- 计费 ----------
    def _acc(self, seconds: float):
        self.bill_acc += seconds

    async def _flush_usage(self, final=False):
        """把累计时长折算扣点。订阅用户只记账不扣点。"""
        if self._flushed:
            return
        if final:
            self._flushed = True   # 先置位，防止扣点->close() 递归
            charge = int(self.bill_acc + 0.999) if self.bill_acc > 0 else 0
        else:
            charge = int(self.bill_acc)
        if charge <= 0:
            return
        self.bill_acc = max(0.0, self.bill_acc - charge)
        self.billed_total += charge
        if not self.sub:
            self.db.add_credits(self.user_id, -charge)
            self.credit_seconds = max(0, self.credit_seconds - charge)
        self.db.log_usage(self.user_id, self.stream, charge)
        await self._send({
            "type": "usage",
            "used_seconds": self.billed_total,
            "credit_seconds": self.credit_seconds,
            "sub": self.sub,
        })
        if not self.sub and self.credit_seconds <= 0:
            await self._err("insufficient_balance", "点数已用完，请充值")
            await self.close()

    # ---------- 上游 ----------
    async def _connect_upstream(self):
        url = f"{config.DASHSCOPE_WS_URL}?model={config.TRANSLATE_MODEL}"
        self.upstream = await websockets.connect(
            url,
            additional_headers={"Authorization": f"Bearer {config.DASHSCOPE_API_KEY}"},
            ping_interval=20,
            max_size=8 * 1024 * 1024,
        )
        await self.upstream.send(json.dumps(_session_config(self.stream, self.target)))

    async def _up_send(self, obj: dict):
        if self.upstream:
            await self.upstream.send(json.dumps(obj))

    # ---------- 主循环 ----------
    async def run(self):
        if not self.sub and self.credit_seconds <= 0:
            await self._err("insufficient_balance", "无可用点数或订阅")
            await self._send({"type": "closed", "reason": "no_balance"})
            return
        try:
            if not config.MOCK_UPSTREAM:
                await self._connect_upstream()
        except Exception as e:
            log.exception("上游连接失败")
            await self._err("upstream_connect_failed", f"翻译服务连接失败: {e}")
            return

        await self._send({"type": "ready", "stream": self.stream,
                          "credit_seconds": self.credit_seconds, "sub": self.sub})
        tasks = [asyncio.create_task(t) for t in (
            self._pump_client_to_up(),
            self._pump_up_to_client(),
            self._meter_loop(),
        )]
        try:
            await asyncio.gather(*tasks)
        except Exception as e:
            log.info("会话结束: %s", e)
        finally:
            for t in tasks:
                t.cancel()
            await self.close()

    async def _pump_client_to_up(self):
        async for raw in self.ws.iter_text():
            if self._closed:
                break
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            t = msg.get("type")
            if t == "audio":
                pcm = base64.b64decode(msg.get("pcm", ""))
                if self._first_audio_at is None:
                    self._first_audio_at = time.monotonic()
                secs = len(pcm) / 2 / 16000
                self._appended_seconds += secs
                if self.stream == "self" or not self._saw_speech_event:
                    # self 流按住才发 = 发多少计多少；mate 流未收到 speech 事件时退化
                    self._acc(secs)
                await self._up_send({
                    "type": "input_audio_buffer.append",
                    "audio": msg["pcm"],
                })
            elif t == "commit":
                await self._up_send({"type": "input_audio_buffer.commit"})
            elif t == "finish":
                await self._up_send({"type": "session.finish"})
            elif t == "bye":
                break
        self._closed = True

    async def _pump_up_to_client(self):
        if config.MOCK_UPSTREAM:
            await self._mock_upstream_loop()
            return
        async for raw in self.upstream:
            if self._closed:
                break
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            await self._handle_up_event(ev)

    async def _handle_up_event(self, ev: dict):
        et = ev.get("type", "")

        if et == "input_audio_buffer.speech_started":
            self._saw_speech_event = True
            self._speech_t0 = time.monotonic()
            return
        if et == "input_audio_buffer.speech_stopped":
            if self._speech_t0 is not None:
                self._acc(time.monotonic() - self._speech_t0)
                self._speech_t0 = None
            return
        if et in ("session.created", "session.updated", "input_audio_buffer.committed",
                  "response.created"):
            return
        if et == "response.done":
            await self._send({"type": "turn_done",
                              "speaker": _deep_find_speaker(ev)})
            return

        if et == "error" or ev.get("code"):
            await self._err("upstream_error", json.dumps(ev, ensure_ascii=False)[:500])
            return

        # --- 原文识别 ---
        if et.startswith("conversation.item.input_audio_transcription"):
            speaker = _deep_find_speaker(ev)
            text = ev.get("transcript") or ev.get("text") or ""
            stash = ev.get("stash", "") or ""
            delta = ev.get("delta", "") or ""
            final = et.endswith(".completed")
            if delta and not text:
                # delta 事件：客户端自行累加
                await self._send({"type": "seg", "speaker": speaker,
                                  "src_delta": delta, "final": False})
                return
            await self._send({"type": "seg", "speaker": speaker,
                              "src": text + ("" if final else stash),
                              "src_final": final})
            return

        # --- 译文 ---
        if et.startswith("response.text.") or et.startswith("response.audio_transcript."):
            speaker = _deep_find_speaker(ev)
            if et.endswith(".delta"):
                await self._send({"type": "seg", "speaker": speaker,
                                  "dst_delta": ev.get("delta", ""), "final": False})
                return
            text = ev.get("text") or ev.get("transcript") or ""
            stash = ev.get("stash", "") or ""
            final = et.endswith(".done")
            await self._send({"type": "seg", "speaker": speaker,
                              "dst": text + ("" if final else stash),
                              "dst_final": final})
            return

        if et == "session.finished":
            await self._send({"type": "closed", "reason": "finished"})
            self._closed = True
            return

        # 其它事件（含 speaker_detection 专属事件）：尝试提取 speaker 透传，便于前端标记
        spk = _deep_find_speaker(ev)
        if spk:
            await self._send({"type": "speaker", "speaker": spk, "event": et})

    async def _meter_loop(self):
        while not self._closed:
            await asyncio.sleep(3)
            if self.bill_acc >= FLUSH_EVERY_SECONDS:
                await self._flush_usage()

    # ---------- Mock 上游：无 DashScope Key 时端到端调试用 ----------
    async def _mock_upstream_loop(self):
        mock_speakers = ["S1", "S2"]
        i = 0
        last = self._appended_seconds
        while not self._closed:
            await asyncio.sleep(0.8)
            if self._appended_seconds - last >= 1.0:
                last = self._appended_seconds
                spk = mock_speakers[i % 2] if self.stream == "mate" else "ME"
                i += 1
                self.seq += 1
                await self._send({"type": "seg", "speaker": spk,
                                  "src": f"[mock src {i}]", "src_final": True,
                                  "dst": f"[mock dst {i}]", "dst_final": True})
        # 保证循环退出
        return

    async def close(self):
        self._closed = True
        # 未闭合的语音窗口一并结算
        if self._speech_t0 is not None:
            self._acc(time.monotonic() - self._speech_t0)
            self._speech_t0 = None
        try:
            if self.upstream:
                await self._up_send({"type": "session.finish"})
                await asyncio.wait_for(self.upstream.wait_closed(), timeout=3)
        except Exception:
            pass
        try:
            if self.upstream:
                await self.upstream.close()
        except Exception:
            pass
        await self._flush_usage(final=True)
