/* 悬浮字幕窗：收主窗经主进程转发的 seg/seg-done/status/ptt/mate_tick 事件。
 * 每段语音（按 key = 通道+原文 item_id）一个气泡：译文在上、原文在下，定稿后按设置自动淡出。 */
const list = document.getElementById('list');
const statusEl = document.getElementById('status');
const pttDot = document.getElementById('pttDot');
const mateDot = document.getElementById('mateDot');
const root = document.documentElement;

const bubbles = new Map();   // key -> {el, srcEl, dstEl, timer}
let cfg = { max_bubbles: 6, overlay_src_color: '#b8c2cc', overlay_dst_color: '#ffffff',
            overlay_opacity: .6, overlay_font_scale: 1, overlay_fade_sec: 20,
            overlay_show_src: true, click_through: false, overlay_locked: false, ui_lang: 'zh' };
const EN = { '队友': 'Mate', '穿透': 'Pin', '就绪': 'Ready' };
const t = (s) => (cfg.ui_lang === 'en' ? EN[s] || s : s);

let mateTimer = null;
api.on('cfg', (c) => { Object.assign(cfg, c); applyStyle(); });
api.on('seg', upsert);
api.on('seg-done', ({ key }) => {
  const b = bubbles.get(key);
  if (b) { b.el.classList.remove('live'); scheduleFade(key, b); }
});
api.on('status', (s) => { statusEl.textContent = s; });
api.on('ptt', (on) => pttDot.classList.toggle('on', on));
api.on('mate_tick', () => {
  mateDot.classList.add('on');
  clearTimeout(mateTimer);
  mateTimer = setTimeout(() => mateDot.classList.remove('on'), 900);
});
api.on('mate-state', (on) => document.getElementById('btnMate').classList.toggle('off', !on));

function applyStyle() {
  root.style.setProperty('--bgA', String(cfg.overlay_opacity));
  root.style.setProperty('--fs', String(cfg.overlay_font_scale || 1));
  document.body.classList.toggle('nosrc', cfg.overlay_show_src === false);
  document.body.classList.toggle('through', !!cfg.click_through);
  document.body.classList.toggle('locked', !!cfg.overlay_locked);
  const lock = document.getElementById('btnLock');
  lock.textContent = cfg.overlay_locked ? '🔒' : '🔓';
  lock.title = cfg.overlay_locked ? t('已锁定，点击解锁') : t('点击锁定位置');
  document.getElementById('btnMate').textContent = t('队友');
  document.getElementById('btnPin').textContent = t('穿透');
  for (const b of bubbles.values()) {
    b.srcEl.style.color = cfg.overlay_src_color;
    b.dstEl.style.color = cfg.overlay_dst_color;
  }
  trim();
}

function scheduleFade(key, b) {
  clearTimeout(b.timer);
  const sec = +cfg.overlay_fade_sec || 0;
  if (!sec) return;
  b.timer = setTimeout(() => {
    b.el.classList.add('fade');
    setTimeout(() => remove(key), 1300);
  }, sec * 1000);
}
function remove(key) {
  const b = bubbles.get(key);
  if (!b) return;
  clearTimeout(b.timer); b.el.remove(); bubbles.delete(key);
}
function trim() {
  const max = cfg.max_bubbles || 6;
  while (bubbles.size > max) remove(bubbles.keys().next().value);
}

function upsert(m) {
  let b = bubbles.get(m.key);
  if (!b && m.late) return;   // 已淡出的段收到迟到更新：不复活气泡
  if (!b) {
    const el = document.createElement('div');
    el.className = 'b live';
    const who = document.createElement('span');
    who.className = 'who'; who.textContent = m.name; who.style.background = m.color || '#ffd479';
    const dst = document.createElement('div'); dst.className = 'dst'; dst.style.color = cfg.overlay_dst_color;
    const src = document.createElement('div'); src.className = 'src'; src.style.color = cfg.overlay_src_color;
    el.append(who, dst, src);
    list.appendChild(el);
    b = { el, srcEl: src, dstEl: dst, timer: null };
    bubbles.set(m.key, b);
    trim();
  }
  b.dstEl.textContent = m.dst || '';
  b.srcEl.textContent = m.src || '';
  b.dstEl.style.display = m.dst ? '' : 'none';
  b.srcEl.style.display = m.src ? '' : 'none';
  if (m.final) { b.el.classList.remove('live'); scheduleFade(m.key, b); }
  list.scrollTop = list.scrollHeight;
}

/* 顶栏按钮 -> 主窗 */
document.getElementById('btnMate').onclick = () => api.overlayCtl('mate');
document.getElementById('btnPin').onclick = () => api.overlayCtl('pin');
document.getElementById('btnLock').onclick = () => api.overlayCtl('lock');

/* 拖拽：顶栏按下 -> 主进程跟随光标移动窗口；松开/失焦结束。
 * 锁定或穿透（bar 不可交互到不了这）时不触发。 */
const bar = document.getElementById('bar');
bar.addEventListener('mousedown', (e) => {
  if (cfg.overlay_locked || e.target.closest('.bbtn')) return;
  api.ovDrag('start');
});
// 穿透模式：光标碰到顶栏 -> 整窗临时可交互；光标出窗由主进程轮询判定后恢复穿透
bar.addEventListener('mouseenter', () => {
  if (cfg.click_through && !cfg.overlay_locked) {
    document.body.classList.add('barhover'); api.ovHover(true);
  }
});
api.on('ovarm', (on) => { if (!on) document.body.classList.remove('barhover'); });
window.addEventListener('mouseup', () => { api.ovDrag('end'); api.ovSize('end'); });
window.addEventListener('blur', () => { api.ovDrag('end'); api.ovSize('end'); });
// 右下角手柄：拖拽缩放
document.getElementById('grip').addEventListener('mousedown', (e) => {
  if (cfg.overlay_locked || cfg.click_through) return;
  e.preventDefault();
  api.ovSize('start');
});
document.getElementById('btnHide').onclick = () => api.overlayCtl('hide');
document.getElementById('btnFold').onclick = () => document.body.classList.toggle('folded');
