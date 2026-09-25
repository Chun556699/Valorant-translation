/* AudioWorklet：多声道 float32@ctxRate -> 单声道 Int16@16000，约每 100ms 输出一块。
 *
 * 重采样用加窗 sinc（Blackman，截止 ≈7.3kHz）做抗混叠：旧实现直接隔点抽取，
 * 8kHz 以上的枪声/脚步高频会折叠进语音频段，明显拖累识别准确率。
 * 多声道先平均混成单声道（游戏语音可能被声像到任一侧，只取左声道会丢字）。
 *
 * port.onmessage: {type:'filter', on:bool} 切语音频段过滤（队友流抑制低频轰鸣/高频噪声）。
 * postMessage({pcm: ArrayBuffer(Int16), level: rms(0..1)})。
 */
const OUT_RATE = 16000;
const ZEROS = 12;          // 每侧过零点数：越大过渡带越陡
const RES = 64;            // 核函数查表精度（每输入采样 64 格）

class PCM16 extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / OUT_RATE;                     // 输入采样/输出采样
    this.fc = 0.5 * Math.min(1, 1 / this.ratio) * 0.92;      // 截止（周期/输入采样）
    this.hw = Math.ceil(ZEROS / (2 * this.fc));              // 半宽（输入采样数）
    const n = this.hw * RES;
    this.kern = new Float32Array(n + 2);
    for (let i = 0; i <= n + 1; i++) {
      const x = i / RES;
      const s = x === 0 ? 1 : Math.sin(2 * Math.PI * this.fc * x) / (2 * Math.PI * this.fc * x);
      const w = x >= this.hw ? 0
        : 0.42 + 0.5 * Math.cos(Math.PI * x / this.hw) + 0.08 * Math.cos(2 * Math.PI * x / this.hw);
      this.kern[i] = 2 * this.fc * s * w;
    }
    this.hist = new Float32Array(0);   // 未消费的输入（含左侧 hw 个历史样本）
    this.pos = this.hw;                // 下一个输出样本在 hist 中的位置（小数）
    this.pending = [];
    this.pendingLen = 0;
    this.block = Math.round(sampleRate * 0.1);

    this.filterOn = false;
    this._yp = 0; this._xp = 0; this._lp = 0;
    const dt = 1 / OUT_RATE;
    const rc = (f) => 1 / (2 * Math.PI * f);
    this._aHP = rc(200) / (rc(200) + dt);    // 200Hz 高通：去轰鸣/爆炸低频
    this._aLP = dt / (rc(5000) + dt);        // 5kHz 低通：保留齿音，削尖锐噪声
    this.port.onmessage = (e) => {
      if (e.data && e.data.type === 'filter') this.filterOn = !!e.data.on;
    };
  }

  _mix(input) {
    const chs = input.length, len = input[0].length;
    if (chs === 1) return input[0].slice(0);
    const out = new Float32Array(len);
    for (let c = 0; c < chs; c++) {
      const ch = input[c];
      for (let i = 0; i < len; i++) out[i] += ch[i];
    }
    for (let i = 0; i < len; i++) out[i] /= chs;
    return out;
  }

  _resample() {
    const add = this.pendingLen;
    const buf = new Float32Array(this.hist.length + add);
    buf.set(this.hist, 0);
    let o = this.hist.length;
    for (const b of this.pending) { buf.set(b, o); o += b.length; }
    this.pending = []; this.pendingLen = 0;

    const hw = this.hw, kern = this.kern, step = this.ratio;
    const maxOut = Math.max(0, Math.floor((buf.length - hw - this.pos) / step) + 1);
    const out = new Float32Array(maxOut);
    let n = 0, pos = this.pos;
    while (pos + hw < buf.length) {
      const c = Math.floor(pos), frac = pos - c;
      let acc = 0;
      for (let k = -hw + 1; k <= hw; k++) {
        const d = Math.abs(k - frac) * RES;
        const di = d | 0;
        if (di >= kern.length - 1) continue;
        const h = kern[di] + (kern[di + 1] - kern[di]) * (d - di);
        acc += buf[c + k] * h;
      }
      out[n++] = acc;
      pos += step;
    }
    // 保留还需要的尾部（含左侧 hw 历史）
    const keepFrom = Math.max(0, Math.floor(pos) - hw);
    this.hist = buf.slice(keepFrom);
    this.pos = pos - keepFrom;
    return n === out.length ? out : out.subarray(0, n);
  }

  process(inputs) {
    const input = inputs[0];
    if (input && input.length && input[0].length) {
      const m = this._mix(input);
      this.pending.push(m);
      this.pendingLen += m.length;
    }
    if (this.pendingLen < this.block) return true;

    const f = this._resample();
    const out = new Int16Array(f.length);
    let yp = this._yp, xp = this._xp, lp = this._lp, sq = 0;
    const aHP = this._aHP, aLP = this._aLP, filt = this.filterOn;
    for (let i = 0; i < f.length; i++) {
      let s = f[i];
      if (filt) {
        const hp = aHP * (yp + s - xp); xp = s; yp = hp;
        lp += aLP * (hp - lp); s = lp;
      }
      sq += s * s;
      out[i] = Math.max(-32768, Math.min(32767, Math.round(s * 32767)));
    }
    this._yp = yp; this._xp = xp; this._lp = lp;
    if (out.length)
      this.port.postMessage({ pcm: out.buffer, level: Math.sqrt(sq / out.length) }, [out.buffer]);
    return true;
  }
}
registerProcessor('pcm16', PCM16);
