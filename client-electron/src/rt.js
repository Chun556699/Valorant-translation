/* 翻译会话（主进程）：直连 DashScope Realtime / 服务器中转。
 * ws 包支持 Authorization 头（浏览器 WebSocket 不支持自定义头）。
 * 事件经回调发回主窗 renderer；TTS 音频帧走 'tts' 通道。
 */
const WebSocket = require('ws');

const DEFAULT_WS =
  "wss://llm-5gwthr14damb7ihz.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime";
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

function sessionConfig(target, voice, wantAudio, stream, phrases) {
  // mate 连续流用 speaker_detection；self 按住说话用 Manual(null)+commit
  const turn = stream === "self" ? null : {
    type: "speaker_detection", threshold: 0.5, silence_duration_ms: 1000,
  };
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
      try {
        await this._connectOnce(this.phrases);
      } catch (e) {
        if (e === "RETRY_NO_PHRASES" && this.phrases) {
          this.phrases = null;
          console.log(`[${this.stream}] 端点不支持热词，降级重连`);
          try { await this._connectOnce(null); }
          catch (e2) { this._connErr(e2); }
        } else this._connErr(e);
      }
      this.ready = false;
      if (this._stop) break;
      this._state("reconnecting");
      await new Promise(r => setTimeout(r, Math.min(backoff, 15) * 1000));
      backoff = Math.min(backoff * 2, 15);
    }
  }

  _connErr(e) {
    this._ev({ type: "error", code: "conn",
               msg: String(e && e.message || e).slice(0, 300) });
  }

  _connectOnce(phrases) {
    return new Promise((resolve, reject) => {
      let settled = false, handshakeDone = false;
      const ws = new WebSocket(this.url, {
        headers: { Authorization: `Bearer ${this.key}` },
        handshakeTimeout: 15000,
      });
      this.ws = ws;
      ws.on("open", () => {
        ws.send(JSON.stringify(sessionConfig(
          this.target, this.voice, this.wantAudio, this.stream, phrases)));
      });
      ws.on("error", (err) => {
        if (!settled) { settled = true; reject(err); }
      });
      ws.on("close", () => {
        this.ready = false;
        if (!settled) { settled = true;
          handshakeDone ? resolve() : reject(new Error("ws closed"));
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
          const msg = JSON.stringify(ev).slice(0, 300);
          if (!settled) { settled = true;
            reject(phrases && /corpus|phrases/i.test(msg)
                   ? "RETRY_NO_PHRASES" : new Error(msg));
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
      this._ev({ type: "error", code: "upstream",
                 msg: JSON.stringify(ev).slice(0, 300) }); return;
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
    this.url = `${base}/ws/translate?token=${cfg.token}&stream=${stream}` +
               `&target=${stream === "mate" ? cfg.target_mate : cfg.target_self}` +
               `&voice=${cfg.voice}&audio=${cfg.play_audio ? 1 : 0}`;
    this.ws = null; this.ready = false; this._stop = false;
  }
  start() { this._stop = false; this._loop(); }
  stop() { this._stop = true; try { this.ws && this.ws.close(); } catch {} }
  async _loop() {
    let backoff = 1;
    while (!this._stop) {
      this.emit("rt-state", { stream: this.stream, state: "connecting" });
      try {
        await new Promise((res) => {
          const ws = new WebSocket(this.url, { handshakeTimeout: 15000 });
          this.ws = ws;
          ws.on("close", res); ws.on("error", res);
          ws.on("message", (raw) => {
            let ev; try { ev = JSON.parse(raw.toString()); } catch { return; }
            if (ev.type === "ready") {
              this.ready = true;
              this.emit("rt-state", { stream: this.stream, state: "ready" });
            } else if (ev.type === "closed")
              this.emit("rt-state", { stream: this.stream, state: "closed" });
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
      if (this._stop) break;
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
