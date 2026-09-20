/* 本地配置：与 Python 版共用 %APPDATA%/VTranslate/config.json 同一文件 */
const DEFAULTS = {
  mode: "direct",
  server_url: "http://127.0.0.1:8787",
  token: "", username: "",
  dash_key: "",
  dash_ws_url: "wss://llm-5gwthr14damb7ihz.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime",
  dash_model: "qwen3.8-livetranslate-flash-realtime",
  hotkey: { device: "mouse", key: "4" },       // uiohook: 鼠标侧键 X1=4
  target_mate: "zh", target_self: "en",
  mate_enabled: true, running: false,
  voice: "Cherry", play_audio: true,
  ui_lang: "",
  auto_copy: false, mate_filter: true,
  overlay_opacity: 0.85, click_through: false, max_bubbles: 8,
  mic_device: "", loopback_device: "", output_device: "",
  overlay_src_color: "#bbbbbb", overlay_dst_color: "#ffffff",
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
  await api.cfgSet(patch);
}
