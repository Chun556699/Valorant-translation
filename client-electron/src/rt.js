/* 翻译会话（主进程）：直连 DashScope Realtime / 服务器中转。
 * ws 包支持 Authorization 头（浏览器 WebSocket 不支持自定义头）。
 * 事件经回调发回主窗 renderer；TTS 音频帧走 'tts' 通道。
 */
const WebSocket = require('ws');

/* 通用端点；业务空间专属域名请在设置页自行填写 */
const DEFAULT_WS = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime";
const DEFAULT_MODEL = "qwen3.8-livetranslate-flash-realtime";

function deepFindSpeaker(obj) {
  const keys = ["speaker", "speaker_id", "speaker_label", "speaker_idx",
                "spk", "spk_id"];
  if (obj && typeof obj === "object") {
    if (Array.isArray(obj)) {
      for (const v of obj) { const r = deepFindSpeaker(v); if (r) return r; }
    } else {
      for (const k of keys) {
        const v = obj[k];
        if (v !== undefined && v !== null && String(v) !== "") return String(v);
      }
      for (const v of Object.values(obj)) {
        const r = deepFindSpeaker(v); if (r) return r;
      }
    }
  }
  return null;
}

function errText(ev) {
  /* DashScope error 事件 -> 可读文案 */
  const e = ev.error || ev;
  const t = e.type || ev.code || "error";
  const m = e.message || ev.message || "";
  return m ? `[${t}] ${m}` : JSON.stringify(ev).slice(0, 200);
}

/* 上游事件 -> 归一化 seg（与服务端 relay.EventNormalizer 同逻辑）。
 * qwen3.8：speaker_id 只在 speech_started（带 item_id）里；原文事件带同一 item_id；
 * 译文 item 经 conversation.item.created.previous_item_id 指回原文 item。
 * 按 item 关联后，多人同时说话各自成段，不会串进同一个气泡。 */
class EventNormalizer {
  constructor() {
    this.spkByItem = new Map(); this.srcOf = new Map();
    this.lastSrc = null; this.lastSpk = null;
  }
  _put(m, k, v) {
    m.delete(k); m.set(k, v);
    if (m.size > 256) m.delete(m.keys().next().value);
  }
  _spk(item, ev) {
    let s = item ? this.spkByItem.get(item) : undefined;
    if (s == null) {
      s = deepFindSpeaker(ev);
      if (s != null && item) this._put(this.spkByItem, item, s);
    }
    if (s == null && item && item === this.lastSrc) s = this.lastSpk;
    return s ?? null;
  }
  _srcForDst(tid) {
    if (!tid) return this.lastSrc;
    let sid = this.srcOf.get(tid);
    if (sid == null) { sid = this.lastSrc || tid; this._put(this.srcOf, tid, sid); }
    return sid;
  }
  feed(ev) {
    const t = ev.type || "";
    if (t === "input_audio_buffer.speech_started") {
      const item = ev.item_id || null;
      let spk = ev.speaker_id;
      spk = spk != null && String(spk) !== "" ? String(spk) : deepFindSpeaker(ev);
      if (item) { this.lastSrc = item; if (spk != null) this._put(this.spkByItem, item, spk); }
      if (spk != null) this.lastSpk = spk;
      return [{ type: "speaker", id: item, speaker: spk }];
    }
    if (t === "conversation.item.created") {
      const it = ev.item || {};
      if (it.id && it.role === "assistant" && ev.previous_item_id)
        this._put(this.srcOf, it.id, ev.previous_item_id);
      else if (it.id && it.role === "user") this.lastSrc = it.id;
      return [];
    }
    if (t.startsWith("conversation.item.input_audio_transcription")) {
      if (t.endsWith(".failed")) return [];
      const id = ev.item_id || this.lastSrc;
      const base = { type: "seg", id, speaker: this._spk(id, ev) };
      const delta = ev.delta || "", text = ev.transcript || ev.text || "";
      if (t.endsWith(".completed")) return [{ ...base, src: text, src_final: true }];
      if (delta && !text) return [{ ...base, src_delta: delta }];
      return [{ ...base, src: text + (ev.stash || "") }];
    }
    if (t.startsWith("response.text.") || t.startsWith("response.audio_transcript.")) {
      const id = this._srcForDst(ev.item_id);
      const base = { type: "seg", id, speaker: this._spk(id, ev) };
      if (t.endsWith(".delta")) return ev.delta ? [{ ...base, dst_delta: ev.delta }] : [];
      const text = ev.text || ev.transcript || "";
      if (t.endsWith(".done")) return [{ ...base, dst: text, dst_final: true }];
      return [{ ...base, dst: text + (ev.stash || "") }];
    }
    if (t === "response.done") {
      const out = ((ev.response || {}).output || []).map((it) => {
        const id = this._srcForDst(it.id);
        return { type: "turn_done", id, speaker: this._spk(id, ev) };
      });
      return out.length ? out
        : [{ type: "turn_done", id: this.lastSrc, speaker: deepFindSpeaker(ev) }];
    }
    return [];
  }
}

function sessionConfig(target, voice, wantAudio, stream, phrases, manual, silenceMs) {
  // mate 连续流用 speaker_detection；self 按住说话用 Manual(null)+commit；
  // manual=false 是兜底档（端点若拒 null 则退回自动断句）
  const auto = { type: "speaker_detection", threshold: 0.5,
                 silence_duration_ms: silenceMs || 800 };
  const turn = (stream === "self" && manual !== false) ? null : auto;
  const translation = { language: target };
  if (phrases && Object.keys(phrases).length)
    translation.corpus = { phrases };
  return {
    type: "session.update",
    session: {
      output_modalities: wantAudio ? ["text", "audio"] : ["text"],
      audio: {
        input: { format: { type: "pcm", sample_rate: 16000 },
                 turn_detection: turn },
        output: { format: { type: "pcm", sample_rate: 24000 },
                  ...(wantAudio && voice ? { voice } : {}) },
      },
      input_audio_transcription: { model: "qwen3-asr-flash-realtime" },
      translation,
    },
  };
}

class DirectSession {
  constructor(cfg, stream, phrases, emit) {
    this.stream = stream;
    this.emit = emit;                       // (channel, payload) -> renderer
    this.url = `${(cfg.dash_ws_url || DEFAULT_WS).replace(/\/+$/, "")}` +
               `?model=${cfg.dash_model || DEFAULT_MODEL}`;
    this.key = cfg.dash_key;
    this.target = stream === "mate" ? cfg.target_mate : cfg.target_self;
    this.voice = cfg.voice;
    this.wantAudio = !!cfg.play_audio;
    this.phrases = phrases;
    this.silenceMs = +cfg.mate_silence_ms || 800;
    this.ws = null; this.ready = false; this._stop = false; this.readyAt = 0;
    this._seen = new Set();
    this.norm = new EventNormalizer();
  }

  start() { this._stop = false; this._loop(); }
  stop() { this._stop = true; try { this.ws && this.ws.close(); } catch {} }

  _state(s, reason) {
    this.emit("rt-state", { stream: this.stream, state: s, ...(reason ? { reason } : {}) });
  }
  _ev(e)    { this.emit("rt-ev", { stream: this.stream, ev: e }); }

  async _loop() {
    let backoff = 1;
    while (!this._stop) {
      this._state("connecting");
      // 降级链：完整配置 -> 去热词 -> 去音色 -> 纯文本（哪个被拒就降级）
      const tiers = [
        { phrases: this.phrases, voice: this.voice, audio: this.wantAudio },
        { phrases: null,         voice: this.voice, audio: this.wantAudio },
        { phrases: null,         voice: "",         audio: this.wantAudio },
        { phrases: null,         voice: "",         audio: false },
        { phrases: null,         voice: "",         audio: false,
          manual: false },   // 兜底：self 流退回自动断句
      ];
      let ok = false, cleanClose = false;
      for (const t of tiers) {
        try {
          cleanClose = await this._connectOnce(t);
          ok = true;
          break;
        } catch (e) {
          if (e === "WS_CLOSED") break;   // 正常断开
          const msg = String(e && e.message || e);
          if (e && e.fatal) {
            // 致命鉴权错误（Key 无效 401/403）：换档重试也没有意义，直接终止；
            // 补发带原因的终态 closed，让 UI 复位「翻译中/连接中」假状态
            // （'conn' 错误码不在 renderer 终态表，靠这里兜底复位）
            this._connErr(msg);
            this._state("closed", msg);
            this._stop = true;
            break;
          }
          if (/corpus|phrases/i.test(msg) && t.phrases) {
            this._ev({ type: "error", code: "fallback",
                      msg: "端点不支持术语热词，已降级" });
            continue;
          }
          if (/voice|output|modalit/i.test(msg) && (t.voice || t.audio)) {
            this._ev({ type: "error", code: "fallback",
                      msg: "端点拒绝音色/音频输出，已降级" });
            continue;
          }
          if (/turn_detection/i.test(msg) && t.manual !== false) {
            this._ev({ type: "error", code: "fallback",
                      msg: "端点不支持手动断句，已切自动断句" });
            continue;
          }
          this._connErr(msg);   // 其他错误：报出来，退出重试
          break;
        }
      }
      this.ready = false;
      if (this._stop) break;
      // 连接曾 ready 且存活 >= 30s 才重置退避（与 RelaySession 一致）：
      // 健康运行过的长连接遇到网络抖动，不必再等满之前累计到的 15s
      if (this.readyAt && Date.now() - this.readyAt >= 30000) backoff = 1;
      // 握手成功后的意外断线也按失败处理：先退避再重连，避免重连风暴；
      // 只有干净结束（session.finished / 主动 stop）才立即重连
      if (!ok || !cleanClose) {
        this._state("reconnecting");
        await new Promise(r => setTimeout(r, Math.min(backoff, 15) * 1000));
        backoff = Math.min(backoff * 2, 15);
      }
    }
  }

  _connErr(e) {
    this._ev({ type: "error", code: "conn",
               msg: String(e && e.message || e).slice(0, 300) });
  }

  _connectOnce(tier) {
    /* resolve(true) = 干净结束（session.finished/主动 stop）；
       resolve(false) = 握手成功但意外断线（调用方按失败退避重连） */
    return new Promise((resolve, reject) => {
      let settled = false, handshakeDone = false;
      this._cleanFinish = false;
      this.readyAt = 0;
      const ws = new WebSocket(this.url, {
        headers: { Authorization: `Bearer ${this.key}` },
        handshakeTimeout: 15000,
      });
      this.ws = ws;
      ws.on("open", () => {
        ws.send(JSON.stringify(sessionConfig(
          this.target, tier.voice, tier.audio, this.stream,
          tier.phrases, tier.manual, this.silenceMs)));
      });
      ws.on("error", (err) => {
        if (!settled) {
          settled = true;
          // 401/403/Key 无效：握手期鉴权失败属致命错误，标记后外层不再重试
          if (/401|403|unauthorized|invalid.{0,20}key/i.test(String(err && err.message)))
            err.fatal = true;
          reject(err);
        }
      });
      ws.on("close", () => {
        this.ready = false;
        if (!settled) { settled = true;
          handshakeDone ? resolve(this._cleanFinish || this._stop)
                        : reject("WS_CLOSED");
        }
      });
      ws.on("message", (raw) => {
        let ev; try { ev = JSON.parse(raw.toString()); } catch { return; }
        const t = ev.type || "";
        if (t === "session.created" || t === "session.updated") {
          handshakeDone = true; this.ready = true; this.readyAt = Date.now();
          this._state("ready");
          return;
        }
        if (!handshakeDone && (t === "error" || ev.code)) {
          if (!settled) {
            settled = true;
            try { ws.close(); } catch {}
            const err = new Error(errText(ev));
            // 握手期 401/403/Key 无效属致命错误：标记后外层终止，不再重试
            if (/401|403|unauthorized|invalid.{0,20}key/i.test(String(err.message)))
              err.fatal = true;
            reject(err);
          }
          return;
        }
        this._handle(ev);
      });
    });
  }

  _handle(ev) {
    const t = ev.type || "";
    if (["input_audio_buffer.committed", "response.created",
         "input_audio_buffer.speech_stopped"].includes(t)) return;
    if (t === "error" || ev.code) {
      this._ev({ type: "error", code: "upstream", msg: errText(ev) });
      return;
    }
    if (t.endsWith("audio.delta") && !t.includes("transcript")) {
      const pcm = ev.delta || "";
      if (pcm) this.emit("tts", { stream: this.stream, pcm });
      return;
    }
    if (t === "session.finished") {
      this._cleanFinish = true;
      this._ev({ type: "closed", reason: "finished" }); return;
    }
    const out = this.norm.feed(ev);
    for (const m of out) this._ev(m);
    if (!out.length && t && !this._seen.has(t)) {
      this._seen.add(t);
      console.log(`[${this.stream}] ev: ${t}`);
    }
  }

  sendAudio(b64) {
    if (this.ready && this.ws && this.ws.readyState === 1) {
      this.ws.send(JSON.stringify(
        { type: "input_audio_buffer.append", audio: b64 }));
      this._appended = (this._appended || 0) + 1;
    } else if ((this._dropped = (this._dropped || 0) + 1) === 1) {
      // 按住说话期间通道未就绪：帧被丢，commit 会报"缓冲为空"——先报出来
      this._ev({ type: "error", code: "dropped",
                 msg: "通道未就绪，音频未送达（检查连接状态）" });
    }
  }
  commit() {
    if (!this.ws || this.ws.readyState !== 1) return;
    if (!this._appended) {
      // 本回合没送达任何音频：发 commit 只会换来上游"缓冲为空"报错
      this._ev({ type: "error", code: "empty",
                 msg: "未采集到音频：麦克风无声或按得太快" });
      return;
    }
    this._appended = 0; this._dropped = 0;
    this._send({ type: "input_audio_buffer.commit" });
  }
  clear()  {
    this._appended = 0; this._dropped = 0;
    this._send({ type: "input_audio_buffer.clear" });
  }
  _send(o) { if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify(o)); }
}

/* 服务器中转模式 */
/* 终态错误：透传给 UI 显示原因，但不再自动重连 */
const TERMINAL = new Set(["unauthorized", "insufficient_balance", "banned", "kicked"]);

class RelaySession {
  // 与 DirectSession 同签名（phrases 在中转模式由服务端注入，这里忽略）
  constructor(cfg, stream, _phrases, emit) {
    this.stream = stream; this.emit = emit;
    const base = String(cfg.server_url || "")
      .replace(/^https?/, m => m === "https" ? "wss" : "ws")
      .replace(/\/+$/, "");
    // token 走 Authorization 头，不进 URL（避免进代理/服务器日志）
    const q = new URLSearchParams({
      stream, target: stream === "mate" ? cfg.target_mate : cfg.target_self,
      voice: cfg.voice || "", audio: cfg.play_audio ? "1" : "0" });
    // 模型自选：服务端只收白名单内的值，其余回落服务端默认（ready 回实际模型）
    if (cfg.dash_model) q.set("model", cfg.dash_model);
    this.url = `${base}/ws/translate?${q}`;
    this.token = cfg.token || "";
    this.ws = null; this.ready = false; this._stop = false;
  }
  start() { this._stop = false; this._loop(); }
  stop() { this._stop = true; try { this.ws && this.ws.close(); } catch {} }
  // 与 DirectSession 一致的事件助手：sendAudio 丢帧/commit 空缓冲分支要用，
  // 缺了会在 ipcMain 回调里同步抛 TypeError（曾致主进程未捕获异常）
  _ev(e)    { this.emit("rt-ev", { stream: this.stream, ev: e }); }
  async _loop() {
    let backoff = 1;
    while (!this._stop) {
      this.emit("rt-state", { stream: this.stream, state: "connecting" });
      let gotReady = false, terminal = false, termReason = "";
      const connectAt = Date.now();
      try {
        await new Promise((res) => {
          const ws = new WebSocket(this.url, {
            handshakeTimeout: 15000,
            headers: { Authorization: `Bearer ${this.token}` },
          });
          this.ws = ws;
          ws.on("close", res); ws.on("error", res);
          ws.on("message", (raw) => {
            let ev; try { ev = JSON.parse(raw.toString()); } catch { return; }
            if (ev.type === "ready") {
              this.ready = true; gotReady = true;
              this.emit("rt-state", { stream: this.stream, state: "ready" });
              // 服务端协商降级（热词/音色/音频输出/手动断句被上游拒绝）会随 ready 下发；
              // 透传给 UI（与 DirectSession 的 fallback 提示对齐），避免静默降级——
              // 典型后果：audio 被降级后永远收不到 TTS 帧，界面却毫无提示
              if (Array.isArray(ev.degraded) && ev.degraded.length) {
                const NAMES = { phrases: "术语热词", voice: "音色",
                                audio: "音频播报", manual: "手动断句" };
                this._ev({ type: "error", code: "fallback",
                           msg: "服务器已降级：" +
                                ev.degraded.map((d) => NAMES[d] || d).join("、") +
                                "不可用" });
              }
            } else if (ev.type === "closed") {
              // 服务端主动结束（可能紧跟终态 error）：不再把 closed 当"干净"跳过退避
              this.emit("rt-state", { stream: this.stream, state: "closed" });
            } else if (ev.type === "error" && TERMINAL.has(ev.code)) {
              // 终态错误：照常透传给 UI 显示原因，但连接结束后不再重连
              terminal = true;
              termReason = ev.msg || ev.code;
              this.emit("rt-ev", { stream: this.stream, ev });
            }
            else if (ev.type === "audio")
              this.emit("tts", { stream: this.stream, pcm: ev.pcm || "" });
            else this.emit("rt-ev", { stream: this.stream, ev });
          });
        });
      } catch (e) {
        this.emit("rt-ev", { stream: this.stream, ev:
          { type: "error", code: "conn", msg: String(e.message || e) } });
      }
      this.ready = false;
      if (terminal)
        // 终态退出：补发带原因的 closed，让 UI 复位通道芯片/测试状态
        // （running 已由终态 rt-ev 复位；裸 closed 不带 reason，服务端结束后仍会重连）
        this.emit("rt-state", { stream: this.stream, state: "closed",
                                reason: termReason });
      if (this._stop || terminal) break;
      // 连接曾 ready 且存活 >= 30s 才重置退避；否则一律按失败退避后重连，防重连风暴
      if (gotReady && Date.now() - connectAt >= 30000) backoff = 1;
      this.emit("rt-state", { stream: this.stream, state: "reconnecting" });
      await new Promise(r => setTimeout(r, Math.min(backoff, 15) * 1000));
      backoff = Math.min(backoff * 2, 15);
    }
  }
  sendAudio(b64) {
    if (this.ready && this.ws && this.ws.readyState === 1) {
      this.ws.send(JSON.stringify({ type: "audio", pcm: b64 }));
      this._appended = (this._appended || 0) + 1;
    } else if ((this._dropped = (this._dropped || 0) + 1) === 1) {
      this._ev({ type: "error", code: "dropped",
                 msg: "通道未就绪，音频未送达（检查连接状态）" });
    }
  }
  commit() {
    if (!this.ws || this.ws.readyState !== 1) return;
    if (!this._appended) {
      this._ev({ type: "error", code: "empty",
                 msg: "未采集到音频：麦克风无声或按得太快" });
      return;
    }
    this._appended = 0; this._dropped = 0;
    this._send({ type: "commit" });
  }
  clear()  {
    this._appended = 0; this._dropped = 0;
    this._send({ type: "clear" });
  }
  _send(o) { if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify(o)); }
}

module.exports = { DirectSession, RelaySession, EventNormalizer };
