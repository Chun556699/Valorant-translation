"""翻译中转会话：客户端 WS <-> DashScope Realtime WS，顺带按时长计费。

下行（服务器 -> 客户端）归一化消息：
  {"type":"ready"}
  {"type":"seg","id":"item_x","speaker":"0","src":"...","dst":"...",
   "src_delta":"...","dst_delta":"...","src_final":bool,"dst_final":bool}
     id = 本段原文识别 item_id（原文/译文靠它归到同一气泡，多人同时说话不串行）
  {"type":"speaker","id":"item_x","speaker":"0"}   # 新一段语音开始
  {"type":"turn_done","id":"item_x","speaker":"0"}
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
from collections import OrderedDict

import websockets
from starlette.websockets import WebSocketState

import config

log = logging.getLogger("relay")

FLUSH_EVERY_SECONDS = 15      # 每累计这么多可计费时长就扣一次点
MAX_AUDIO_B64_LEN = 350_000   # 单条音频 base64 上限（约 256KB 原始 PCM）
MAX_AUDIO_RATE = 4.0          # 允许的最大推流倍速（相对实时）
RATE_WINDOW_SECONDS = 10.0    # 超速判定窗口：持续超过该时长才断开
HANDSHAKE_TIMEOUT = 10.0      # 等待 session.updated 的超时
MAX_PHRASES = 1000            # 官方建议热词不超过 1000 条

# 在线会话登记表（管理后台"实时会话"用）：id(session) -> session
ACTIVE: dict[int, "RelaySession"] = {}

# 热词提供者：app 启动时注入（读库带缓存），缺省为空表
terms_provider = lambda target: {}  # noqa: E731


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


def _session_config(stream: str, target: str, voice: str, want_audio: bool,
                    phrases: dict | None = None, manual: bool = True) -> dict:
    """mate 流 speaker_detection（连续多人语音自动断句+分说话人）；
    self 流 manual——按住说话松开 commit 整句翻译，防止中途断句。
    manual=False 为兜底档（端点拒绝 null 时退回自动断句）。
    corpus.phrases 注入游戏术语热词。"""
    turn = (None if stream == "self" and manual else {
        "type": "speaker_detection",
        "threshold": 0.5,
        "silence_duration_ms": config.MATE_SILENCE_MS,
    })
    translation = {"language": target}
    if phrases:
        translation["corpus"] = {"phrases": dict(list(phrases.items())[:MAX_PHRASES])}
    session = {
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
    }
    session.update(config.DASH_SESSION_EXTRA)
    return {"type": "session.update", "session": session}


class EventNormalizer:
    """上游事件 -> 归一化 seg，按 item 关联原文/译文/说话人。

    qwen3.8 协议：speaker_id 只出现在 input_audio_buffer.speech_started（带 item_id）；
    原文识别事件带同一 item_id；译文 item 通过 conversation.item.created 的
    previous_item_id 指回原文 item。不做关联时多人同时说话会串进同一个气泡。"""
    MAX_ITEMS = 256
    SRC_PREFIX = "conversation.item.input_audio_transcription"
    DST_PREFIXES = ("response.text.", "response.audio_transcript.")

    def __init__(self):
        self.spk_by_item: OrderedDict[str, str] = OrderedDict()
        self.src_of: OrderedDict[str, str] = OrderedDict()   # 译文 item -> 原文 item
        self.last_src: str | None = None
        self.last_spk: str | None = None

    def _put(self, d: OrderedDict, k, v):
        d[k] = v
        d.move_to_end(k)
        while len(d) > self.MAX_ITEMS:
            d.popitem(last=False)

    def _spk(self, item: str | None, ev: dict) -> str | None:
        spk = self.spk_by_item.get(item) if item else None
        if spk is None:
            spk = _deep_find_speaker(ev)
            if spk is not None and item:
                self._put(self.spk_by_item, item, spk)
        if spk is None and item and item == self.last_src:
            spk = self.last_spk
        return spk

    def _src_for_dst(self, tid: str | None) -> str | None:
        if not tid:
            return self.last_src
        sid = self.src_of.get(tid)
        if sid is None:
            # 未收到 item.created 关联：按"最近一段语音"兜底，并记住避免后续漂移
            sid = self.last_src or tid
            self._put(self.src_of, tid, sid)
        return sid

    def feed(self, ev: dict) -> list[dict]:
        et = ev.get("type", "")
        if et == "input_audio_buffer.speech_started":
            item = ev.get("item_id")
            spk = ev.get("speaker_id")
            spk = str(spk) if spk is not None and str(spk) != "" else _deep_find_speaker(ev)
            if item:
                self.last_src = item
                if spk is not None:
                    self._put(self.spk_by_item, item, spk)
            if spk is not None:
                self.last_spk = spk
            return [{"type": "speaker", "id": item, "speaker": spk}]
        if et == "conversation.item.created":
            item = ev.get("item") or {}
            iid, prev = item.get("id"), ev.get("previous_item_id")
            if iid and item.get("role") == "assistant" and prev:
                self._put(self.src_of, iid, prev)
            elif iid and item.get("role") == "user":
                self.last_src = iid     # Manual 模式 commit 产生的原文 item
            return []

        if et.startswith(self.SRC_PREFIX):
            if et.endswith(".failed"):
                return []
            sid = ev.get("item_id") or self.last_src
            base = {"type": "seg", "id": sid, "speaker": self._spk(sid, ev)}
            delta, text = ev.get("delta") or "", ev.get("transcript") or ev.get("text") or ""
            if et.endswith(".completed"):
                return [{**base, "src": text, "src_final": True}]
            if delta and not text:
                return [{**base, "src_delta": delta}]
            return [{**base, "src": text + (ev.get("stash") or "")}]

        if et.startswith(self.DST_PREFIXES):
            sid = self._src_for_dst(ev.get("item_id"))
            base = {"type": "seg", "id": sid, "speaker": self._spk(sid, ev)}
            if et.endswith(".delta"):
                d = ev.get("delta") or ""
                return [{**base, "dst_delta": d}] if d else []
            text = ev.get("text") or ev.get("transcript") or ""
            if et.endswith(".done"):
                return [{**base, "dst": text, "dst_final": True}]
            return [{**base, "dst": text + (ev.get("stash") or "")}]

        if et == "response.done":
            out = []
            for it in ((ev.get("response") or {}).get("output") or []):
                sid = self._src_for_dst(it.get("id"))
                out.append({"type": "turn_done", "id": sid, "speaker": self._spk(sid, ev)})
            return out or [{"type": "turn_done", "id": self.last_src,
                            "speaker": _deep_find_speaker(ev)}]
        return []


class RelaySession:
    def __init__(self, client_ws, user: dict, stream: str, target: str, db,
                 voice: str = "", want_audio: bool = False):
        self.ws = client_ws            # starlette WebSocket (已 accept)
        self.user = user
        self.stream = stream           # "mate" | "self"
        self.target = target           # "zh" | "en" ...
        self.voice = voice
        self.want_audio = want_audio
        self.db = db

        self.upstream = None           # websockets 连接或 None(mock)
        self.seq = 0
        self.bill_acc = 0.0            # 未结算的可计费秒数
        self.billed_total = 0
        self.sub = db.sub_active(user)
        self.user_id = user["id"]
        # 权益池快照（仅用于下发 usage 展示；扣费/校验一律走库，防同账号多会话双花）
        self.trial_seconds = float(user["trial_seconds"])
        self.credit_seconds = float(user["credit_seconds"])
        self.money_cents = float(user["money_cents"])
        self._money_frac = 0.0         # 余额计费的零碎分累计

        # mate 流：用上游 speech_started/stopped 事件计量；self 流按输入量计量
        self._speech_t0: float | None = None
        self._saw_speech_event = False
        self._first_audio_at: float | None = None
        self._appended_seconds = 0.0
        self._audio_chunks = 0
        self._closed = False
        self._flushed = False
        self.norm = EventNormalizer()
        self.started_at = int(time.time())
        self.client_ip = ""
        self.degraded: list[str] = []   # 被上游拒绝而降级掉的能力

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

    def _has_entitlement(self) -> bool:
        """权益校验直接读库（单条 SELECT，便宜），不信快照——快照可能被并发会话消耗。"""
        user = self.db.user_by_id(self.user_id)
        if not user:
            return False
        self.user = user
        self.sub = self.db.sub_active(user)
        return (self.sub
                or self.db.trial_active(user)
                or user["credit_seconds"] > 0
                or user["money_cents"] > 0)

    def _charge(self, seconds: int) -> str:
        """按优先级扣费：订阅 > 免费额度 > 时长卡 > 余额。返回使用的池子。
        扣减走 db.deduct_* 原子操作，实际扣到多少算多少，杜绝并发双花。"""
        if self.sub:
            return "free"
        s = seconds
        used = []
        if s > 0:
            t = self.db.deduct_trial(self.user_id, s)
            if t:
                used.append(("trial", t, 0))
                s -= t
        if s > 0:
            t = self.db.deduct_credits(self.user_id, s)
            if t:
                used.append(("card", t, 0))
                s -= t
        if s > 0:
            # 余额按量：秒 * 单价(分/秒)，零碎分累计
            fen_per_sec = config.PRICE_FEN_PER_MINUTE / 60.0
            self._money_frac += s * fen_per_sec
            fen = int(self._money_frac)
            if fen > 0:
                got = self.db.deduct_money(self.user_id, fen)
                self._money_frac -= got
                if got >= fen:
                    used.append(("money", s, fen))
                    s = 0
                else:
                    # 余额不足扣满：只记能负担的秒数，剩余算欠费（触发断开）
                    covered = int(got / fen_per_sec) if fen_per_sec > 0 else 0
                    used.append(("money", covered, got))
                    s -= covered
            else:
                # 不足 1 分：库里确有余额才记账，否则该秒数无法计费（<2s 零头不记）
                fresh = self.db.user_by_id(self.user_id)
                if fresh and fresh["money_cents"] > 0:
                    used.append(("money", s, 0))
                s = 0
        for pool, secs, fen in used:
            self.db.log_usage(self.user_id, self.stream, int(round(secs)),
                              pool, fen)
        return "+".join(p for p, _, _ in used) or "none"

    def _refresh_entitlement(self):
        """结算后从库里刷新快照：usage 下发展示真实剩余值。"""
        user = self.db.user_by_id(self.user_id)
        if not user:
            return
        self.user = user
        self.sub = self.db.sub_active(user)
        self.trial_seconds = float(user["trial_seconds"])
        self.credit_seconds = float(user["credit_seconds"])
        self.money_cents = float(user["money_cents"])

    async def _flush_usage(self, final=False):
        """把累计时长折算扣费。"""
        if self._flushed:
            return
        if final:
            self._flushed = True   # 先置位，防止扣费->close() 递归
            charge = int(self.bill_acc + 0.999) if self.bill_acc > 0 else 0
        else:
            charge = int(self.bill_acc)
        if charge <= 0:
            return
        self.bill_acc = max(0.0, self.bill_acc - charge)
        self.billed_total += charge
        self._charge(charge)
        self._refresh_entitlement()
        await self._send({
            "type": "usage",
            "used_seconds": self.billed_total,
            "credit_seconds": int(self.credit_seconds),
            "trial_seconds": int(self.trial_seconds),
            "money_cents": int(self.money_cents),
            "sub": self.sub,
        })
        if not self._has_entitlement():
            await self._err("insufficient_balance", "额度已用完，请充值")
            await self.close()

    # ---------- 上游 ----------
    async def _open_upstream(self, tier: dict) -> str | None:
        """建连 + session.update，等到 session.updated 返回 None；被拒返回错误文本。"""
        url = f"{config.DASHSCOPE_WS_URL}?model={config.TRANSLATE_MODEL}"
        ws = await websockets.connect(
            url,
            additional_headers={"Authorization": f"Bearer {config.DASHSCOPE_API_KEY}"},
            ping_interval=20,
            max_size=8 * 1024 * 1024,
        )
        await ws.send(json.dumps(_session_config(
            self.stream, self.target, tier["voice"], tier["audio"],
            tier["phrases"], tier["manual"])))
        deadline = time.monotonic() + HANDSHAKE_TIMEOUT
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), max(0.1, deadline - time.monotonic()))
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                if ev.get("type") == "session.updated":
                    self.upstream = ws
                    return None
                if ev.get("type") == "error" or ev.get("code"):
                    await ws.close()
                    return json.dumps(ev, ensure_ascii=False)[:500]
        except BaseException:
            await ws.close()
            raise

    async def _connect_upstream(self):
        """降级链：完整配置 -> 去热词 -> 去音色 -> 纯文本 -> 自动断句（哪个被拒降哪个）。"""
        tier = {"phrases": terms_provider(self.target), "voice": self.voice,
                "audio": self.want_audio, "manual": True}
        for _ in range(5):
            err = await self._open_upstream(tier)
            if err is None:
                return
            low = err.lower()
            if tier["phrases"] and ("corpus" in low or "phrases" in low):
                tier["phrases"] = None
                self.degraded.append("phrases")
            elif (tier["voice"] or tier["audio"]) and any(
                    k in low for k in ("voice", "output", "modalit")):
                if tier["voice"]:
                    tier["voice"] = ""
                    self.degraded.append("voice")
                else:
                    tier["audio"] = False
                    self.want_audio = False
                    self.degraded.append("audio")
            elif tier["manual"] and self.stream == "self" and "turn_detection" in low:
                tier["manual"] = False
                self.degraded.append("manual")
            else:
                raise RuntimeError(err)
            log.warning("上游拒绝配置，降级 %s：%s", self.degraded[-1], err[:200])
        raise RuntimeError("上游配置协商失败")

    async def _up_send(self, obj: dict):
        if self.upstream:
            await self.upstream.send(json.dumps(obj))

    # ---------- 主循环 ----------
    async def run(self):
        if not self._has_entitlement():
            await self._err("insufficient_balance", "无可用额度，请充值")
            await self._send({"type": "closed", "reason": "no_balance"})
            return
        try:
            if not config.MOCK_UPSTREAM:
                await self._connect_upstream()
        except Exception as e:
            log.exception("上游连接失败")
            await self._err("upstream_connect_failed", f"翻译服务连接失败: {e}")
            return

        ACTIVE[id(self)] = self
        self.db.touch_user(self.user_id)
        await self._send({"type": "ready", "stream": self.stream,
                          "credit_seconds": int(self.credit_seconds),
                          "trial_seconds": int(self.trial_seconds),
                          "money_cents": int(self.money_cents),
                          "sub": self.sub, "degraded": self.degraded})
        tasks = [asyncio.create_task(t) for t in (
            self._pump_client_to_up(),
            self._pump_up_to_client(),
            self._meter_loop(),
        )]
        try:
            # 任一任务结束（客户端断开/上游关闭/额度耗尽）即收尾，
            # 不能 gather 等全部：mock/计量循环靠 _closed 退出，互相等会死锁
            done, _ = await asyncio.wait(tasks,
                                         return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.exception():
                    log.info("会话结束: %s", t.exception())
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
                b64 = msg.get("pcm", "")
                if not isinstance(b64, str) or len(b64) > MAX_AUDIO_B64_LEN:
                    await self._err("audio_too_large", "音频帧过大")
                    break
                try:
                    pcm = base64.b64decode(b64, validate=True)
                except Exception:
                    continue   # 非法 base64 直接丢弃
                now = time.monotonic()
                if self._first_audio_at is None:
                    self._first_audio_at = now
                secs = len(pcm) / 2 / 16000
                self._appended_seconds += secs
                # 防刷流：累计音频时长超出 MAX_AUDIO_RATE 倍实时的部分即"透支"，
                # 透支超过 RATE_WINDOW_SECONDS 秒则断开（只增不减，防间歇性刷流绕过）
                over = self._appended_seconds - MAX_AUDIO_RATE * (
                    now - self._first_audio_at)
                if over > RATE_WINDOW_SECONDS:
                    await self._err("audio_rate_abuse", "音频推流超速")
                    break
                if self.stream == "self" or not self._saw_speech_event:
                    # self 流按住才发 = 发多少计多少；mate 流未收到 speech 事件时退化
                    self._acc(secs)
                await self._up_send({
                    "type": "input_audio_buffer.append",
                    "audio": b64,
                })
            elif t == "commit":
                await self._up_send({"type": "input_audio_buffer.commit"})
            elif t == "clear":
                await self._up_send({"type": "input_audio_buffer.clear"})
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
            if self._speech_t0 is None:   # 多人重叠说话：从最早开口计起，不重置
                self._speech_t0 = time.monotonic()
        elif et == "input_audio_buffer.speech_stopped":
            if self._speech_t0 is not None:
                self._acc(time.monotonic() - self._speech_t0)
                self._speech_t0 = None
            return
        if et in ("session.created", "session.updated", "input_audio_buffer.committed",
                  "response.created"):
            return

        if et == "error" or ev.get("code"):
            await self._err("upstream_error", json.dumps(ev, ensure_ascii=False)[:500])
            return

        # --- 译文音频（TTS 播报）---
        # 事件名兼容：response.audio.delta / *.output_audio.delta 等，
        # 排除 audio_transcript（那是文本）
        if self.want_audio and et.endswith("audio.delta") \
                and "transcript" not in et:
            pcm = ev.get("delta", "")
            if pcm:
                await self._send({"type": "audio", "pcm": pcm})
                self._audio_chunks += 1
                if self._audio_chunks == 1:
                    log.info("收到首个 TTS 音频帧(%s)", et)
            return

        if et == "session.finished":
            await self._send({"type": "closed", "reason": "finished"})
            self._closed = True
            return

        # 原文 / 译文 / 说话人 / 轮次结束：按 item 关联后下发
        for msg in self.norm.feed(ev):
            await self._send(msg)

    async def _meter_loop(self):
        while not self._closed:
            await asyncio.sleep(3)
            if self.bill_acc >= FLUSH_EVERY_SECONDS:
                await self._flush_usage()
            else:
                # 无结算时也刷新权益：订阅中途过期/余额被扣光可在一个周期内生效
                self._refresh_entitlement()
                if self.user.get("banned"):
                    await self.kick("banned", "账号已被封禁")
                    break
                if not self._has_entitlement():
                    await self._err("insufficient_balance", "额度已用完，请充值")
                    await self.close()
                    break

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
                await self._send({"type": "seg", "id": f"mock_{self.seq}", "speaker": spk,
                                  "src": f"[mock src {i}]", "src_final": True,
                                  "dst": f"[mock dst {i}]", "dst_final": True})
                if self.want_audio:
                    # 发 0.3s 静音 PCM(24k) 模拟译文音频
                    pcm = b"\x00" * int(24000 * 2 * 0.3)
                    await self._send({"type": "audio",
                                      "pcm": base64.b64encode(pcm).decode()})
        # 保证循环退出
        return

    def info(self) -> dict:
        """管理后台实时会话展示用。"""
        return {"sid": id(self), "user_id": self.user_id,
                "username": self.user.get("username") or "游客",
                "stream": self.stream, "target": self.target,
                "audio": self.want_audio, "started_at": self.started_at,
                "billed_seconds": self.billed_total, "ip": self.client_ip,
                "degraded": self.degraded}

    async def kick(self, code: str = "kicked", msg: str = "会话已被管理员断开"):
        await self._err(code, msg)
        await self._send({"type": "closed", "reason": code})
        await self.close()

        async def _close_ws():   # 关闭握手可能等慢客户端，放后台不阻塞调用方
            try:
                await self.ws.close()
            except Exception:
                pass
        asyncio.create_task(_close_ws())

    async def close(self):
        self._closed = True
        ACTIVE.pop(id(self), None)
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
