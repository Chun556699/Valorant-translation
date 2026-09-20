/* VTranslate Electron 主进程：
 * 主窗(无边框) + 字幕悬浮窗(透明/置顶/可穿透) + 托盘 + 全局 PTT 钩子 + 单实例。
 * 音频采集/翻译全在 renderer 的 Web API + WebSocket 里跑。
 */
const { app, BrowserWindow, Tray, Menu, ipcMain, screen,
        session, desktopCapturer, dialog } = require('electron');
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

/* ---------- 悬浮窗 ---------- */
function createOverlay() {
  overlayWin = new BrowserWindow({
    width: 340, height: 240,
    x: screen.getPrimaryDisplay().workAreaSize.width - 360,
    y: 60,
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
  // 无边框置顶可能被抢，周期复位
  setInterval(() => {
    if (overlayWin && !overlayWin.isDestroyed())
      overlayWin.setAlwaysOnTop(true, 'screen-saver');
  }, 1500);
  overlayWin.on('closed', () => { overlayWin = null; });
}

/* ---------- 主窗 ---------- */
function createMain() {
  mainWin = new BrowserWindow({
    width: 760, height: 500, minWidth: 600, minHeight: 400,
    frame: false, backgroundColor: '#f4f5f7',
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
function createTray() {
  tray = new Tray(path.join(__dirname, '..', 'assets', 'icon.png'));
  tray.setToolTip('VTranslate');
  const menu = Menu.buildFromTemplate([
    { label: '打开主窗口', click: () => mainWin && (mainWin.show(), mainWin.focus()) },
    { type: 'separator' },
    { label: '退出', click: () => { app.isQuitting = true; app.quit(); } },
  ]);
  tray.setContextMenu(menu);
  tray.on('double-click', () => mainWin && (mainWin.show(), mainWin.focus()));
}

/* ---------- 全局 PTT（uiohook：键盘 + 鼠标侧键） ---------- */
function startHook() {
  try {
    const mod = require('uiohook-napi');
    uIOhook = mod.uIOhook; UiohookKey = mod.UiohookKey;
    const keyOf = () => (readCfg().hotkey || {});

    uIOhook.on('keydown', (e) => {
      if (recordingHotkey) {
        recordingHotkey = false;
        send('hotkey-captured', { device: 'keyboard', key: String(e.keycode) });
        return;
      }
      const hk = keyOf();
      if (hk.device === 'keyboard' && String(e.keycode) === hk.key)
        send('ptt', true);
    });
    uIOhook.on('keyup', (e) => {
      const hk = keyOf();
      if (hk.device === 'keyboard' && String(e.keycode) === hk.key)
        send('ptt', false);
    });
    uIOhook.on('mousedown', (e) => {
      if (recordingHotkey) {
        recordingHotkey = false;
        send('hotkey-captured', { device: 'mouse', key: String(e.button) });
        return;
      }
      const hk = keyOf();
      if (hk.device === 'mouse' && String(e.button) === hk.key)
        send('ptt', true);
    });
    uIOhook.on('mouseup', (e) => {
      const hk = keyOf();
      if (hk.device === 'mouse' && String(e.button) === hk.key)
        send('ptt', false);
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
ipcMain.handle('cfg:get', () => readCfg());
ipcMain.handle('cfg:set', (_e, patch) => writeCfg(patch));
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
ipcMain.on('overlay:through', (_e, on) => {
  if (overlayWin) overlayWin.setIgnoreMouseEvents(on, { forward: true });
});
ipcMain.on('hotkey:record', () => { recordingHotkey = true; });
// renderer -> overlay 转发；overlay -> 主窗 反向控制
ipcMain.on('ov', (_e, ch, payload) => sendOverlay(ch, payload));
ipcMain.on('ovctl', (_e, action) => send('ovctl', action));
ipcMain.on('app:relaunch', () => { app.relaunch(); app.exit(0); });

/* ---------- 翻译会话（WS 带 Authorization 头，须跑在主进程） ---------- */
const { DirectSession, RelaySession } = require('./rt');
const { termsForTarget } = require('./terms');
const sessions = { mate: null, self: null };

ipcMain.on('session:start', (_e, { stream, cfg }) => {
  try {
    if (sessions[stream]) sessions[stream].stop();
    const emit = (ch, payload) => send(ch, payload);
    const S = cfg.mode === 'direct' ? DirectSession : RelaySession;
    const phrases = cfg.mode === 'direct'
      ? termsForTarget(stream === 'mate' ? cfg.target_mate : cfg.target_self)
      : null;
    sessions[stream] = new S(cfg, stream, phrases, emit);
    sessions[stream].start();
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

// 连通性测试：真实握手验 Key/模型/端点
ipcMain.handle('session:test', async (_e, cfg) => {
  const WebSocket = require('ws');
  return new Promise((res) => {
    const t0 = Date.now();
    try {
      const url = `${(cfg.dash_ws_url || '').replace(/\/+$/, '')}?model=${cfg.dash_model}`;
      const ws = new WebSocket(url, {
        headers: { Authorization: `Bearer ${cfg.dash_key}` },
        handshakeTimeout: 12000,
      });
      const done = (ok, msg) => { try { ws.close(); } catch {} res({ ok, msg, ms: Date.now() - t0 }); };
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
      setTimeout(() => done(false, 'timeout'), 13000);
    } catch (e) { res({ ok: false, msg: String(e.message || e) }); }
  });
});

app.whenReady().then(() => {
  app.setAppUserModelId('chunfff.VTranslate');

  // 系统音频回环：getDisplayMedia 的 audio='loopback'（Windows 支持）
  session.defaultSession.setDisplayMediaRequestHandler((req, cb) => {
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
