/* VTranslate Electron 主进程：
 * 主窗(无边框) + 字幕悬浮窗(透明/置顶/可穿透) + 托盘 + 全局 PTT 钩子 + 单实例。
 * 音频采集/翻译全在 renderer 的 Web API + WebSocket 里跑。
 */
const { app, BrowserWindow, Tray, Menu, ipcMain, screen,
        session, desktopCapturer, dialog, clipboard } = require('electron');
const path = require('path');
const fs = require('fs');

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) { app.quit(); }

let mainWin = null, overlayWin = null, tray = null;
let recordingHotkey = false;
let uIOhook = null, UiohookKey = null;

const cfgPath = () => path.join(app.getPath('userData'), 'config.json');
function readCfg() {
  try { return JSON.parse(fs.readFileSync(cfgPath(), 'utf-8')); }
  catch { return {}; }
}
function writeCfg(patch) {
  const cur = readCfg();
  Object.assign(cur, patch);
  fs.mkdirSync(path.dirname(cfgPath()), { recursive: true });
  fs.writeFileSync(cfgPath(), JSON.stringify(cur, null, 2));
  return cur;
}

/* renderer 可写的配置键白名单（仅设置页字段，其余一律忽略） */
/* token/username 只由主进程在登录成功时写入（srv:request），renderer 不可写也读不到 token */
const CFG_SETTABLE = new Set([
  'ui_lang', 'mode', 'dash_key', 'dash_ws_url', 'dash_model', 'server_url',
  'hotkey', 'target_mate', 'target_self',
  'mate_enabled', 'running', 'voice', 'play_audio',
  'auto_copy', 'mate_filter', 'overlay_opacity', 'click_through',
  'max_bubbles', 'mic_device', 'loopback_device', 'output_device',
  'overlay_src_color', 'overlay_dst_color', 'overlay_font_scale',
  'overlay_fade_sec', 'overlay_show_src', 'mate_silence_ms',
]);
/* session:start 只允许 renderer 传这些非敏感覆盖项，其余一律读磁盘配置。
 * server_url 不在其中：中转连接携带 token，目标地址只能来自磁盘配置 */
const SESSION_OPTS = new Set([
  'mode', 'target_mate', 'target_self', 'voice', 'play_audio', 'mate_enabled',
]);
/* 未保存过的字段在主进程侧的兜底（与 renderer/store.js DEFAULTS 保持一致） */
const SESSION_DEFAULTS = {
  mode: 'server', target_mate: 'zh', target_self: 'en', voice: 'Cherry',
  dash_ws_url: 'wss://dashscope.aliyuncs.com/api-ws/v1/realtime',
  dash_model: 'qwen3.8-livetranslate-flash-realtime', mate_silence_ms: 800,
};
const sessionCfg = () => {
  const c = { ...SESSION_DEFAULTS, ...readCfg() };
  for (const k of Object.keys(SESSION_DEFAULTS)) if (c[k] === '' || c[k] == null) c[k] = SESSION_DEFAULTS[k];
  return c;
};
function maskCfg(c) {
  /* 下发给 renderer 的配置：抹掉密钥，只留"是否已保存"标记 */
  const masked = { ...c, dash_key: '', token: '' };
  masked.has_dash_key = !!(c.dash_key && String(c.dash_key).trim());
  masked.has_token = !!c.token;
  return masked;
}

/* ---------- 计费服务器 HTTP（主进程发起：无 CORS 限制，token 不出主进程） ---------- */
const SRV_PATHS = /^\/api\/(meta|me|products|referral\/stats|auth\/(login|register|guest)|pay\/(create|status))(\?[\w=&.%-]*)?$/;
ipcMain.handle('srv:request', async (_e, req) => {
  const { path: p, method = 'GET', body } = req || {};
  if (typeof p !== 'string' || !SRV_PATHS.test(p) || !['GET', 'POST'].includes(method))
    return { ok: false, status: 0, data: { msg: 'path not allowed' } };
  const cfg = readCfg();
  const base = String(cfg.server_url || '').trim().replace(/\/+$/, '');
  if (!/^https?:\/\/[^\s/]+/.test(base))
    return { ok: false, status: 0, data: { msg: '服务器地址须以 http(s):// 开头' } };
  const headers = { 'Content-Type': 'application/json' };
  if (cfg.token && !p.startsWith('/api/auth/')) headers.Authorization = `Bearer ${cfg.token}`;
  const t0 = Date.now();
  try {
    const r = await fetch(base + p, {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(12000),
    });
    const data = await r.json().catch(() => ({}));
    if (r.ok && data.code === 1 && p.startsWith('/api/auth/') && data.token)
      writeCfg({ token: data.token, username: data.username || '' });
    delete data.token;
    return { ok: r.ok && data.code === 1, status: r.status, data, ms: Date.now() - t0 };
  } catch (e) {
    return { ok: false, status: 0,
             data: { msg: e.name === 'TimeoutError' ? '连接服务器超时' : String(e.message || e) } };
  }
});
ipcMain.handle('srv:logout', () => maskCfg(writeCfg({ token: '', username: '' })));

/* ---------- 悬浮窗 ---------- */
function createOverlay() {
  const saved = readCfg().overlay_bounds;
  const wa = screen.getPrimaryDisplay().workArea;
  const onScreen = saved && screen.getAllDisplays().some((d) =>
    saved.x >= d.workArea.x - 40 && saved.y >= d.workArea.y - 20 &&
    saved.x < d.workArea.x + d.workArea.width - 60 &&
    saved.y < d.workArea.y + d.workArea.height - 40);
  overlayWin = new BrowserWindow({
    width: onScreen ? saved.width : 420, height: onScreen ? saved.height : 280,
    x: onScreen ? saved.x : wa.x + wa.width - 440,
    y: onScreen ? saved.y : wa.y + 60,
    minWidth: 260, minHeight: 34,
    frame: false, transparent: true, alwaysOnTop: true,
    skipTaskbar: true, resizable: true, hasShadow: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
    },
  });
  overlayWin.setAlwaysOnTop(true, 'screen-saver');
  overlayWin.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  overlayWin.loadFile(path.join(__dirname, 'renderer', 'overlay.html'));
  // 悬浮窗可能晚于主窗加载完成：就绪后让主窗重推一次配置/状态
  overlayWin.webContents.on('did-finish-load', () => send('ovctl', 'ready'));
  if (overlayTopTimer) clearInterval(overlayTopTimer);
  overlayTopTimer = setInterval(() => {
    if (overlayWin && !overlayWin.isDestroyed())
      overlayWin.setAlwaysOnTop(true, 'screen-saver');
  }, 1500);
  // 记住用户拖动/缩放后的位置（去抖写盘）
  let boundsTimer = null;
  const saveBounds = () => {
    clearTimeout(boundsTimer);
    boundsTimer = setTimeout(() => {
      if (overlayWin && !overlayWin.isDestroyed())
        writeCfg({ overlay_bounds: overlayWin.getBounds() });
    }, 500);
  };
  overlayWin.on('moved', saveBounds);
  overlayWin.on('resized', saveBounds);
  overlayWin.on('closed', () => {
    overlayWin = null;
    if (overlayTopTimer) { clearInterval(overlayTopTimer); overlayTopTimer = null; }
  });
}

/* 无边框置顶可能被抢，周期复位（随窗口销毁清理） */
let overlayTopTimer = null;

/* ---------- 主窗 ---------- */
function createMain() {
  mainWin = new BrowserWindow({
    width: 980, height: 640, minWidth: 820, minHeight: 540,
    frame: false, backgroundColor: '#0b1016',
    icon: path.join(__dirname, '..', 'assets', 'icon.png'),
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
    },
  });
  mainWin.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  mainWin.once('ready-to-show', () => mainWin.show());
  mainWin.on('closed', () => { mainWin = null; });
}

/* ---------- 托盘 ---------- */
const trayState = { running: false, through: false, ovVisible: true };
ipcMain.on('tray:state', (_e, s) => Object.assign(trayState, s));

function buildTrayMenu() {
  return Menu.buildFromTemplate([
    { label: '打开主窗口',
      click: () => { if (mainWin) { mainWin.show(); mainWin.focus(); } } },
    { label: trayState.running ? '停止翻译' : '开始翻译',
      click: () => send('tray', 'toggle-run') },
    { type: 'separator' },
    { label: '显示字幕窗', type: 'checkbox', checked: trayState.ovVisible,
      click: () => send('tray', 'toggle-overlay') },
    { label: '字幕穿透', type: 'checkbox', checked: trayState.through,
      click: () => send('tray', 'toggle-through') },
    { label: '锁定字幕窗', type: 'checkbox', checked: trayState.locked,
      click: () => send('tray', 'toggle-lock') },
    { type: 'separator' },
    { label: '设置',
      click: () => {
        if (mainWin) { mainWin.show(); mainWin.focus(); }
        send('tray', 'open-settings');
      } },
    { label: '退出', click: () => { app.isQuitting = true; app.quit(); } },
  ]);
}

function createTray() {
  tray = new Tray(path.join(__dirname, '..', 'assets', 'icon.png'));
  tray.setToolTip('VTranslate');
  tray.setContextMenu(buildTrayMenu());
  tray.on('right-click', () => tray.setContextMenu(buildTrayMenu())); // 动态刷新
  tray.on('double-click', () => mainWin && (mainWin.show(), mainWin.focus()));
}

/* ---------- 全局 PTT（uiohook：键盘 + 鼠标侧键） ---------- */
const LEGACY_MOUSE = { x1: '4', x2: '5', middle: '3', left: '1', right: '2' };
function normalizeHotkey(hk) {
  /* 兼容 Python 版配置：pynput 名字 -> uiohook 码 */
  if (!hk || !hk.device) return null;
  let key = String(hk.key || '');
  if (hk.device === 'mouse') {
    if (LEGACY_MOUSE[key]) key = LEGACY_MOUSE[key];   // "x1"->"4"
    return { device: 'mouse', key };
  }
  if (hk.device === 'keyboard') {
    if (/^\d+$/.test(key)) return { device: 'keyboard', key };
    // pynput 名字 -> UiohookKey 常量（f9->F9, caps_lock->CapsLock, a->A）
    const name = key.replace(/_([a-z])/g, (_, c) => c.toUpperCase())
                    .replace(/^./, c => c.toUpperCase());
    const code = UiohookKey && UiohookKey[name];
    if (code !== undefined) return { device: 'keyboard', key: String(code) };
    return null;   // 认不出 → 让 UI 提示重录
  }
  return null;
}

function startHook() {
  try {
    const mod = require('uiohook-napi');
    uIOhook = mod.uIOhook; UiohookKey = mod.UiohookKey;
    let hkBadWarned = false;
    let pttPressed = false;   // 边沿检测：按住自动重复 keydown 只发一次 true
    const ptt = (on) => {
      if (on === pttPressed) return;
      pttPressed = on;
      send('ptt', on);
    };
    const keyOf = () => {
      const hk = normalizeHotkey(readCfg().hotkey);
      if (!hk && !hkBadWarned) {
        hkBadWarned = true;
        send('hotkey-invalid');
      }
      return hk || {};
    };

    uIOhook.on('keydown', (e) => {
      if (recordingHotkey) {
        if (UiohookKey && e.keycode === UiohookKey.Escape) {
          cancelHotkeyRecord();   // Esc 取消录入
          return;
        }
        finishHotkeyRecord({ device: 'keyboard', key: String(e.keycode) });
        return;
      }
      const hk = keyOf();
      if (hk.device === 'keyboard' && String(e.keycode) === hk.key)
        ptt(true);
    });
    uIOhook.on('keyup', (e) => {
      const hk = keyOf();
      if (hk.device === 'keyboard' && String(e.keycode) === hk.key)
        ptt(false);
    });
    uIOhook.on('mousedown', (e) => {
      if (recordingHotkey) {
        finishHotkeyRecord({ device: 'mouse', key: String(e.button) });
        return;
      }
      const hk = keyOf();
      if (hk.device === 'mouse' && String(e.button) === hk.key)
        ptt(true);
    });
    uIOhook.on('mouseup', (e) => {
      const hk = keyOf();
      if (hk.device === 'mouse' && String(e.button) === hk.key)
        ptt(false);
    });
    uIOhook.start();
    console.log('uiohook started');
  } catch (e) {
    console.warn('uiohook 启动失败（键盘热键仍可用？）:', e.message);
  }
}

function send(ch, ...args) {
  if (mainWin && !mainWin.isDestroyed())
    mainWin.webContents.send(ch, ...args);
}
function sendOverlay(ch, ...args) {
  if (overlayWin && !overlayWin.isDestroyed())
    overlayWin.webContents.send(ch, ...args);
}

/* ---------- IPC ---------- */
ipcMain.handle('cfg:get', () => maskCfg(readCfg()));
ipcMain.handle('cfg:set', (_e, patch) => {
  /* 白名单过滤；dash_key 传空串表示"不改动已保存的 Key" */
  const clean = {};
  for (const k of Object.keys(patch || {})) {
    if (!CFG_SETTABLE.has(k)) continue;
    if (k === 'dash_key' && !String(patch[k] || '').trim()) continue;
    clean[k] = patch[k];
  }
  return maskCfg(writeCfg(clean));
});
ipcMain.on('win:min', () => mainWin && mainWin.minimize());
ipcMain.on('win:close', () => mainWin && mainWin.hide());   // 关到托盘
ipcMain.on('win:max', () => {
  if (!mainWin) return;
  mainWin.isMaximized() ? mainWin.unmaximize() : mainWin.maximize();
});
ipcMain.on('overlay:show', (_e, show) => {
  if (!overlayWin) createOverlay();
  else show ? overlayWin.show() : overlayWin.hide();
});
let ovThrough = false;
ipcMain.on('overlay:through', (_e, on) => {
  ovThrough = !!on;
  if (overlayWin) overlayWin.setIgnoreMouseEvents(ovThrough, { forward: true });
});
/* 穿透模式下光标悬停顶栏：临时恢复可交互（可拖动/点按钮），离开后恢复穿透 */
ipcMain.on('ovhover', (_e, on) => {
  if (!overlayWin || overlayWin.isDestroyed() || !ovThrough) return;
  if (!on && ovDragTimer) return;   // 拖动中光标可能短暂出界，不切回穿透
  overlayWin.setIgnoreMouseEvents(!on, { forward: true });
});
/* 悬浮窗拖拽：app-region 在透明窗上不可靠，改为主进程跟随光标。
 * renderer mousedown -> 记录偏移并启动跟随；mouseup/blur -> 停止 */
let ovDragTimer = null, ovDragOff = null;
ipcMain.on('ovdrag', (_e, phase) => {
  if (phase !== 'start') {
    if (ovDragTimer) { clearInterval(ovDragTimer); ovDragTimer = null; }
    ovDragOff = null;
    // 拖完若光标已不在窗内，穿透模式恢复忽略鼠标
    if (ovThrough && overlayWin && !overlayWin.isDestroyed()) {
      const c = screen.getCursorScreenPoint(), b = overlayWin.getBounds();
      const inBar = c.x >= b.x && c.x < b.x + b.width && c.y >= b.y && c.y < b.y + 30;
      if (!inBar) overlayWin.setIgnoreMouseEvents(true, { forward: true });
    }
    return;
  }
  if (!overlayWin || overlayWin.isDestroyed()) return;
  const p = screen.getCursorScreenPoint();
  const [wx, wy] = overlayWin.getPosition();
  ovDragOff = { x: p.x - wx, y: p.y - wy };
  if (ovDragTimer) clearInterval(ovDragTimer);
  ovDragTimer = setInterval(() => {
    if (!overlayWin || overlayWin.isDestroyed() || !ovDragOff) return;
    const c = screen.getCursorScreenPoint();
    overlayWin.setPosition(Math.round(c.x - ovDragOff.x),
                           Math.round(c.y - ovDragOff.y));
  }, 16);
});
/* 热键录入：Esc 取消 / 10s 超时自动取消，避免卡死导致 PTT 失效 */
let hkRecordTimer = null;
function finishHotkeyRecord(spec) {
  recordingHotkey = false;
  if (hkRecordTimer) { clearTimeout(hkRecordTimer); hkRecordTimer = null; }
  send('hotkey-captured', spec);
}
function cancelHotkeyRecord() {
  recordingHotkey = false;
  if (hkRecordTimer) { clearTimeout(hkRecordTimer); hkRecordTimer = null; }
  send('hotkey-captured', null);   // null = 取消，renderer 恢复原标签
}
ipcMain.on('hotkey:record', () => {
  recordingHotkey = true;
  if (hkRecordTimer) clearTimeout(hkRecordTimer);
  hkRecordTimer = setTimeout(cancelHotkeyRecord, 10000);
});
// renderer -> overlay 转发；overlay -> 主窗 反向控制
ipcMain.on('ov', (_e, ch, payload) => sendOverlay(ch, payload));
ipcMain.on('ovctl', (_e, action) => send('ovctl', action));
ipcMain.on('app:relaunch', () => { app.relaunch(); app.exit(0); });
/* 剪贴板写必须走主进程：窗口失焦/最小化时 navigator.clipboard 会静默失败 */
ipcMain.on('clip:write', (_e, text) => {
  try { clipboard.writeText(String(text || '')); } catch {}
});

/* ---------- 翻译会话（WS 带 Authorization 头，须跑在主进程） ---------- */
const { DirectSession, RelaySession } = require('./rt');
const { termsForTarget } = require('./terms');
const sessions = { mate: null, self: null };

ipcMain.on('session:start', (_e, payload) => {
  /* 密钥/端点只信磁盘配置；renderer 只能覆盖白名单内的非敏感项 */
  const { stream } = payload || {};
  if (stream !== 'mate' && stream !== 'self') return;
  const opts = (payload && payload.opts) || {};
  const cfg = sessionCfg();
  for (const k of Object.keys(opts))
    if (SESSION_OPTS.has(k)) cfg[k] = opts[k];
  try {
    if (sessions[stream]) sessions[stream].stop();
    const emit = (ch, payload) => send(ch, payload);
    const S = cfg.mode === 'direct' ? DirectSession : RelaySession;
    const phrases = cfg.mode === 'direct'
      ? termsForTarget(stream === 'mate' ? cfg.target_mate : cfg.target_self)
      : null;
    const s = new S(cfg, stream, phrases, emit);
    sessions[stream] = s;
    // 事件代际校验：旧会话（被 stop 后仍可能在退避/连接中）的事件一律丢弃，
    // 防止快速重开时新旧会话事件串扰
    const origEmit = s.emit;
    s.emit = (ch, payload) => {
      if (sessions[stream] === s) origEmit(ch, payload);
    };
    s.start();
  } catch (e) {
    send('rt-ev', { stream, ev: { type: 'error', code: 'start',
                                  msg: String(e.message || e) } });
  }
});
ipcMain.on('session:stop', (_e, stream) => {
  if (sessions[stream]) { sessions[stream].stop(); sessions[stream] = null; }
});
ipcMain.on('session:audio', (_e, { stream, b64 }) => {
  const s = sessions[stream]; if (s) s.sendAudio(b64);
});
ipcMain.on('session:commit', (_e, stream) => {
  const s = sessions[stream]; if (s) s.commit();
});
ipcMain.on('session:clear', (_e, stream) => {
  const s = sessions[stream]; if (s) s.clear();
});

// 连通性测试：真实握手验 Key/模型/端点。
// URL/模型一律用磁盘配置；仅允许 renderer 传入待测的新 dash_key（未保存的输入）。
ipcMain.handle('session:test', async (_e, opts) => {
  const WebSocket = require('ws');
  const stored = sessionCfg();
  const key = (opts && String(opts.dash_key || '').trim()) || stored.dash_key;
  return new Promise((res) => {
    const t0 = Date.now();
    let settled = false, timer = null;
    let ws = null;
    // 前置校验：缺 Key/端点时给出可操作的错误，而不是去连一个坏 URL
    if (!key)
      return res({ ok: false, msg: '未配置 API Key（设置页填写 DashScope Key）', ms: 0 });
    if (!stored.dash_ws_url || !/^wss?:\/\//.test(stored.dash_ws_url))
      return res({ ok: false, msg: '端点地址无效（应以 wss:// 开头）', ms: 0 });
    const done = (ok, msg) => {
      if (settled) return;
      settled = true;
      if (timer) { clearTimeout(timer); timer = null; }
      try { ws && ws.close(); } catch {}
      res({ ok, msg, ms: Date.now() - t0 });
    };
    try {
      const url = `${(stored.dash_ws_url || '').replace(/\/+$/, '')}?model=${stored.dash_model}`;
      ws = new WebSocket(url, {
        headers: { Authorization: `Bearer ${key}` },
        handshakeTimeout: 12000,
      });
      ws.on('message', (raw) => {
        let ev; try { ev = JSON.parse(raw.toString()); } catch { return; }
        if (ev.type === 'session.created' || ev.type === 'session.updated')
          done(true, `${Date.now() - t0}ms`);
        else if (ev.type === 'error' || ev.code)
          done(false, JSON.stringify(ev).slice(0, 200));
      });
      ws.on('open', () => ws.send(JSON.stringify({
        type: 'session.update', session: { output_modalities: ['text'] } })));
      ws.on('error', (e) => done(false, e.message));
      ws.on('close', (c) => { if (!c) return; done(false, `closed ${c}`); });
      timer = setTimeout(() => done(false, 'timeout'), 13000);
    } catch (e) { done(false, String(e.message || e)); }
  });
});

app.whenReady().then(() => {
  app.setAppUserModelId('chunfff.VTranslate');

  // 权限收紧：仅放行本应用自有窗口的 'media'（麦克风）请求，其余全拒
  const ownWindow = (wc) =>
    (mainWin && wc === mainWin.webContents) ||
    (overlayWin && wc === overlayWin.webContents);
  session.defaultSession.setPermissionRequestHandler((wc, perm, cb) =>
    cb(perm === 'media' && ownWindow(wc)));
  session.defaultSession.setPermissionCheckHandler((wc, perm) =>
    perm === 'media' && (!wc || ownWindow(wc)));

  // 系统音频回环：getDisplayMedia 的 audio='loopback'（Windows 支持）
  // 仅本应用 file:// 页面可触发，只给回环音频（视频轨 renderer 拿到即丢弃）
  session.defaultSession.setDisplayMediaRequestHandler((req, cb) => {
    if (!req.securityOrigin || !req.securityOrigin.startsWith('file://'))
      return cb({});
    desktopCapturer.getSources({ types: ['screen'] }).then(([src]) => {
      cb({ video: src, audio: 'loopback' });
    }).catch(() => cb({}));
  });

  createOverlay();
  createMain();
  createTray();
  startHook();
});

app.on('window-all-closed', () => { /* 全关也不退，住托盘 */ });
app.on('before-quit', () => { try { uIOhook && uIOhook.stop(); } catch {} });
app.on('second-instance', () => {
  if (mainWin) { mainWin.show(); mainWin.focus(); }
});
