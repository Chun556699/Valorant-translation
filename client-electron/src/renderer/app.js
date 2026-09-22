/* 主窗逻辑：导航/页面/设置热更新/PTT/翻译会话编排/悬浮窗转发 */

const $ = (id) => document.getElementById(id);
const state = {
  running: false, pttOn: false, testSelf: false,
  mateOn: true, chState: { mate: 'off', self: 'off' },
  // 无原型对象：服务器下发的 speaker id 可能是 "__proto__" 等，防原型污染
  speakerMap: Object.create(null), nextFriend: 1,
  selfAccum: '', lastDstSelf: '',
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

/* ================= 初始化 ================= */
window.addEventListener('DOMContentLoaded', async () => {
  await cfgLoad();
  if (!cfg().ui_lang) {
    // 首次启动：选语言
    const l = await pickLanguage();
    await cfgSet({ ui_lang: l });
  }
  setLang(cfg().ui_lang);
  translateDom();
  initNav(); initHome(); initSubtitle(); initTest();
  initSettings(); initPages();
  fillDevices();
  pushOverlayCfg();
  api.overlayShow(true);
  api.overlayThrough(cfg().click_through);
  wireIpc();
  startMic();
  api.on('ptt', onPtt);
  api.on('hotkey-captured', onHotkeyCaptured);
  api.on('rt-state', onRtState);
  api.on('rt-ev', onRtEv);
  api.on('tts', onTts);
  api.on('ovctl', onOverlayCtl);
  api.on('hotkey-invalid', () =>
    toast('热键未识别，请到设置页重新录入'));
  api.on('tray', onTrayAction);
  pushTrayState();
  setStatus('就绪');
  refreshChips();
});

function pickLanguage() {
  return new Promise((res) => {
    const d = document.createElement('div');
    d.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.3);display:flex;align-items:center;justify-content:center;z-index:99';
    d.innerHTML = `<div style="background:#fff;border-radius:12px;padding:22px 26px;min-width:230px;box-shadow:0 10px 40px rgba(0,0,0,.18)">
      <div style="font-size:14px;font-weight:600;margin-bottom:12px">选择界面语言 / Choose language</div>
      <select id="lg" style="width:100%;padding:8px;border:1px solid #e3e6ea;border-radius:8px;margin-bottom:14px">
        <option value="zh">中文</option><option value="en">English</option></select>
      <button id="lgok" style="width:100%;padding:8px;border:0;border-radius:8px;background:#ff4655;color:#fff;font-weight:600;cursor:pointer">OK</button></div>`;
    document.body.appendChild(d);
    d.querySelector('#lgok').onclick = () => { const v = d.querySelector('#lg').value; d.remove(); res(v); };
  });
}

function translateDom() {
  document.querySelectorAll('[data-t]').forEach(el => { el.textContent = tr(el.dataset.t || el.textContent); });
}

/* ================= 导航 ================= */
function initNav() {
  document.querySelectorAll('.nav').forEach(b => {
    b.onclick = () => switchPage(b.dataset.p);
  });
  $('btnMin').onclick = () => api.winMin();
  $('btnClose').onclick = () => { api.winClose(); toast(tr('已最小化到托盘，右键托盘图标可退出')); };
}
function switchPage(p) {
  document.querySelectorAll('.nav').forEach(b => b.classList.toggle('on', b.dataset.p === p));
  document.querySelectorAll('.page').forEach(s => s.classList.toggle('on', s.id === 'pg-' + p));
  if (p === 'history') renderHistory();
}

/* ================= 翻译页 ================= */
function initHome() {
  $('btnRun').onclick = () => setRunning(!state.running);
  $('playAudio').checked = !!cfg().play_audio;
  $('playAudio').onchange = async () => {
    await cfgSet({ play_audio: $('playAudio').checked });
    applyLiveSettings();
  };
  $('voiceSel').onchange = async () => {
    await cfgSet({ voice: $('voiceSel').value });
    applyLiveSettings();
  };
  $('voiceSel').value = cfg().voice || 'Cherry';
}

function refreshChips() {
  const hk = cfg().hotkey || {};
  const LEG = { x1: 'X1', x2: 'X2', middle: '中键' };
  const dev = hk.device === 'mouse' ? '鼠标' : '键盘';
  const kn = hk.device === 'mouse'
    ? ({ 3: '中键', 4: 'X1', 5: 'X2' }[hk.key] || LEG[hk.key] || hk.key)
    : hk.key;
  $('homeChips').textContent =
    `队友→${cfg().target_mate} · 按住${dev}${kn}自翻→${cfg().target_self} · ${cfg().play_audio ? '播报译文' : '仅文本'}`;
}

let _ovVisible = true;
function pushTrayState() {
  api.trayState({ running: state.running, through: cfg().click_through,
                  ovVisible: _ovVisible });
}
function onTrayAction(a) {
  if (a === 'toggle-run') setRunning(!state.running);
  else if (a === 'toggle-overlay') { _ovVisible = !_ovVisible; api.overlayShow(_ovVisible); pushTrayState(); }
  else if (a === 'toggle-through') {
    cfgSet({ click_through: !cfg().click_through }).then(() => {
      api.overlayThrough(cfg().click_through); pushTrayState();
    });
  }
  else if (a === 'open-settings') switchPage('settings');
}

function setRunning(on) {
  state.running = on;
  $('btnRun').textContent = on ? tr('停止翻译') : tr('开始翻译');
  $('btnRun').classList.toggle('running', on);
  $('homeStatus').textContent = on ? tr('翻译中…') : tr('准备就绪');
  $('tbDot').classList.toggle('on', on);
  $('homePill').textContent = cfg().mode === 'direct' ? '直连' : '服务器';
  cfgSet({ running: on });
  if (on) startSessions(); else stopSessions();
  setStatus(on ? tr('翻译中') : tr('就绪'));
  pushTrayState();
}

async function startSessions() {
  state.speakerMap = Object.create(null); state.nextFriend = 1;
  api.sessionStart('self');
  if (state.mateOn && cfg().mate_enabled) api.sessionStart('mate');
  if (cfg().mate_enabled) {
    try { await loopback.startLoopback(); loopback.setFilter(cfg().mate_filter); }
    catch (e) { setStatus(tr('回环采集失败: ') + e.message); }
  }
}
function stopSessions() {
  api.sessionStop('mate'); api.sessionStop('self');
  loopback.stop();
}

/* ================= 会话事件 ================= */
function onRtState({ stream, state: s }) {
  state.chState[stream] = s;
  $('chStatus').textContent = { connecting: '连接中…', ready: '通道就绪 ✓',
    reconnecting: '重连中…', closed: '已关闭', off: '' }[s] || s;
  if (s === 'ready') setStatus(state.running ? tr('翻译中') : tr('就绪'));
}
function onRtEv({ stream, ev }) {
  if (ev.type === 'error') {
    setStatus(`[${stream === 'mate' ? '队友' : '自翻'}] ${ev.msg || ev.code}`);
    return;
  }
  handleSeg(stream, ev);
}
function onTts({ pcm }) {
  if (cfg().play_audio) player.play(pcm, cfg().output_device || undefined);
}

function speakerName(stream, raw) {
  if (stream === 'self') return '我';
  if (!raw) return '队友';
  if (!state.speakerMap[raw]) {
    state.speakerMap[raw] = `好友${state.nextFriend++}`;
  }
  return state.speakerMap[raw];
}

function handleSeg(stream, ev) {
  const name = speakerName(stream, ev.speaker);
  api.toOverlay('seg', { stream, speaker: ev.speaker, name, ev });
  if (ev.type === 'seg') api.toOverlay('mate_tick');
  if (stream === 'self') {
    if (ev.dst_delta) state.selfAccum += ev.dst_delta;
    else if (ev.dst) state.selfAccum = ev.dst;
    if (ev.dst_final) {
      // delta 模式下 ev.dst 为空，用累计文本；复制 + 记录历史
      const text = state.selfAccum || ev.dst || '';
      if (text) {
        navigator.clipboard.writeText(text);
        recordHistory(stream, name, { dst_final: true, dst: text });
      }
      state.selfAccum = '';
      return;
    }
  } else if (ev.dst_final && cfg().auto_copy) {
    const text = ev.dst || '';
    if (text) navigator.clipboard.writeText(text);
  }
  recordHistory(stream, name, ev);
}

/* ================= PTT ================= */
function onPtt(on) {
  state.pttOn = on;
  $('pttLamp').classList.toggle('on', on);
  $('pttLamp').querySelector('span').textContent =
    on ? tr('说话中…松开完成') : tr('按住绑定的热键说话');
  api.toOverlay('ptt', on);
  if (on) {
    if (!state.running && !state.testSelf) { setStatus(tr('未开启翻译')); return; }
    if (state.chState.self !== 'ready') { setStatus(tr('自翻通道未就绪，检查 Key/网络')); return; }
    api.sessionClear('self');
    state.selfAccum = '';
  } else {
    api.sessionCommit('self');
    setStatus(state.running ? tr('翻译中') : tr('就绪'));
  }
}

/* ================= 音频采集 ================= */
async function startMic() {
  mic.onLevel = (l) => { $('micBar').style.width = Math.min(100, l * 400) + '%'; };
  mic.onChunk = (pcm) => {
    if ((state.running || state.testSelf) && state.pttOn)
      api.sessionAudio('self', b64(pcm));
  };
  mic.onEnded = () => setStatus(tr('麦克风已断开（设备拔出？），请在设置页重选'));
  try { await mic.startMic(cfg().mic_device || undefined); }
  catch (e) { setStatus(tr('麦克风失败: ') + e.message); }
}
loopback.onLevel = (l) => { $('mateBar').style.width = Math.min(100, l * 400) + '%'; };
loopback.onEnded = () => {
  setStatus(tr('系统音频回环已断开'));
  if (state.running) {
    // 尝试自动恢复一次（例如默认输出设备切换后）
    loopback.startLoopback()
      .then(() => { loopback.setFilter(cfg().mate_filter); setStatus(tr('回环已恢复')); })
      .catch(() => setStatus(tr('回环恢复失败，请停止后重新开始翻译')));
  }
};
loopback.onChunk = (pcm) => {
  if (!state.running || !cfg().mate_enabled) return;
  if (l_level(pcm) > 0.02) api.toOverlay('mate_tick');
  let out = pcm;
  // TTS 回声抑制：播报走默认输出时，播报期间给回环送静音
  if (!cfg().output_device && player.isActive())
    out = new ArrayBuffer(pcm.byteLength);
  api.sessionAudio('mate', b64(out));
};
function l_level(buf) {
  const a = new Int16Array(buf); let s = 0;
  for (let i = 0; i < a.length; i++) s += Math.abs(a[i]);
  return s / a.length / 32768;
}

/* ================= 悬浮窗控制 ================= */
function onOverlayCtl(a) {
  if (a === 'mate') {
    state.mateOn = !state.mateOn;
    cfgSet({ mate_enabled: state.mateOn });
    api.toOverlay('mate-state', state.mateOn);
    if (state.running) { state.mateOn ? api.sessionStart('mate') : api.sessionStop('mate'); }
  } else if (a === 'pin') {
    cfgSet({ click_through: !cfg().click_through }).then(() => {
      api.overlayThrough(cfg().click_through); pushTrayState();
    });
  } else if (a === 'hide') { _ovVisible = false; api.overlayShow(false); pushTrayState(); }
}
function pushOverlayCfg() {
  api.toOverlay('cfg', {
    max_bubbles: cfg().max_bubbles,
    overlay_src_color: cfg().overlay_src_color,
    overlay_dst_color: cfg().overlay_dst_color,
    overlay_opacity: cfg().overlay_opacity,
  });
}
function setStatus(s) { api.toOverlay('status', s); }

/* ================= 字幕页 ================= */
function initSubtitle() {
  $('btnOvShow').onclick = () => { _ovVisible = true; api.overlayShow(true); pushTrayState(); };
  const syncPin = () => { $('btnThrough').textContent = tr(cfg().click_through ? '穿透: 开' : '穿透: 关'); };
  syncPin();
  $('btnThrough').onclick = async () => {
    await cfgSet({ click_through: !cfg().click_through });
    api.overlayThrough(cfg().click_through); syncPin(); pushTrayState();
  };
  $('ovOpacity').value = Math.round(cfg().overlay_opacity * 100) + '%';
  $('ovOpacity').onchange = async () => {
    await cfgSet({ overlay_opacity: parseInt($('ovOpacity').value) / 100 });
    pushOverlayCfg();
  };
  $('srcColor').value = cfg().overlay_src_color;
  $('dstColor').value = cfg().overlay_dst_color;
  $('srcColor').oninput = async () => { await cfgSet({ overlay_src_color: $('srcColor').value }); $('pvSrc').style.color = $('srcColor').value; pushOverlayCfg(); };
  $('dstColor').oninput = async () => { await cfgSet({ overlay_dst_color: $('dstColor').value }); $('pvDst').style.color = $('dstColor').value; pushOverlayCfg(); };
}

/* ================= 测试页 ================= */
function initTest() {
  $('btnTestSelf').onclick = () => {
    state.testSelf = !state.testSelf;
    $('btnTestSelf').textContent = state.testSelf ? tr('停止测试') : tr('开始测试（拉起自翻通道）');
    if (state.testSelf) api.sessionStart('self');
    else api.sessionStop('self');
  };
}

/* ================= 记录页 ================= */
function recordHistory(stream, name, ev) {
  if (!ev.dst_final) return;
  const text = ev.dst || '';
  if (!text) return;
  const list = JSON.parse(localStorage.getItem('hist') || '[]');
  list.unshift({ t: Date.now(), who: name, dst: text });
  if (list.length > 500) list.length = 500;
  localStorage.setItem('hist', JSON.stringify(list));
}
function renderHistory() {
  const list = JSON.parse(localStorage.getItem('hist') || '[]');
  $('histList').innerHTML = list.map(h =>
    `<div><span class="who">[${escapeHtml(h.who)}]</span> ${escapeHtml(h.dst)}</div>`).join('');
}
function escapeHtml(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

function initPages() {
  $('btnClearHist').onclick = () => { localStorage.removeItem('hist'); renderHistory(); };
  $('tutorialText').textContent = TUTORIAL;
  $('contactText').textContent = CONTACT;
}

/* ================= 设置页 ================= */
async function fillDevices() {
  const mics = await navigator.mediaDevices.enumerateDevices();
  const ins = mics.filter(d => d.kind === 'audioinput');
  const outs = mics.filter(d => d.kind === 'audiooutput');
  const fill = (sel, list, def, cur) => {
    sel.innerHTML = `<option value="">${def}</option>` +
      list.map(d => `<option value="${d.deviceId}">${d.label || '设备'}</option>`).join('');
    sel.value = cur || '';
  };
  fill($('sMic'), ins, tr('默认麦克风'), cfg().mic_device);
  fill($('sOut'), outs, tr('默认扬声器'), cfg().output_device);
}

function initSettings() {
  const c = cfg();
  $('sUiLang').value = c.ui_lang || 'zh';
  $('sMode').value = c.mode;
  // dash_key 不下发：已保存则留空并提示，输入新值才覆盖
  $('sKey').value = '';
  $('sKey').placeholder = c.has_dash_key ? tr('已保存（输入新 Key 覆盖）') : 'sk-xxxx';
  $('sUrl').value = c.dash_ws_url;
  $('sModel').value = c.dash_model; $('sServer').value = c.server_url;
  $('sTargetMate').value = c.target_mate; $('sTargetSelf').value = c.target_self;
  $('sPlayAudio').checked = !!c.play_audio;
  $('sAutoCopy').checked = !!c.auto_copy;
  $('sMateFilter').checked = !!c.mate_filter;
  $('sOpacity').value = Math.round(c.overlay_opacity * 100);
  $('opVal').textContent = Math.round(c.overlay_opacity * 100) + '%';
  $('sMaxLines').value = c.max_bubbles;
  $('sThrough').checked = !!c.click_through;
  syncModeRows(); syncHkLabel();

  $('sMode').onchange = () => { syncModeRows(); applyLiveSettings(); };
  $('btnTestConn').onclick = testConn;
  $('btnRecKey').onclick = () => { $('hkLabel').textContent = tr('请按下按键或鼠标键…(Esc 取消)'); api.recordHotkey(); };
  $('btnBeep').onclick = () => player.beep(cfg().output_device || undefined);

  // —— 服务器账号：登录 / 注册 / 游客 / 清除 ——
  $('btnLogin').onclick = () => authFetch('/api/auth/login', {
    username: $('sUser').value.trim(), password: $('sPass').value });
  $('btnRegister').onclick = () => authFetch('/api/auth/register', {
    username: $('sUser').value.trim(), password: $('sPass').value });
  $('btnGuest').onclick = () => {
    // 设备号：首次生成并持久化，游客模式据此复用同一账号
    let did = localStorage.getItem('vt_device');
    if (!did) { did = crypto.randomUUID(); localStorage.setItem('vt_device', did); }
    authFetch('/api/auth/guest', { device_id: did }, tr('游客'));
  };
  $('btnLogout').onclick = async () => {
    await cfgSet({ token: '', username: '' });
    $('acctStatus').textContent = tr('未登录');
    $('acctStatus').style.color = '#7a8494';
    syncModeRows();
  };
  if (cfg().token && cfg().username)
    $('acctStatus').textContent = tr('已登录：') + cfg().username;

  // 全部控件 → 即时保存
  const map = {
    sUiLang: 'ui_lang', sMode: 'mode', sKey: 'dash_key', sUrl: 'dash_ws_url',
    sModel: 'dash_model', sServer: 'server_url', sTargetMate: 'target_mate',
    sTargetSelf: 'target_self', sMic: 'mic_device', sOut: 'output_device',
    sMaxLines: 'max_bubbles',
  };
  for (const [id, key] of Object.entries(map)) {
    $(id).onchange = async () => {
      const v = $(id).type === 'number' ? +$(id).value : $(id).value;
      // dash_key 留空 = 不改动已保存的 Key（主进程也会忽略空值）
      if (key === 'dash_key' && !String(v).trim()) { applyLiveSettings(key); return; }
      await cfgSet({ [key]: v }); applyLiveSettings(key);
      if (key === 'dash_key') { $(id).value = ''; $(id).placeholder = tr('已保存（输入新 Key 覆盖）'); }
    };
  }
  for (const [id, key] of Object.entries({
    sPlayAudio: 'play_audio', sAutoCopy: 'auto_copy',
    sMateFilter: 'mate_filter', sThrough: 'click_through',
  })) {
    $(id).onchange = async () => {
      await cfgSet({ [key]: $(id).checked }); applyLiveSettings(key);
    };
  }
  $('sOpacity').oninput = () => { $('opVal').textContent = $('sOpacity').value + '%'; };
  $('sOpacity').onchange = async () => {
    await cfgSet({ overlay_opacity: $('sOpacity').value / 100 }); applyLiveSettings();
  };
}

/* —— 服务器账号认证（登录/注册/游客共用） —— */
async function authFetch(path, body, label) {
  const st = $('acctStatus');
  const base = $('sServer').value.trim().replace(/\/+$/, '');
  if (!/^https?:\/\//.test(base)) {
    st.textContent = tr('服务器地址须以 http(s):// 开头');
    st.style.color = '#ff4655';
    return;
  }
  st.textContent = '…'; st.style.color = '#7a8494';
  try {
    const r = await fetch(base + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await r.json().catch(() => ({}));
    if (r.ok && d.code === 1) {
      await cfgSet({ token: d.token || '', username: d.username || '' });
      st.textContent = renderAcct(d, label);
      st.style.color = '#2fae66';
      syncModeRows();
      restartSessionsIfRunning();
    } else {
      st.textContent = d.msg || ('HTTP ' + r.status);
      st.style.color = '#ff4655';
    }
  } catch (e) {
    st.textContent = '✗ ' + (e.message || e);
    st.style.color = '#ff4655';
  }
}

/* 登录响应 -> 账号信息一行：用户名 · 余额 · 时长卡 · 订阅（· 试用） */
function renderAcct(d, label) {
  const parts = [label || d.username];
  if (d.money_yuan !== undefined && d.money_yuan !== null)
    parts.push(`${tr('余额')} ¥${d.money_yuan}`);
  if (d.credit_minutes !== undefined && d.credit_minutes !== null)
    parts.push(`${tr('时长卡')} ${d.credit_minutes}min`);
  if (d.sub_active)
    parts.push(`${tr('订阅')} →${String(d.sub_expire || d.sub_end || '').slice(5, 10)}`);
  else
    parts.push(`${tr('订阅')} ${tr('无')}`);
  if (d.trial_active && d.trial_minutes)
    parts.push(`${tr('试用')} ${d.trial_minutes}min`);
  return parts.join(' · ');
}

/* 服务器模式下登录成功后原地重开会话，使新 token 生效（同 applyLiveSettings） */
function restartSessionsIfRunning() {
  if (cfg().mode !== 'server' || !(state.running || state.testSelf)) return;
  api.sessionStop('mate'); api.sessionStop('self');
  api.sessionStart('self');
  if (state.running && state.mateOn) api.sessionStart('mate');
}

let _relaySnap = null;
function relaySnap() {
  const c = cfg();
  return JSON.stringify([c.mode, c.has_dash_key, c.dash_ws_url, c.dash_model,
    c.server_url, c.target_mate, c.target_self, c.voice, c.play_audio]);
}
async function applyLiveSettings(changedKey) {
  const c = cfg();
  $('playAudio').checked = !!c.play_audio;
  $('voiceSel').value = c.voice;
  loopback.setFilter(c.mate_filter);
  api.overlayThrough(c.click_through);
  pushOverlayCfg(); refreshChips();

  if (changedKey === 'ui_lang') {
    if (confirm('界面语言更改需重启生效 / Restart to apply language?')) api.relaunch();
    return;
  }
  if (changedKey === 'mic_device') { startMic(); return; }
  if (changedKey === 'loopback_device' && state.running) {
    try { await loopback.startLoopback(); loopback.setFilter(c.mate_filter); } catch {}
    return;
  }
  if (changedKey === 'mode') {
    if (confirm('运行模式切换需重启。\n现在重启？')) api.relaunch();
    return;
  }
  // 影响会话的配置 → 原地重开（主进程自行读磁盘配置，不传密钥）
  const snap = relaySnap();
  if (_relaySnap !== null && snap !== _relaySnap && (state.running || state.testSelf)) {
    api.sessionStop('mate'); api.sessionStop('self');
    api.sessionStart('self');
    if (state.running && state.mateOn) api.sessionStart('mate');
    else if (state.testSelf) api.sessionStart('self');
  }
  _relaySnap = snap;
}

function syncModeRows() {
  const direct = $('sMode').value === 'direct';
  document.querySelectorAll('.direct-only').forEach(el => el.style.display = direct ? '' : 'none');
  document.querySelectorAll('.srv-only').forEach(el => el.style.display = direct ? 'none' : '');
  $('sideBal').textContent = direct ? tr('本地直连') : (cfg().username || '—');
}

function syncHkLabel() {
  const hk = cfg().hotkey || {};
  const LEG = { x1: 'X1', x2: 'X2', middle: '中键' };
  const dev = hk.device === 'mouse' ? '鼠标' : '键盘';
  const kn = hk.device === 'mouse'
    ? ({ 3: '中键', 4: 'X1', 5: 'X2' }[hk.key] || LEG[hk.key] || hk.key)
    : hk.key;
  $('hkLabel').textContent = `${dev} ${kn}`;
}
function onHotkeyCaptured(spec) {
  if (!spec) { syncHkLabel(); return; }   // Esc/超时取消：恢复原标签
  cfgSet({ hotkey: spec }).then(() => { syncHkLabel(); refreshChips(); });
  $('hkLabel').textContent = '✓';
}

async function testConn() {
  const res = $('connRes'); res.textContent = tr('测试中…'); res.style.color = '#7a8494';
  const t0 = Date.now();
  try {
    if ($('sMode').value === 'direct') {
      // URL/模型用主进程保存的配置；仅把输入框里的新 Key（若有）带去测试
      const r = await api.sessionTest({ dash_key: $('sKey').value.trim() });
      res.textContent = (r.ok ? `✓ ${tr('连接成功')} ${r.ms}ms` : '✗ ' + r.msg);
      res.style.color = r.ok ? '#2fae66' : '#ff4655';
    } else {
      const r = await fetch(`${$('sServer').value.replace(/\/+$/, '')}/api/meta`);
      if (!r.ok) throw new Error('HTTP ' + r.status);
      res.textContent = `✓ ${tr('服务器可达')} ${Date.now() - t0}ms`;
      res.style.color = '#2fae66';
    }
  } catch (e) {
    res.textContent = '✗ ' + (e.message || e); res.style.color = '#ff4655';
  }
}

/* ================= 工具 ================= */
function toast(msg) {
  const t = $('toast'); t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2600);
}

/* ================= 教程 / 联系 ================= */
const TUTORIAL = `═══════ VTranslate 配置说明书 ═══════

【一、运行模式】（设置页顶部，改动即时保存到 %APPDATA%\\VTranslate\\config.json）

● 直连 DashScope（个人测试，推荐现在用）
  - 阿里云百炼控制台申请 API Key：
    1) https://bailian.console.aliyun.com/ 注册/登录
    2) 「API-KEY 管理」→ 创建 sk-xxxx
    3) 粘贴到 设置 → API Key
  - 模型地址已预填专属域名（勿改）
  - 直连按你的 DashScope 账号 token 计费

【二、音频】
- 队友语音：WASAPI 系统扬声器回环采集（游戏从哪出声就采哪）
- 自翻语音：默认麦克风
- TTS 播报：译文从「播报输出设备」播放，默认扬声器
  播报与回环同设备时播报期间自动静音防回声环路

【三、使用】
1. 点「开始翻译」→ 悬浮窗实时双语显示队友发言（自动区分 好友1/好友2…）
2. 按住热键（默认鼠标 X1）说话，松开出译文并自动复制——
   游戏内 Enter → Ctrl+V → Enter 发送
3. 「穿透」开启后鼠标点不到字幕（游戏内推荐开）
4. 已内置 Valorant 术语热词：特工/地图/枪械/战术术语自动准确翻译
5. 队友识别太杂？开启「语音频段过滤」削弱枪声脚步声；
   游戏播报语音（Spike planted 等）属人声无法区分，属正常
6. 翻译记录页保存最近 500 条（仅存本机）

【四、常见问题】
Q 没有翻译输出？  A 检查 API Key / 余额；设置页「测试连接」验端点
Q 游戏内看不到字幕？  A 游戏画面必须设为「无边框窗口」——
  全屏独占盖住所有置顶窗，Windows 限制无解
Q 进游戏热键失效？  A Valorant 以管理员权限运行时，普通权限程序的
  全局热键无法注入。本程序默认不再强制提权（安全考虑），如需游戏内
  热键请右键 exe「以管理员身份运行」
Q 会不会封号？  A 不会。本程序只读写音频设备，不接触游戏进程/
  内存/文件/网络数据，与普通录屏软件同级

开源仓库：https://gitee.com/chunfff/v-translation`;

const CONTACT = `问题反馈 / 商务合作：
QQ 群：在官网或推广页获取最新群号
邮箱：support@example.com（请替换为你的联系方式）
开源仓库：https://gitee.com/chunfff/v-translation`;
