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

function sessionConfig(target, voice, wantAudio, stream, phrases, manual) {
  // mate 连续流用 speaker_detection；self 按住说话用 Manual(null)+commit；
  // manual=false 是兜底档（端点若拒 null 则退回自动断句）
  const auto = { type: "speaker_detection", threshold: 0.5,
                 silence_duration_ms: 1000 };
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
    this.ws = null; this.ready = false; this._stop = false;
    this._seen = new Set();
  }

  start() { this._stop = false; this._loop(); }
  stop() { this._stop = true; try { this.ws && this.ws.close(); } catch {} }

  _state(s) { this.emit("rt-state", { stream: this.stream, state: s }); }
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
            // 致命鉴权错误（Key 无效 401/403）：换档重试也没有意义，直接终止
            this._connErr(msg);
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
      const ws = new WebSocket(this.url, {
        headers: { Authorization: `Bearer ${this.key}` },
        handshakeTimeout: 15000,
      });
      this.ws = ws;
      ws.on("open", () => {
        ws.send(JSON.stringify(sessionConfig(
          this.target, tier.voice, tier.audio, this.stream,
          tier.phrases, tier.manual)));
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
          handshakeDone = true; this.ready = true; this._state("ready");
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
         "input_audio_buffer.speech_started",
         "input_audio_buffer.speech_stopped"].includes(t)) return;
    if (t === "response.done") {
      this._ev({ type: "turn_done", speaker: deepFindSpeaker(ev) }); return;
    }
    if (t === "error" || ev.code) {
      this._ev({ type: "error", code: "upstream", msg: errText(ev) });
      return;
    }
    if (t.endsWith("audio.delta") && !t.includes("transcript")) {
      const pcm = ev.delta || "";
      if (pcm) this.emit("tts", { stream: this.stream, pcm });
      return;
    }
    if (t.startsWith("conversation.item.input_audio_transcription")) {
      const speaker = deepFindSpeaker(ev);
      const text = ev.transcript || ev.text || "";
      const stash = ev.stash || "", delta = ev.delta || "";
      const final = t.endsWith(".completed");
      if (delta && !text)
        this._ev({ type: "seg", speaker, src_delta: delta, final: false });
      else
        this._ev({ type: "seg", speaker, src: text + (final ? "" : stash),
                   src_final: final });
      return;
    }
    if (t.startsWith("response.text.") ||
        t.startsWith("response.audio_transcript.")) {
      const speaker = deepFindSpeaker(ev);
      if (t.endsWith(".delta")) {
        this._ev({ type: "seg", speaker, dst_delta: ev.delta || "",
                   final: false });
        return;
      }
      const text = ev.text || ev.transcript || "", stash = ev.stash || "";
      this._ev({ type: "seg", speaker,
                 dst: text + (t.endsWith(".done") ? "" : stash),
                 dst_final: t.endsWith(".done") });
      return;
    }
    if (t === "session.finished") {
      this._cleanFinish = true;
      this._ev({ type: "closed", reason: "finished" }); return;
    }
    const spk = deepFindSpeaker(ev);
    if (spk) this._ev({ type: "speaker", speaker: spk, event: t });
    if (t && !this._seen.has(t)) {
      this._seen.add(t);
      console.log(`[${this.stream}] ev: ${t}`);
    }
  }

  sendAudio(b64) {
    if (this.ready && this.ws && this.ws.readyState === 1)
      this.ws.send(JSON.stringify(
        { type: "input_audio_buffer.append", audio: b64 }));
  }
  commit() { this._send({ type: "input_audio_buffer.commit" }); }
  clear()  { this._send({ type: "input_audio_buffer.clear" }); }
  _send(o) { if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify(o)); }
}

/* 服务器中转模式 */
class RelaySession {
  constructor(cfg, stream, emit) {
    this.stream = stream; this.emit = emit;
    const base = cfg.server_url
      .replace(/^https?/, m => m === "https" ? "wss" : "ws")
      .replace(/\/+$/, "");
    // token 走 Authorization 头，不进 URL（避免进代理/服务器日志）
    this.url = `${base}/ws/translate?stream=${stream}` +
               `&target=${stream === "mate" ? cfg.target_mate : cfg.target_self}` +
               `&voice=${cfg.voice}&audio=${cfg.play_audio ? 1 : 0}`;
    this.token = cfg.token || "";
    this.ws = null; this.ready = false; this._stop = false;
  }
  start() { this._stop = false; this._loop(); }
  stop() { this._stop = true; try { this.ws && this.ws.close(); } catch {} }
  async _loop() {
    let backoff = 1;
    while (!this._stop) {
      this.emit("rt-state", { stream: this.stream, state: "connecting" });
      let gotReady = false, terminal = false;
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
            } else if (ev.type === "closed") {
              // 服务端主动结束（可能紧跟终态 error）：不再把 closed 当"干净"跳过退避
              this.emit("rt-state", { stream: this.stream, state: "closed" });
            } else if (ev.type === "error" &&
                       (ev.code === "unauthorized" || ev.code === "insufficient_balance")) {
              // 终态错误：照常透传给 UI 显示原因，但连接结束后不再重连
              terminal = true;
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
      if (this._stop || terminal) break;
      // 连接曾 ready 且存活 >= 30s 才重置退避；否则一律按失败退避后重连，防重连风暴
      if (gotReady && Date.now() - connectAt >= 30000) backoff = 1;
      this.emit("rt-state", { stream: this.stream, state: "reconnecting" });
      await new Promise(r => setTimeout(r, Math.min(backoff, 15) * 1000));
      backoff = Math.min(backoff * 2, 15);
    }
  }
  sendAudio(b64) {
    if (this.ready && this.ws && this.ws.readyState === 1)
      this.ws.send(JSON.stringify({ type: "audio", pcm: b64 }));
  }
  commit() { this._send({ type: "commit" }); }
  clear()  { this._send({ type: "clear" }); }
  _send(o) { if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify(o)); }
}

module.exports = { DirectSession, RelaySession };
