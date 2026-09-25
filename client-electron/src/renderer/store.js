/* 本地配置：%APPDATA%/VTranslate/config.json（主进程读写，密钥/token 不下发到页面） */
const DEFAULTS = {
  mode: "server",
  server_url: "",
  username: "", has_token: false,
  dash_key: "",
  dash_ws_url: "wss://dashscope.aliyuncs.com/api-ws/v1/realtime",
  dash_model: "qwen3.8-livetranslate-flash-realtime",
  hotkey: { device: "mouse", key: "4" },       // uiohook: 鼠标侧键 X1=4
  target_mate: "zh", target_self: "en",
  mate_enabled: true, running: false,
  voice: "Cherry", play_audio: false,
  ui_lang: "",
  auto_copy: false, mate_filter: true,
  mate_silence_ms: 800,
  overlay_opacity: 0.6, click_through: false, max_bubbles: 6,
  overlay_font_scale: 1, overlay_fade_sec: 20, overlay_show_src: true,
  mic_device: "", loopback_device: "", output_device: "",
  overlay_src_color: "#b8c2cc", overlay_dst_color: "#ffffff",
};

let _cfg = { ...DEFAULTS };

async function cfgLoad() {
  try {
    const saved = await api.cfgGet();
    _cfg = { ...DEFAULTS, ...saved };
  } catch {}
  return _cfg;
}
function cfg() { return _cfg; }
async function cfgSet(patch) {
  Object.assign(_cfg, patch);
  // 主进程返回过滤后的实际配置（dash_key/token 已脱敏），以其为准
  const saved = await api.cfgSet(patch);
  if (saved && typeof saved === 'object') _cfg = { ...DEFAULTS, ...saved };
  return _cfg;
}
function cfgReplace(saved) { if (saved && typeof saved === 'object') _cfg = { ...DEFAULTS, ...saved }; }
