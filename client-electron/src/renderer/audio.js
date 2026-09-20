/* 音频：麦克风/系统回环采集（Web Audio + Worklet 重采样 16k）
 *      + TTS 播报（24kHz PCM -> AudioContext，可选输出设备）
 * 回环 = getDisplayMedia({audio:'loopback'})，采系统默认输出设备。
 */

class Capture {
  /* onChunk(pcm16k:ArrayBuffer), onLevel(rms) */
  constructor() {
    this.ctx = null; this.node = null; this.stream = null;
    this.onChunk = null; this.onLevel = null;
    this.running = false;
    this._filterOn = false;
  }
  setFilter(on) {
    this._filterOn = on;
    if (this.node) this.node.port.postMessage({ type: 'filter', on });
  }
  async _attach(mediaStream) {
    this.stream = mediaStream;
    this.ctx = new AudioContext();
    await this.ctx.audioWorklet.addModule('../worklets/pcm16.js');
    const src = this.ctx.createMediaStreamSource(mediaStream);
    this.node = new AudioWorkletNode(this.ctx, 'pcm16');
    this.node.port.postMessage({ type: 'filter', on: this._filterOn });
    this.node.port.onmessage = (e) => {
      const { pcm, level } = e.data;
      if (this.onLevel) this.onLevel(level);
      if (this.onChunk) this.onChunk(pcm);
    };
    const mute = this.ctx.createGain();   // 不回放，只挂进图让 worklet 跑
    mute.gain.value = 0;
    src.connect(this.node).connect(mute).connect(this.ctx.destination);
    this.running = true;
  }
  async startMic(deviceId) {
    this.stop();
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        deviceId: deviceId ? { exact: deviceId } : undefined,
        channelCount: 1, echoCancellation: false,
        noiseSuppression: false, autoGainControl: false,
      },
    });
    await this._attach(stream);
  }
  async startLoopback() {
    this.stop();
    const stream = await navigator.mediaDevices.getDisplayMedia({
      video: true, audio: true,      // video 必需，拿到音频后丢弃视频轨
    });
    stream.getVideoTracks().forEach(t => t.stop());
    if (!stream.getAudioTracks().length) throw new Error('无系统音频轨');
    await this._attach(stream);
  }
  stop() {
    this.running = false;
    try { this.node && this.node.disconnect(); } catch {}
    try { this.stream && this.stream.getTracks().forEach(t => t.stop()); } catch {}
    try { this.ctx && this.ctx.close(); } catch {}
    this.node = null; this.ctx = null; this.stream = null;
  }
}

/* TTS 播报：PCM24k 队列 -> AudioContext 顺序排播 */
class TTSPlayer {
  constructor() {
    this.ctx = null; this.nextTime = 0; this.playing = false;
    this._pending = 0;
  }
  async _ensure(deviceId) {
    if (!this.ctx || this._dev !== deviceId) {
      try { this.ctx && this.ctx.close(); } catch {}
      this.ctx = new AudioContext({ sampleRate: 24000 });
      this._dev = deviceId;
      if (deviceId && this.ctx.setSinkId) {
        try { await this.ctx.setSinkId(deviceId); } catch (e) {
          console.warn('setSinkId 失败，用默认输出', e);
        }
      }
      this.nextTime = 0;
    }
    if (this.ctx.state === 'suspended') await this.ctx.resume();
  }
  isActive() { return this._pending > 0; }
  async play(b64, deviceId) {
    await this._ensure(deviceId);
    const raw = atob(b64);
    const n = Math.floor(raw.length / 2);
    const f32 = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      let v = raw.charCodeAt(i * 2) | (raw.charCodeAt(i * 2 + 1) << 8);
      if (v > 32767) v -= 65536;
      f32[i] = v / 32768;
    }
    if (!n) return;
    const buf = this.ctx.createBuffer(1, n, 24000);
    buf.copyToChannel(f32, 0);
    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.ctx.destination);
    const t = Math.max(this.ctx.currentTime + 0.02, this.nextTime);
    src.start(t);
    this.nextTime = t + buf.duration;
    this._pending++;
    src.onended = () => { this._pending--; };
  }
  beep(deviceId) {   // 试听：880Hz 0.5s
    const raw = new Uint8Array(24000);   // 0.5s * 24000 * 2B
    for (let i = 0; i < 12000; i++) {
      const v = Math.round(Math.sin(2 * Math.PI * 880 * i / 24000) * 0.4 * 32767);
      raw[i * 2] = v & 0xff; raw[i * 2 + 1] = (v >> 8) & 0xff;
    }
    let bin = ''; for (const b of raw) bin += String.fromCharCode(b);
    this.play(btoa(bin), deviceId);
  }
  stop() { try { this.ctx && this.ctx.close(); } catch {}; this.ctx = null; this._pending = 0; }
}
