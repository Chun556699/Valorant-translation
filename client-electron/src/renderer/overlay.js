/* 悬浮字幕窗：收主窗经主进程转发的 seg/status/ptt/mate_tick 事件 */
const SPEAKER_COLORS = {
  "我": "#7ecfff", "队友": "#ffd479",
  "好友1": "#9dff9d", "好友2": "#ff9dc4", "好友3": "#c9a7ff",
  "好友4": "#ffb27e", "好友5": "#8ef0e4",
};
const colorFor = (n) => SPEAKER_COLORS[n] || "#ffd479";

const list = document.getElementById('list');
const statusEl = document.getElementById('status');
const pttDot = document.getElementById('pttDot');
const mateDot = document.getElementById('mateDot');

let segs = {};            // key -> {el, src, dst, open}
let bubbles = [];
let cfg = { max_bubbles: 8, overlay_src_color: "#bbbbbb",
            overlay_dst_color: "#ffffff", overlay_opacity: .85 };

let mateTimer = null;
api.on('cfg', (c) => { Object.assign(cfg, c); applyStyle(); });
api.on('seg', (m) => addSeg(m.stream, m.speaker, m.name, m.ev));
api.on('status', (s) => statusEl.textContent = s);
api.on('ptt', (on) => pttDot.classList.toggle('on', on));
api.on('mate_tick', () => {
  mateDot.classList.add('on');
  clearTimeout(mateTimer);
  mateTimer = setTimeout(() => mateDot.classList.remove('on'), 900);
});

function applyStyle() {
  document.getElementById('wrap').style.background =
    `rgba(20,20,26,${cfg.overlay_opacity * 0.65})`;
  for (const b of bubbles) {
    b.srcEl.style.color = cfg.overlay_src_color;
    b.dstEl.style.color = cfg.overlay_dst_color;
  }
}

function addSeg(stream, rawSpeaker, name, ev) {
  const key = stream + '|' + (rawSpeaker || name);
  let seg = segs[key];
  if (ev.type === 'turn_done' || ev.turn_done) { if (seg) seg.open = false; return; }
  if (!seg || !seg.open) {
    const el = document.createElement('div');
    el.className = 'bubble';
    const who = document.createElement('div');
    who.className = 'b-who'; who.textContent = name;
    who.style.color = colorFor(name);
    const src = document.createElement('div');
    src.className = 'b-src'; src.style.color = cfg.overlay_src_color;
    const dst = document.createElement('div');
    dst.className = 'b-dst'; dst.style.color = cfg.overlay_dst_color;
    el.append(who, src, dst);
    list.appendChild(el);
    seg = segs[key] = { el, srcEl: src, dstEl: dst, src: '', dst: '', open: true };
    bubbles.push(seg);
    while (bubbles.length > (cfg.max_bubbles || 8)) {
      const old = bubbles.shift(); old.el.remove();
      // 同步释放 segs 里指向已移除气泡的引用，防内存缓增
      for (const k in segs) if (segs[k] === old) { delete segs[k]; break; }
    }
  }
  if (ev.src_delta) seg.src += ev.src_delta;
  else if (ev.src !== undefined && ev.src) seg.src = ev.src;
  if (ev.dst_delta) seg.dst += ev.dst_delta;
  else if (ev.dst !== undefined && ev.dst) seg.dst = ev.dst;
  seg.srcEl.textContent = seg.src;
  seg.dstEl.textContent = seg.dst;
  seg.srcEl.style.display = seg.src ? '' : 'none';
  seg.dstEl.style.display = seg.dst ? '' : 'none';
  if (ev.src_final && seg.dstDone) seg.open = false;
  if (ev.src_final) seg.srcDone = true;
  if (ev.dst_final) { seg.dstDone = true; if (seg.srcDone) seg.open = false; }
  list.scrollTop = list.scrollHeight;
}

/* 顶栏按钮 -> 主窗 */
document.getElementById('btnMate').onclick = () => api.overlayCtl('mate');
document.getElementById('btnPin').onclick  = () => api.overlayCtl('pin');
document.getElementById('btnHide').onclick = () => api.overlayCtl('hide');
document.getElementById('btnFold').onclick = () => {
  document.body.classList.toggle('folded');
};
api.on('mate-state', (on) =>
  document.getElementById('btnMate').textContent = on ? '队友✓' : '队友✗');
api.on('status-text', () => {});
