/* 主窗逻辑：导航/页面/设置热更新/PTT/翻译会话编排/悬浮窗转发 */

const $ = (id) => document.getElementById(id);
const LANGS = [['zh', '中文'], ['en', 'English'], ['ja', '日本語'], ['ko', '한국어'],
  ['ru', 'Русский'], ['fr', 'Français'], ['de', 'Deutsch'], ['es', 'Español'], ['pt', 'Português']];
const VOICES = ['Cherry', 'Tina', 'Ethan', 'Serena', 'Kiki', 'Anna'];
const SPEAKER_COLORS = ['#9dff9d', '#ff9dc4', '#c9a7ff', '#ffb27e', '#8ef0e4', '#ffd479'];
const SELF_COLOR = '#7ecfff';
const PREROLL_CHUNKS = 3;     // 按下热键前补发 ~300ms 音频，防首字被吞
const PTT_TAIL_MS = 350;      // 松开后继续收 ~350ms 再提交，防尾字被截
const PTT_MIN_MS = 200;       // 短于此的误触不提交

const state = {
  running: false, testSelf: false,
  mateOn: true, chState: { mate: 'off', self: 'off' },
  // 无原型对象：服务器下发的 speaker id 可能是 "__proto__" 等，防原型污染
  speakerMap: Object.create(null), nextFriend: 1,
  ptt: false, pttSending: false, pttAt: 0, tailTimer: null, preroll: [],
  segs: new Map(),              // id -> {stream,name,color,src,dst,done,el}
};
const mic = new Capture();
const loopback = new Capture();
const player = new TTSPlayer();

function b64(buf) {
  const bytes = new Uint8Array(buf);
  let s = '';
  for (let i = 0; i < bytes.length; i += 0x8000)
    s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}
const levelPct = (rms) => rms <= 0 ? 0 : Math.max(0, Math.min(100, (20 * Math.log10(rms) + 55) * 2.2));
function escapeHtml(s) { const d = document.createElement('div'); d.textContent = s ?? ''; return d.innerHTML; }

/* ================= 初始化 ================= */
window.addEventListener('DOMContentLoaded', async () => {
  await cfgLoad();
  if (!cfg().ui_lang) await cfgSet({ ui_lang: await pickLanguage() });
  setLang(cfg().ui_lang);
  translateDom();
  fillStaticSelects();
  initNav(); initHome(); initSubtitle(); initTest(); initHistory();
  initSettings(); initHelp();
  fillDevices();
  navigator.mediaDevices.addEventListener('devicechange', fillDevices);
  pushOverlayCfg();
  api.overlayShow(true);
  api.overlayThrough(cfg().click_through);
  startMic();
  api.on('ptt', onPtt);
  api.on('hotkey-captured', onHotkeyCaptured);
  api.on('rt-state', onRtState);
  api.on('rt-ev', onRtEv);
  api.on('tts', onTts);
  api.on('ovctl', onOverlayCtl);
  api.on('hotkey-invalid', () => toast(tr('热键未识别，请到设置页重新录入')));
  api.on('tray', onTrayAction);
  state.mateOn = cfg().mate_enabled !== false;
  $('mateOn').checked = state.mateOn;
  api.toOverlay('mate-state', state.mateOn);
  pushTrayState();
  setStatus(tr('就绪'));
  refreshChips(); syncMode();
  if (cfg().mode === 'server') refreshServer();
  setInterval(() => { if (cfg().mode === 'server' && cfg().has_token) refreshAccount(); }, 60000);
});

function pickLanguage() {
  return new Promise((res) => {
    const d = document.createElement('div');
    d.className = 'modal';
    d.innerHTML = `<div class="modal-card"><b>选择界面语言 / Choose language</b>
      <select id="lg" class="input"><option value="zh">中文</option><option value="en">English</option></select>
      <button id="lgok" class="btn-primary">OK</button></div>`;
    document.body.appendChild(d);
    d.querySelector('#lgok').onclick = () => { const v = d.querySelector('#lg').value; d.remove(); res(v); };
  });
}
function translateDom() {
  document.querySelectorAll('[data-t]').forEach(el => { el.textContent = tr(el.dataset.t); });
  document.querySelectorAll('[data-tp]').forEach(el => { el.placeholder = tr(el.dataset.tp); });
}
function fillStaticSelects() {
  document.querySelectorAll('.lang-sel').forEach(s => {
    s.innerHTML = LANGS.map(([v, n]) => `<option value="${v}">${n}</option>`).join('');
  });
  document.querySelectorAll('.voice-sel').forEach(s => {
    s.innerHTML = VOICES.map(v => `<option>${v}</option>`).join('');
  });
}

/* ================= 导航 ================= */
function initNav() {
  document.querySelectorAll('.nav').forEach(b => { b.onclick = () => switchPage(b.dataset.p); });
  $('btnMin').onclick = () => api.winMin();
  $('btnClose').onclick = () => { api.winClose(); };
  $('sideAcct').onclick = () => switchPage('settings');
}
function switchPage(p) {
  document.querySelectorAll('.nav').forEach(b => b.classList.toggle('on', b.dataset.p === p));
  document.querySelectorAll('.page').forEach(s => s.classList.toggle('on', s.id === 'pg-' + p));
  if (p === 'history') renderHistory();
}

/* ================= 翻译页 ================= */
function initHome() {
  $('btnRun').onclick = () => setRunning(!state.running);
  $('mateOn').onchange = () => setMateOn($('mateOn').checked);
  $('playAudio').onchange = async () => { await cfgSet({ play_audio: $('playAudio').checked }); applyLiveSettings('play_audio'); };
  $('voiceSel').onchange = async () => { await cfgSet({ voice: $('voiceSel').value }); applyLiveSettings('voice'); };
  $('qTargetMate').onchange = async () => { await cfgSet({ target_mate: $('qTargetMate').value }); applyLiveSettings('target_mate'); };
  $('qTargetSelf').onchange = async () => { await cfgSet({ target_self: $('qTargetSelf').value }); applyLiveSettings('target_self'); };
  $('btnFeedClear').onclick = () => { state.segs.clear(); $('feed').querySelectorAll('.fi').forEach(e => e.remove()); $('feedEmpty').classList.remove('hidden'); };
  $('acctGo').onclick = () => switchPage('settings');
  syncQuick();
}
function syncQuick() {
  const c = cfg();
  $('playAudio').checked = !!c.play_audio;
  $('voiceSel').value = c.voice || 'Cherry';
  $('qTargetMate').value = c.target_mate; $('qTargetSelf').value = c.target_self;
}
function hotkeyName() {
  const hk = cfg().hotkey || {};
  if (hk.device === 'mouse')
    return tr('鼠标') + ' ' + ({ 1: tr('左键'), 2: tr('右键'), 3: tr('中键'), 4: 'X1', 5: 'X2', x1: 'X1', x2: 'X2', middle: tr('中键') }[hk.key] || hk.key);
  return tr('键盘') + ' #' + hk.key;
}
function langName(v) { return (LANGS.find(l => l[0] === v) || [v, v])[1]; }
function refreshChips() {
  const c = cfg();
  $('homeChips').innerHTML =
    `${escapeHtml(tr('队友'))} → <b>${escapeHtml(langName(c.target_mate))}</b> · ${escapeHtml(tr('按住'))} <span class="kbd">${escapeHtml(hotkeyName())}</span> ${escapeHtml(tr('说话'))} → <b>${escapeHtml(langName(c.target_self))}</b><br>` +
    `${escapeHtml(c.play_audio ? tr('播报译文') + ' · ' + c.voice : tr('仅文字字幕'))}`;
  $('pttHint').textContent = `${tr('按住')} ${hotkeyName()} ${tr('说话，松开自动翻译并复制')}`;
  $('hkLabel').textContent = hotkeyName();
}

let _ovVisible = true;
function pushTrayState() {
  api.trayState({ running: state.running, through: cfg().click_through,
                  locked: cfg().overlay_locked, ovVisible: _ovVisible });
}
function onTrayAction(a) {
  if (a === 'toggle-run') setRunning(!state.running);
  else if (a === 'toggle-overlay') { _ovVisible = !_ovVisible; api.overlayShow(_ovVisible); pushTrayState(); }
  else if (a === 'toggle-through') setThrough(!cfg().click_through);
  else if (a === 'toggle-lock') setLocked(!cfg().overlay_locked);
  else if (a === 'open-settings') switchPage('settings');
}

function canStart() {
  const c = cfg();
  if (c.mode === 'server') {
    if (!/^https?:\/\//.test(c.server_url || '')) { toast(tr('请先在设置页填写服务器地址')); switchPage('settings'); return false; }
    if (!c.has_token) { toast(tr('请先登录账号或使用游客模式')); switchPage('settings'); return false; }
  } else if (!c.has_dash_key) { toast(tr('请先在设置页填写 API Key')); switchPage('settings'); return false; }
  return true;
}
function setRunning(on) {
  if (on && !canStart()) return;
  state.running = on;
  $('btnRun').classList.toggle('on', on);
  $('homeStatus').textContent = on ? tr('翻译中…') : tr('准备就绪');
  $('tbState').classList.toggle('live', on);
  $('tbState').querySelector('b').textContent = on ? tr('翻译中') : tr('待机');
  cfgSet({ running: on });
  if (on) startSessions(); else stopSessions();
  setStatus(on ? tr('翻译中') : tr('就绪'));
  pushTrayState();
}
function setMateOn(on) {
  state.mateOn = on;
  $('mateOn').checked = on;
  cfgSet({ mate_enabled: on });
  api.toOverlay('mate-state', on);
  if (!state.running) return;
  if (on) startMate(); else { api.sessionStop('mate'); loopback.stop(); setChan('mate', 'off'); }
}
async function startMate() {
  api.sessionStart('mate');
  try { await loopback.startLoopback(); loopback.setFilter(cfg().mate_filter); }
  catch (e) { setStatus(tr('回环采集失败: ') + e.message); }
}
function startSessions() {
  state.speakerMap = Object.create(null); state.nextFriend = 1;
  api.sessionStart('self');
  if (state.mateOn) startMate();
}
function stopSessions() {
  api.sessionStop('mate'); api.sessionStop('self');
  loopback.stop();
  setChan('mate', 'off'); setChan('self', 'off');
}
function restartSessions() {
  if (!(state.running || state.testSelf)) return;
  api.sessionStop('mate'); api.sessionStop('self');
  api.sessionStart('self');
  if (state.running && state.mateOn) api.sessionStart('mate');
}

/* ================= 会话事件 ================= */
const CH_TEXT = { connecting: '连接中…', ready: '已就绪', reconnecting: '重连中…', closed: '已断开', off: '—' };
function setChan(stream, s) {
  state.chState[stream] = s;
  const el = $(stream === 'mate' ? 'chMate' : 'chSelf');
  el.classList.toggle('ready', s === 'ready');
  el.classList.toggle('busy', s === 'connecting' || s === 'reconnecting');
  el.classList.toggle('err', s === 'closed');
  $(stream === 'mate' ? 'stMate' : 'stSelf').textContent = tr(CH_TEXT[s] || s);
}
function onRtState({ stream, state: s }) {
  setChan(stream, s);
  if (stream === 'self') $('chStatus').textContent = tr(CH_TEXT[s] || s);
  if (s === 'ready') setStatus(state.running ? tr('翻译中') : tr('就绪'));
}
const TERMINAL_MSG = {
  unauthorized: '登录已失效，请重新登录', insufficient_balance: '额度已用完，请充值后重新开始',
  banned: '账号已被封禁', kicked: '会话已被服务器断开',
};
function onRtEv({ stream, ev }) {
  if (ev.type === 'error') {
    const who = stream === 'mate' ? tr('队友') : tr('自翻');
    if (TERMINAL_MSG[ev.code]) {
      toast(tr(TERMINAL_MSG[ev.code]));
      setStatus(`[${who}] ${tr(TERMINAL_MSG[ev.code])}`);
      if (ev.code === 'unauthorized' || ev.code === 'banned')
        api.srvLogout().then((c) => { cfgReplace(c); syncMode(); });
      if (state.running) setRunning(false);
      refreshAccount();
    } else setStatus(`[${who}] ${ev.msg || ev.code}`);
    return;
  }
  if (ev.type === 'usage') { applyUsage(ev); return; }
  if (ev.type === 'seg' || ev.type === 'turn_done') handleSeg(stream, ev);
}
function onTts({ pcm }) {
  if (cfg().play_audio) player.play(pcm, cfg().output_device || undefined);
}

function speakerName(stream, raw) {
  if (stream === 'self') return { name: tr('我'), color: SELF_COLOR };
  const key = raw == null || raw === '' ? '_' : String(raw);
  if (!state.speakerMap[key]) {
    const n = state.nextFriend++;
    state.speakerMap[key] = { name: `${tr('队友')}${n}`, color: SPEAKER_COLORS[(n - 1) % SPEAKER_COLORS.length] };
  }
  return state.speakerMap[key];
}

function handleSeg(stream, ev) {
  // 老版本服务端无 id：按说话人归并（向后兼容）
  const id = stream + '|' + (ev.id || ('spk:' + (ev.speaker ?? '')));
  let seg = state.segs.get(id);
  if (ev.type === 'turn_done') { if (seg) finishSeg(seg); return; }
  // item id 唯一：定稿后迟到的事件（如原文 completed 晚于译文 done）原地更新，不另起气泡；
  // 仅无 id 的老协议按说话人归并时，定稿后才开新段
  if (!seg || (seg.done && !ev.id)) {
    const who = speakerName(stream, ev.speaker);
    seg = { id, stream, name: who.name, color: who.color, src: '', dst: '', done: false, el: null };
    state.segs.set(id, seg);
    if (state.segs.size > 200) state.segs.delete(state.segs.keys().next().value);
  }
  if (ev.src_delta) seg.src += ev.src_delta; else if (ev.src) seg.src = ev.src;
  if (ev.dst_delta) seg.dst += ev.dst_delta; else if (ev.dst) seg.dst = ev.dst;
  const late = seg.done, live = !late && !ev.dst_final;
  api.toOverlay('seg', { key: id, name: seg.name, color: seg.color, src: seg.src, dst: seg.dst,
                         final: !live, late });
  if (ev.type === 'seg' && stream === 'mate' && !late) api.toOverlay('mate_tick');
  renderFeedItem(seg, live);
  if (ev.dst_final) finishSeg(seg);
}
function finishSeg(seg) {
  if (seg.done) return;
  seg.done = true;
  renderFeedItem(seg, false);
  api.toOverlay('seg-done', { key: seg.id });
  const text = seg.dst.trim();
  if (!text) return;
  if (seg.stream === 'self' || cfg().auto_copy) api.clip(text);
  if (seg.stream === 'self') setStatus(tr('已复制：') + text.slice(0, 40));
  recordHistory(seg);
}
function renderFeedItem(seg, live) {
  const feed = $('feed');
  if (!seg.el) {
    seg.el = document.createElement('div');
    seg.el.className = 'fi';
    seg.el.style.setProperty('--c', seg.color);
    seg.el.innerHTML = '<div class="who"></div><div class="dst"></div><div class="src"></div>';
    seg.el.querySelector('.who').textContent = seg.name;
    feed.appendChild(seg.el);
    $('feedEmpty').classList.add('hidden');
    const items = feed.querySelectorAll('.fi');
    if (items.length > 60) items[0].remove();
  }
  seg.el.classList.toggle('live', live);
  seg.el.querySelector('.dst').textContent = seg.dst || '…';
  seg.el.querySelector('.src').textContent = seg.src;
  const nearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;
  if (nearBottom) feed.scrollTop = feed.scrollHeight;
}

/* ================= PTT（带前置缓冲 + 尾音延迟提交） ================= */
function onPtt(on) {
  state.ptt = on;
  $('pttLamp').classList.toggle('on', on);
  $('pttLamp').querySelector('span').textContent = on ? tr('说话中…松开完成') : tr('按住绑定的热键说话');
  $('pttHint').classList.toggle('on', on);
  api.toOverlay('ptt', on);
  if (on) {
    if (!state.running && !state.testSelf) { setStatus(tr('未开启翻译')); return; }
    if (state.chState.self !== 'ready') { setStatus(tr('自翻通道未就绪，检查 Key/网络')); return; }
    if (state.tailTimer) {       // 上一句还在收尾：立即提交，再开新句
      clearTimeout(state.tailTimer); state.tailTimer = null;
      api.sessionCommit('self');
    }
    api.sessionClear('self');
    for (const c of state.preroll) api.sessionAudio('self', c);
    state.preroll = [];
    state.pttSending = true; state.pttAt = Date.now();
    setStatus(tr('说话中…'));
  } else if (state.pttSending) {
    const short = Date.now() - state.pttAt < PTT_MIN_MS;
    state.tailTimer = setTimeout(() => {
      state.tailTimer = null; state.pttSending = false;
      if (short) { api.sessionClear('self'); setStatus(tr('按键太短，已忽略')); return; }
      api.sessionCommit('self');
      setStatus(tr('翻译中…'));
    }, short ? 0 : PTT_TAIL_MS);
  }
}

/* ================= 音频采集 ================= */
async function startMic() {
  mic.onLevel = (l) => { const p = levelPct(l) + '%'; $('micBar').style.width = p; $('micBar2').style.width = p; };
  mic.onChunk = (pcm) => {
    const b = b64(pcm);
    if ((state.running || state.testSelf) && state.pttSending) api.sessionAudio('self', b);
    else { state.preroll.push(b); if (state.preroll.length > PREROLL_CHUNKS) state.preroll.shift(); }
  };
  mic.onEnded = () => setStatus(tr('麦克风已断开（设备拔出？），请在设置页重选'));
  try { await mic.startMic(cfg().mic_device || undefined); }
  catch (e) { setStatus(tr('麦克风失败: ') + e.message); }
}
loopback.onLevel = (l) => { const p = levelPct(l) + '%'; $('mateBar').style.width = p; $('mateBar2').style.width = p; };
loopback.onEnded = () => {
  setStatus(tr('系统音频回环已断开'));
  if (state.running && state.mateOn) {
    // 尝试自动恢复一次（例如默认输出设备切换后）
    loopback.startLoopback()
      .then(() => { loopback.setFilter(cfg().mate_filter); setStatus(tr('回环已恢复')); })
      .catch(() => setStatus(tr('回环恢复失败，请停止后重新开始翻译')));
  }
};
loopback.onChunk = (pcm) => {
  if (!state.running || !state.mateOn) return;
  let out = pcm;
  // TTS 回声抑制：播报走默认输出时，播报期间给回环送静音（避免把自己的播报再翻一遍）
  if (!cfg().output_device && player.isActive()) out = new ArrayBuffer(pcm.byteLength);
  api.sessionAudio('mate', b64(out));
};

/* ================= 悬浮窗控制 ================= */
function onOverlayCtl(a) {
  if (a === 'ready') {
    pushOverlayCfg(); api.toOverlay('mate-state', state.mateOn);
    api.overlayThrough(cfg().click_through);
  } else if (a === 'mate') setMateOn(!state.mateOn);
  else if (a === 'pin') setThrough(!cfg().click_through);
  else if (a === 'lock') setLocked(!cfg().overlay_locked);
  else if (a === 'hide') { _ovVisible = false; api.overlayShow(false); pushTrayState(); }
}
async function setThrough(on) {
  await cfgSet({ click_through: on });
  api.overlayThrough(on); $('sThrough').checked = on; pushTrayState(); pushOverlayCfg();
}
async function setLocked(on) {
  await cfgSet({ overlay_locked: on });
  const el = $('sLock'); if (el) el.checked = on;
  pushOverlayCfg(); pushTrayState();
}
function pushOverlayCfg() {
  const c = cfg();
  api.toOverlay('cfg', {
    max_bubbles: c.max_bubbles, overlay_src_color: c.overlay_src_color,
    overlay_dst_color: c.overlay_dst_color, overlay_opacity: c.overlay_opacity,
    overlay_font_scale: c.overlay_font_scale, overlay_fade_sec: c.overlay_fade_sec,
    overlay_show_src: c.overlay_show_src, click_through: c.click_through,
    overlay_locked: c.overlay_locked, ui_lang: c.ui_lang,
  });
}
function setStatus(s) { api.toOverlay('status', s); }

/* ================= 字幕页 ================= */
function initSubtitle() {
  const c = cfg();
  $('btnOvShow').onclick = () => { _ovVisible = !_ovVisible; api.overlayShow(_ovVisible); pushTrayState(); };
  $('sThrough').checked = !!c.click_through;
  $('sThrough').onchange = () => setThrough($('sThrough').checked);
  $('sLock').checked = !!c.overlay_locked;
  $('sLock').onchange = () => setLocked($('sLock').checked);
  const slider = (id, valId, key, toCfg, fromCfg, fmt) => {
    $(id).value = fromCfg(c[key]);
    const show = () => { $(valId).textContent = fmt(+$(id).value); };
    show();
    $(id).oninput = () => { show(); cfgSet({ [key]: toCfg(+$(id).value) }).then(() => { pushOverlayCfg(); syncPreview(); }); };
  };
  slider('sOpacity', 'opVal', 'overlay_opacity', v => v / 100, v => Math.round(v * 100), v => v + '%');
  slider('sFont', 'fontVal', 'overlay_font_scale', v => v / 100, v => Math.round(v * 100), v => v + '%');
  slider('sFade', 'fadeVal', 'overlay_fade_sec', v => v, v => v, v => v ? v + 's' : tr('不淡出'));
  slider('sMaxLines', 'maxVal', 'max_bubbles', v => v, v => v, v => String(v));
  $('sShowSrc').checked = c.overlay_show_src !== false;
  $('sShowSrc').onchange = async () => { await cfgSet({ overlay_show_src: $('sShowSrc').checked }); pushOverlayCfg(); syncPreview(); };
  $('srcColor').value = c.overlay_src_color; $('dstColor').value = c.overlay_dst_color;
  $('srcColor').oninput = async () => { await cfgSet({ overlay_src_color: $('srcColor').value }); pushOverlayCfg(); syncPreview(); };
  $('dstColor').oninput = async () => { await cfgSet({ overlay_dst_color: $('dstColor').value }); pushOverlayCfg(); syncPreview(); };
  syncPreview();
}
function syncPreview() {
  const c = cfg(), pv = $('ovPreview');
  pv.style.backgroundColor = `rgba(10,14,20,${c.overlay_opacity})`;
  pv.querySelectorAll('.pv-dst').forEach(e => { e.style.color = c.overlay_dst_color; e.style.fontSize = (15 * c.overlay_font_scale) + 'px'; });
  pv.querySelectorAll('.pv-src').forEach(e => {
    e.style.color = c.overlay_src_color; e.style.fontSize = (12 * c.overlay_font_scale) + 'px';
    e.style.display = c.overlay_show_src === false ? 'none' : '';
  });
}

/* ================= 测试页 ================= */
function initTest() {
  $('btnTestSelf').onclick = () => {
    if (!state.testSelf && !canStart()) return;
    state.testSelf = !state.testSelf;
    $('btnTestSelf').textContent = state.testSelf ? tr('停止测试') : tr('开始测试（拉起自翻通道）');
    $('btnTestSelf').classList.toggle('running', state.testSelf);
    if (state.testSelf) api.sessionStart('self');
    else if (!state.running) { api.sessionStop('self'); setChan('self', 'off'); }
  };
  $('btnBeep').onclick = () => player.beep(cfg().output_device || undefined);
}

/* ================= 记录页 ================= */
function recordHistory(seg) {
  const list = JSON.parse(localStorage.getItem('hist') || '[]');
  list.unshift({ t: Date.now(), who: seg.name, c: seg.color, dst: seg.dst.trim(), src: seg.src.trim() });
  if (list.length > 500) list.length = 500;
  localStorage.setItem('hist', JSON.stringify(list));
}
function initHistory() {
  $('btnClearHist').onclick = () => { if (confirm(tr('清空全部翻译记录？'))) { localStorage.removeItem('hist'); renderHistory(); } };
  $('histQ').oninput = renderHistory;
  $('histList').addEventListener('click', (e) => {
    const b = e.target.closest('[data-copy]'); if (!b) return;
    const list = JSON.parse(localStorage.getItem('hist') || '[]');
    const h = list[+b.dataset.copy]; if (h) { api.clip(h.dst); toast(tr('已复制')); }
  });
}
function renderHistory() {
  const q = $('histQ').value.trim().toLowerCase();
  const list = JSON.parse(localStorage.getItem('hist') || '[]');
  const pad = (n) => String(n).padStart(2, '0');
  const rows = list.map((h, i) => ({ h, i }))
    .filter(({ h }) => !q || (h.dst + ' ' + (h.src || '')).toLowerCase().includes(q));
  $('histList').innerHTML = rows.length ? rows.map(({ h, i }) => {
    const d = new Date(h.t);
    return `<div class="hi"><span class="t">${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}</span>
      <span class="who" style="color:${/^#[0-9a-f]{6}$/i.test(h.c || '') ? h.c : '#ffd479'}">${escapeHtml(h.who)}</span>
      <div class="tx"><div>${escapeHtml(h.dst)}</div>${h.src ? `<small>${escapeHtml(h.src)}</small>` : ''}</div>
      <button data-copy="${i}" title="copy"><svg><use href="#i-copy"/></svg></button></div>`;
  }).join('') : `<div class="empty">${escapeHtml(tr('暂无记录'))}</div>`;
}

/* ================= 设置页 ================= */
async function fillDevices() {
  const devs = await navigator.mediaDevices.enumerateDevices();
  const fill = (sel, list, def, cur) => {
    sel.innerHTML = `<option value="">${escapeHtml(def)}</option>` +
      list.map(d => `<option value="${escapeHtml(d.deviceId)}">${escapeHtml(d.label || tr('设备'))}</option>`).join('');
    sel.value = list.some(d => d.deviceId === cur) ? cur : '';
  };
  fill($('sMic'), devs.filter(d => d.kind === 'audioinput' && d.deviceId !== 'default' && d.deviceId !== 'communications'), tr('默认麦克风'), cfg().mic_device);
  fill($('sOut'), devs.filter(d => d.kind === 'audiooutput' && d.deviceId !== 'default' && d.deviceId !== 'communications'), tr('默认扬声器'), cfg().output_device);
}

function initSettings() {
  const c = cfg();
  $('sUiLang').value = c.ui_lang || 'zh';
  $('sMode').value = c.mode;
  // dash_key 不下发：已保存则留空并提示，输入新值才覆盖
  $('sKey').value = '';
  $('sKey').placeholder = c.has_dash_key ? tr('已保存（输入新 Key 覆盖）') : 'sk-xxxx';
  $('sUrl').value = c.dash_ws_url; $('sModel').value = c.dash_model;
  $('sServer').value = c.server_url;
  $('sUser').value = c.username || '';
  $('sTargetMate').value = c.target_mate; $('sTargetSelf').value = c.target_self;
  $('sPlayAudio').checked = !!c.play_audio;
  $('sAutoCopy').checked = !!c.auto_copy;
  $('sMateFilter').checked = !!c.mate_filter;
  $('sSilence').value = c.mate_silence_ms || 800;
  $('silVal').textContent = $('sSilence').value + 'ms';

  $('btnTestConn').onclick = testConn;
  $('btnRecKey').onclick = () => { $('hkLabel').textContent = tr('请按下按键或鼠标键…(Esc 取消)'); api.recordHotkey(); };

  // —— 服务器账号：登录 / 注册 / 游客 / 退出 ——
  const creds = () => ({ username: $('sUser').value.trim(), password: $('sPass').value });
  $('btnLogin').onclick = () => authFetch('/api/auth/login', creds());
  $('btnRegister').onclick = () => authFetch('/api/auth/register', creds());
  $('btnGuest').onclick = () => {
    // 设备号：首次生成并持久化，游客模式据此复用同一账号
    let did = localStorage.getItem('vt_device');
    if (!did) { did = crypto.randomUUID(); localStorage.setItem('vt_device', did); }
    authFetch('/api/auth/guest', { device_id: did });
  };
  $('btnLogout').onclick = async () => {
    cfgReplace(await api.srvLogout());
    if (state.running) setRunning(false);
    acctMsg(tr('已退出登录'));
    syncMode();
  };

  // 全部控件 → 即时保存
  const map = {
    sUiLang: 'ui_lang', sMode: 'mode', sKey: 'dash_key', sUrl: 'dash_ws_url',
    sModel: 'dash_model', sServer: 'server_url', sTargetMate: 'target_mate',
    sTargetSelf: 'target_self', sMic: 'mic_device', sOut: 'output_device',
  };
  for (const [id, key] of Object.entries(map)) {
    $(id).onchange = async () => {
      let v = $(id).value;
      if (key === 'server_url') v = v.trim().replace(/\/+$/, '');
      // dash_key 留空 = 不改动已保存的 Key（主进程也会忽略空值）
      if (key === 'dash_key' && !String(v).trim()) return;
      await cfgSet({ [key]: v }); applyLiveSettings(key);
      if (key === 'dash_key') { $(id).value = ''; $(id).placeholder = tr('已保存（输入新 Key 覆盖）'); }
    };
  }
  for (const [id, key] of Object.entries({ sPlayAudio: 'play_audio', sAutoCopy: 'auto_copy', sMateFilter: 'mate_filter' })) {
    $(id).onchange = async () => { await cfgSet({ [key]: $(id).checked }); applyLiveSettings(key); };
  }
  $('sSilence').oninput = () => { $('silVal').textContent = $('sSilence').value + 'ms'; };
  $('sSilence').onchange = async () => { await cfgSet({ mate_silence_ms: +$('sSilence').value }); applyLiveSettings('mate_silence_ms'); };
}

function acctMsg(text, kind = '') {
  const st = $('acctStatus'); st.textContent = text; st.className = 'conn-res ' + kind;
}

/* —— 服务器账号认证（登录/注册/游客共用；请求由主进程代发，token 不进页面） —— */
async function authFetch(path, body) {
  if (!/^https?:\/\//.test(cfg().server_url || '')) return acctMsg(tr('服务器地址须以 http(s):// 开头'), 'bad');
  if (body.username !== undefined && (!body.username || !body.password)) return acctMsg(tr('请输入用户名和密码'), 'bad');
  acctMsg('…');
  const r = await api.srv(path, 'POST', body);
  if (!r.ok) return acctMsg(r.data.msg || ('HTTP ' + r.status), 'bad');
  $('sPass').value = '';
  await cfgLoad();
  renderAccount(r.data);
  acctMsg(tr('登录成功'), 'ok');
  syncMode();
  restartSessions();
}

/* —— 账号信息 —— */
const acct = { money_cents: 0, credit_seconds: 0, trial_seconds: 0, sub_active: false, price: 30, username: '' };
function renderAccount(d) {
  if (d) Object.assign(acct, {
    username: d.username, money_cents: d.money_cents, credit_seconds: d.credit_seconds,
    trial_seconds: d.trial_active ? d.trial_seconds : 0, sub_active: !!d.sub_active,
    sub_expire_at: d.sub_expire_at, price: d.price_fen_per_minute || acct.price });
  const logged = cfg().has_token;
  $('acctName').textContent = logged ? (acct.username === '游客' ? tr('游客') : acct.username || cfg().username || tr('游客')) : tr('未登录');
  if (!logged) {
    $('acctStats').innerHTML = `<div><em>${escapeHtml(tr('状态'))}</em><b>${escapeHtml(tr('未登录'))}</b></div>`;
    $('sideBal').textContent = tr('未登录');
    return;
  }
  const dt = (ts) => { const d = new Date(ts * 1000); return `${d.getMonth() + 1}/${d.getDate()}`; };
  const minsOfMoney = acct.price > 0 ? Math.floor(acct.money_cents / acct.price) : 0;
  $('acctStats').innerHTML = [
    [tr('余额'), `¥${(acct.money_cents / 100).toFixed(2)}`, minsOfMoney > 0],
    [tr('时长卡'), `${Math.floor(acct.credit_seconds / 60)}m`, acct.credit_seconds > 0],
    acct.sub_active ? [tr('订阅至'), dt(acct.sub_expire_at), true]
                    : [tr('免费额度'), `${Math.floor(acct.trial_seconds / 60)}m`, acct.trial_seconds > 0],
  ].map(([k, v, on]) => `<div class="${on ? 'on' : ''}"><em>${escapeHtml(k)}</em><b>${escapeHtml(v)}</b></div>`).join('');
  $('sideBal').textContent = acct.sub_active ? tr('订阅中') : `¥${(acct.money_cents / 100).toFixed(2)}`;
}
function applyUsage(ev) {
  Object.assign(acct, { money_cents: ev.money_cents ?? acct.money_cents,
    credit_seconds: ev.credit_seconds ?? acct.credit_seconds,
    trial_seconds: ev.trial_seconds ?? acct.trial_seconds, sub_active: ev.sub ?? acct.sub_active });
  renderAccount();
}
async function refreshAccount() {
  if (cfg().mode !== 'server' || !cfg().has_token) { renderAccount(); return; }
  const r = await api.srv('/api/me');
  if (r.ok) renderAccount(r.data);
  else if (r.status === 401) { cfgReplace(await api.srvLogout()); renderAccount(); acctMsg(tr('登录已失效，请重新登录'), 'bad'); }
}
async function refreshServer() {
  if (!/^https?:\/\//.test(cfg().server_url || '')) { renderAccount(); return; }
  const r = await api.srv('/api/meta');
  if (r.ok) {
    const a = (r.data.announcement || '').trim();
    $('announce').classList.toggle('hidden', !a);
    $('announceText').textContent = a;
    if (r.data.price_fen_per_minute) acct.price = r.data.price_fen_per_minute;
    $('btnRegister').disabled = r.data.allow_register === false;
    $('btnGuest').disabled = r.data.allow_guest === false;
  }
  refreshAccount();
}

let _snap = null;
function sessionSnap() {
  const c = cfg();
  return JSON.stringify([c.mode, c.has_dash_key, c.dash_ws_url, c.dash_model, c.server_url,
    c.target_mate, c.target_self, c.voice, c.play_audio, c.mate_silence_ms, c.has_token]);
}
async function applyLiveSettings(changedKey) {
  const c = cfg();
  syncQuick();
  $('sTargetMate').value = c.target_mate; $('sTargetSelf').value = c.target_self;
  $('sPlayAudio').checked = !!c.play_audio;
  loopback.setFilter(c.mate_filter);
  pushOverlayCfg(); refreshChips();

  if (changedKey === 'ui_lang') {
    if (confirm(tr('界面语言更改需重启生效，现在重启？'))) api.relaunch();
    return;
  }
  if (changedKey === 'mic_device') { startMic(); return; }
  if (changedKey === 'mode' || changedKey === 'server_url') {
    syncMode();
    if (c.mode === 'server') refreshServer(); else $('announce').classList.add('hidden');
  }
  // 影响会话的配置 → 原地重开（主进程自行读磁盘配置，不传密钥）
  const snap = sessionSnap();
  if (_snap !== null && snap !== _snap) restartSessions();
  _snap = snap;
}

function syncMode() {
  const direct = cfg().mode === 'direct';
  document.querySelectorAll('.direct-only').forEach(el => el.classList.toggle('hidden', !direct));
  document.querySelectorAll('.srv-only').forEach(el => el.classList.toggle('hidden', direct));
  $('tbMode').textContent = direct ? tr('直连') : tr('服务器');
  if (direct) $('sideBal').textContent = tr('本地直连');
  else renderAccount();
  $('btnLogout').classList.toggle('hidden', !cfg().has_token);
  if (_snap === null) _snap = sessionSnap();
}

function onHotkeyCaptured(spec) {
  if (!spec) { refreshChips(); return; }   // Esc/超时取消：恢复原标签
  cfgSet({ hotkey: spec }).then(() => { refreshChips(); toast(tr('热键已设置：') + hotkeyName()); });
}

async function testConn() {
  const res = $('connRes'); res.textContent = tr('测试中…'); res.className = 'conn-res';
  if (cfg().mode === 'direct') {
    // URL/模型用主进程保存的配置；仅把输入框里的新 Key（若有）带去测试
    const r = await api.sessionTest({ dash_key: $('sKey').value.trim() });
    res.textContent = r.ok ? `✓ ${tr('连接成功')} ${r.ms}ms` : '✗ ' + r.msg;
    res.className = 'conn-res ' + (r.ok ? 'ok' : 'bad');
  } else {
    const r = await api.srv('/api/meta');
    res.textContent = r.ok ? `✓ ${tr('服务器可达')} ${r.ms}ms` : '✗ ' + (r.data.msg || 'HTTP ' + r.status);
    res.className = 'conn-res ' + (r.ok ? 'ok' : 'bad');
    if (r.ok) refreshServer();
  }
}

/* ================= 工具 ================= */
let _toastT = null;
function toast(msg) {
  const t = $('toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(_toastT); _toastT = setTimeout(() => t.classList.remove('show'), 2600);
}

/* ================= 帮助 ================= */
function initHelp() {
  $('tutorialText').textContent = cfg().ui_lang === 'en' ? TUTORIAL_EN : TUTORIAL;
  $('contactText').textContent = CONTACT;
}
const TUTORIAL = `═══════ VTranslate 使用指南 ═══════

【一、连接方式】（设置 → 连接）
● 计费服务器（推荐）：填写服务器地址（如 https://api.example.com），
  注册/登录账号或点「游客模式」即可使用；余额、时长卡、订阅在服务器端管理。
● 直连 DashScope：填自己的阿里云百炼 API Key（按你的账号计费）。

【二、开局三步】
1. 游戏画面设为「无边框窗口」（独占全屏会盖住悬浮字幕）
2. 点首页中间的电源按钮开始翻译，悬浮窗实时显示队友发言（自动区分 队友1/队友2…）
3. 按住热键（默认鼠标侧键 X1）说中文，松开即翻译成英文并自动复制——
   游戏内 Enter → Ctrl+V → Enter 发送

【三、让翻译更准】
- 已内置 230+ 条（中英双向近 500 条）Valorant 术语热词（特工/地图/点位/枪械/经济/战术黑话），
  服务器模式下管理员还可在后台在线补充
- 松开热键后程序会多收 0.35 秒尾音再提交，按下前 0.3 秒的声音也会补发，不吞字
- 队友声音杂？开启「语音频段过滤」削弱枪声、脚步、爆炸声
- 麦克风尽量贴近嘴，关闭游戏内「按键说话」以外的麦克风增强
- 游戏播报语音（Spike planted 等）属人声无法区分，属正常

【四、字幕窗】
- 「穿透」开启后鼠标点不到字幕（游戏内推荐开），托盘菜单可关闭
- 字号、背景透明度、自动淡出、是否显示原文都可在「字幕」页调整，拖动窗口位置会被记住

【五、常见问题】
Q 没有翻译输出？  A 设置页「测试连接」；服务器模式检查是否已登录、额度是否用完
Q 进游戏热键失效？  A Valorant 以管理员权限运行时，普通程序的全局热键会被拦截，
  请右键 exe「以管理员身份运行」
Q 会不会封号？  A 不会。本程序只读写音频设备，不接触游戏进程/内存/文件/网络数据，
  与录屏软件同级`;
const TUTORIAL_EN = `═══════ VTranslate Guide ═══════

1. Connection (Settings → Connection)
 • Billing server (recommended): enter the server URL, then log in / register / use Guest mode.
 • Direct DashScope: use your own Alibaba Model Studio API key.

2. Quick start
 1) Set the game to Borderless Windowed (exclusive fullscreen hides overlays).
 2) Press the power button — teammates' voice is translated live on the overlay.
 3) Hold your PTT key (default mouse X1), speak, release: the translation is copied to clipboard.

3. Accuracy tips
 • 230+ built-in Valorant hotwords per direction (agents, maps, callouts, weapons, economy, slang).
 • 0.3s pre-roll and 0.35s tail are captured around your PTT press so words aren't clipped.
 • Enable the speech-band filter to reduce gunfire / footsteps noise.

4. FAQ
 • Hotkey not working in game? Run VTranslate as administrator (Valorant runs elevated).
 • Ban risk? None — the app only touches audio devices, never the game process.`;
const CONTACT = `问题反馈 / 商务合作：
QQ 群：在官网或推广页获取最新群号
邮箱：support@example.com（请替换为你的联系方式）
开源仓库：https://gitee.com/chunfff/v-translation`;
