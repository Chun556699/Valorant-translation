/* AudioWorklet：float32@ctxRate -> Int16@16000 分块输出。
 * port.onmessage: {type:'filter', on:bool} 切语音频段过滤。
 * 每 ~100ms postMessage({pcm: ArrayBuffer, level: rms})。
 */
class PCM16 extends AudioWorkletProcessor {
  constructor() {
    super();
    this.targetRate = 16000;
    this.buf = [];           // float32 攒样
    this.filterOn = false;
    this._yp = 0; this._xp = 0; this._lp = 0;
    const dt = 1 / this.targetRate;
    this._aHP = (1 / (2 * Math.PI * 200)) / (1 / (2 * Math.PI * 200) + dt);
    this._aLP = dt / (1 / (2 * Math.PI * 3400) + dt);
    this.port.onmessage = (e) => {
      if (e.data && e.data.type === 'filter') this.filterOn = !!e.data.on;
    };
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch && ch.length) {
      // 简单复制（48k mono 或取第一声道）
      this.buf.push(ch.slice(0));
    }
    // 攒够 ~200ms 源音频再处理输出（降低 postMessage 频率）
    let total = 0;
    for (const b of this.buf) total += b.length;
    if (total >= sampleRate * 0.1) {
      const flat = new Float32Array(total);
      let o = 0;
      for (const b of this.buf) { flat.set(b, o); o += b.length; }
      this.buf = [];
      // 重采样到 16k
      const n = Math.floor(flat.length * this.targetRate / sampleRate);
      const out = new Int16Array(n);
      const step = flat.length / n;
      let sum = 0;
      for (let i = 0; i < n; i++) {
        const s = flat[Math.floor(i * step)];
        sum += Math.abs(s);
        out[i] = Math.max(-32768, Math.min(32767, Math.round(s * 32767)));
      }
      // 语音频段过滤（单极点 HP+LP，移植自 Python 版）
      if (this.filterOn) {
        let yp = this._yp, xp = this._xp, lp = this._lp;
        const aHP = this._aHP, aLP = this._aLP;
        for (let i = 0; i < out.length; i++) {
          const x = out[i];
          const hp = aHP * (yp + x - xp); xp = x; yp = hp;
          lp += aLP * (hp - lp); out[i] = Math.max(-32768, Math.min(32767, lp));
        }
        this._yp = yp; this._xp = xp; this._lp = lp;
      }
      this.port.postMessage({ pcm: out.buffer, level: sum / n },
                            [out.buffer]);
    }
    return true;
  }
}
registerProcessor('pcm16', PCM16);
