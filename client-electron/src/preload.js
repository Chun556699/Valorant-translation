const { contextBridge, ipcRenderer } = require('electron');

/* renderer 允许监听的 IPC 通道白名单 */
const _CHANNELS = [
  'cfg', 'seg', 'status', 'ptt', 'mate_tick', 'mate-state',
  'rt-state', 'rt-ev', 'tts', 'ovctl', 'tray',
  'hotkey-captured', 'hotkey-invalid', 'speaker',
];

contextBridge.exposeInMainWorld('api', {
  cfgGet: () => ipcRenderer.invoke('cfg:get'),
  cfgSet: (patch) => ipcRenderer.invoke('cfg:set', patch),
  winMin: () => ipcRenderer.send('win:min'),
  winClose: () => ipcRenderer.send('win:close'),
  winMax: () => ipcRenderer.send('win:max'),
  overlayShow: (show) => ipcRenderer.send('overlay:show', show),
  overlayThrough: (on) => ipcRenderer.send('overlay:through', on),
  recordHotkey: () => ipcRenderer.send('hotkey:record'),
  toOverlay: (ch, payload) => ipcRenderer.send('ov', ch, payload),
  overlayCtl: (action) => ipcRenderer.send('ovctl', action),
  relaunch: () => ipcRenderer.send('app:relaunch'),
  trayState: (s) => ipcRenderer.send('tray:state', s),
  // opts 仅接受非敏感会话选项（mode/target/voice 等），密钥由主进程读磁盘
  sessionStart: (stream, opts) => ipcRenderer.send('session:start', { stream, opts }),
  sessionStop: (stream) => ipcRenderer.send('session:stop', stream),
  sessionAudio: (stream, b64) => ipcRenderer.send('session:audio', { stream, b64 }),
  sessionCommit: (stream) => ipcRenderer.send('session:commit', stream),
  sessionClear: (stream) => ipcRenderer.send('session:clear', stream),
  // 仅可传待测的新 dash_key；URL/模型用主进程保存的配置
  sessionTest: (opts) => ipcRenderer.invoke('session:test', opts),
  // renderer 可订阅的 IPC 通道白名单：白名单外的通道一律忽略（防任意监听）
  on: (ch, fn) => {
    if (!_CHANNELS.includes(ch)) return;
    ipcRenderer.on(ch, (_e, ...a) => fn(...a));
  },
});
