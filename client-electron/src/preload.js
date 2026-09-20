const { contextBridge, ipcRenderer } = require('electron');

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
  sessionStart: (stream, cfg) => ipcRenderer.send('session:start', { stream, cfg }),
  sessionStop: (stream) => ipcRenderer.send('session:stop', stream),
  sessionAudio: (stream, b64) => ipcRenderer.send('session:audio', { stream, b64 }),
  sessionCommit: (stream) => ipcRenderer.send('session:commit', stream),
  sessionClear: (stream) => ipcRenderer.send('session:clear', stream),
  sessionTest: (cfg) => ipcRenderer.invoke('session:test', cfg),
  on: (ch, fn) => ipcRenderer.on(ch, (_e, ...a) => fn(...a)),
});
