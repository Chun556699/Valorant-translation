/* 翻译会话：直连 DashScope Realtime / 服务器中转，接口统一。
 *   new Session(stream:'mate'|'self', {onEvent,onState,onAudio})
 *   .start() .stop() .sendAudio(Int16Array/bytes) .commit() .clear()
 */
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

function b64encode(buf) {
  let bin = "";
  const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin);
}
function b64decode(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out.buffer;
}

class DirectSession {
  constructor(c, stream, hooks) {
    this.stream = stream;
    this.hooks = hooks;
    this.url = `${(c.dash_ws_url || DEFAULT_WS).replace(/\/+$/, "")}?model=${c.dash_model || DEFAULT_MODEL}`;
    this.key = c.dash_key;
    this.target = stream === "mate" ? c.target_mate : c.target_self;
    this.voice = c.voice;
    this.wantAudio = !!c.play_audio;
    this.phrases = termsForTarget(this.target);
    this.ws = null; this.ready = false;
    this._stop = false;
    this._seen = new Set();
  }

  start() { this._stop = false; this._loop(); }
  stop() { this._stop = true; try { this.ws && this.ws.close(); } catch {} }

  async _loop() {
    let backoff = 1;
    while (!this._stop) {
      this._state("connecting");
      try {
        await this._connectOnce(this.phrases);
      } catch (e) {
        if (e === "RETRY_NO_PHRASES" && this.phrases) {
          this.phrases = null;
          try { await this._connectOnce(null); }
          catch (e2) { this._afterFail(e2); }
        } else this._afterFail(e);
      }
      this.ready = false;
      if (this._stop) break;
      this._state("reconnecting");
      await new Promise(r => setTimeout(r, Math.min(backoff, 15) * 1000));
      backoff = Math.min(backoff * 2, 15);
    }
  }

  _afterFail(e) {
    this._ev({ type: "error", code: "conn", msg: String(e && e.message || e) });
  }

  _connectOnce(phrases) {
    return new Promise((resolve, reject) => {
      let handshakeDone = false;
      const ws = new WebSocket(this.url, {
        headers: { Authorization: `Bearer ${this.key}` },
      });
      // 浏览器 WebSocket 不支持自定义 header —— Electron renderer 可以走
      // Node net，但更简单：把 key 放子协议兜不住，直接明文 query？
      // Electron 的 WebSocket 支持 headers 选项（Node 实现）。
      this.ws = ws;
      ws.onopen = () => {
        ws.send(JSON.stringify(sessionConfig(
          this.target, this.voice, this.wantAudio, this.stream, phrases)));
      };
      ws.onerror = () => {};
      ws.onclose = () => { if (!handshakeDone) reject(new Error("ws closed")); else resolve(); };
      ws.onmessage = (m) => {
        let ev; try { ev = JSON.parse(m.data); } catch { return; }
        const t = ev.type || "";
        if (t === "session.created" || t === "session.updated") {
          handshakeDone = true; this.ready = true; this._state("ready");
          return;
        }
        if (!handshakeDone && (t === "error" || ev.code)) {
          const msg = JSON.stringify(ev).slice(0, 300);
          if (phrases && /corpus|phrases/i.test(msg)) reject("RETRY_NO_PHRASES");
          else reject(new Error(msg));
          return;
        }
        this._handle(ev);
      };
    });
  }

  _handle(ev) {
    const t = ev.type || "", h = this.hooks;
    if (["input_audio_buffer.committed", "response.created",
         "input_audio_buffer.speech_started",
         "input_audio_buffer.speech_stopped"].includes(t)) return;
    if (t === "response.done") {
      h.onEvent({ type: "turn_done", speaker: deepFindSpeaker(ev) }); return;
    }
    if (t === "error" || ev.code) {
      h.onEvent({ type: "error", code: "upstream",
                  msg: JSON.stringify(ev).slice(0, 300) }); return;
    }
    if (t.endsWith("audio.delta") && !t.includes("transcript") && h.onAudio) {
      const pcm = b64decode(ev.delta || "");
      if (pcm.byteLength) h.onAudio(pcm);
      return;
    }
    if (t.startsWith("conversation.item.input_audio_transcription")) {
      const speaker = deepFindSpeaker(ev);
      const text = ev.transcript || ev.text || "";
      const stash = ev.stash || "", delta = ev.delta || "";
      const final = t.endsWith(".completed");
      if (delta && !text)
        h.onEvent({ type: "seg", speaker, src_delta: delta, final: false });
      else
        h.onEvent({ type: "seg", speaker, src: text + (final ? "" : stash),
                    src_final: final });
      return;
    }
    if (t.startsWith("response.text.") || t.startsWith("response.audio_transcript.")) {
      const speaker = deepFindSpeaker(ev);
      if (t.endsWith(".delta")) {
        h.onEvent({ type: "seg", speaker, dst_delta: ev.delta || "",
                    final: false });
        return;
      }
      const text = ev.text || ev.transcript || "", stash = ev.stash || "";
      h.onEvent({ type: "seg", speaker, dst: text + (t.endsWith(".done") ? "" : stash),
                  dst_final: t.endsWith(".done") });
      return;
    }
    if (t === "session.finished") {
      h.onEvent({ type: "closed", reason: "finished" }); return;
    }
    const spk = deepFindSpeaker(ev);
    if (spk) h.onEvent({ type: "speaker", speaker: spk, event: t });
    if (t && !this._seen.has(t)) { this._seen.add(t); console.log(`[${this.stream}] ev ${t}`); }
  }

  sendAudio(pcm16k) {
    if (this.ready && this.ws && this.ws.readyState === 1)
      this.ws.send(JSON.stringify({ type: "input_audio_buffer.append",
                                    audio: b64encode(pcm16k) }));
  }
  commit() { this._send({ type: "input_audio_buffer.commit" }); }
  clear()  { this._send({ type: "input_audio_buffer.clear" }); }
  _send(o) {
    if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify(o));
  }
  _state(s) { this.hooks.onState && this.hooks.onState(s); }
  _ev(e)    { this.hooks.onEvent && this.hooks.onEvent(e); }
}

/* 服务器中转模式：同样接口 */
class RelaySession {
  constructor(c, stream, hooks) {
    this.stream = stream; this.hooks = hooks;
    const base = c.server_url.replace(/^https?/, m => m === "https" ? "wss" : "ws")
                           .replace(/\/+$/, "");
    this.url = `${base}/ws/translate?token=${c.token}&stream=${stream}` +
               `&target=${stream === "mate" ? c.target_mate : c.target_self}` +
               `&voice=${c.voice}&audio=${c.play_audio ? 1 : 0}`;
    this.ws = null; this.ready = false; this._stop = false;
  }
  start() { this._stop = false; this._loop(); }
  stop() { this._stop = true; try { this.ws && this.ws.close(); } catch {} }
  async _loop() {
    let backoff = 1;
    while (!this._stop) {
      this.hooks.onState && this.hooks.onState("connecting");
      try {
        await new Promise((res, rej) => {
          const ws = new WebSocket(this.url);
          this.ws = ws;
          ws.onclose = () => res();
          ws.onerror = () => rej(new Error("ws error"));
          ws.onmessage = (m) => {
            let ev; try { ev = JSON.parse(m.data); } catch { return; }
            if (ev.type === "ready") { this.ready = true; this.hooks.onState("ready"); }
            else if (ev.type === "closed") this.hooks.onState("closed");
            else if (ev.type === "audio" && this.hooks.onAudio)
              this.hooks.onAudio(b64decode(ev.pcm || ""));
            else this.hooks.onEvent && this.hooks.onEvent(ev);
          };
        });
      } catch (e) {
        this.hooks.onEvent && this.hooks.onEvent(
          { type: "error", code: "conn", msg: String(e.message || e) });
      }
      this.ready = false;
      if (this._stop) break;
      this.hooks.onState && this.hooks.onState("reconnecting");
      await new Promise(r => setTimeout(r, Math.min(backoff, 15) * 1000));
      backoff = Math.min(backoff * 2, 15);
    }
  }
  sendAudio(pcm16k) {
    if (this.ready && this.ws && this.ws.readyState === 1)
      this.ws.send(JSON.stringify({ type: "audio", pcm: b64encode(pcm16k) }));
  }
  commit() { this._send({ type: "commit" }); }
  clear()  { this._send({ type: "clear" }); }
  _send(o) { if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify(o)); }
}
