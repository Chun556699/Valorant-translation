"""客户端配置：持久化到 %APPDATA%/VTranslate/config.json"""
import json
import os
import uuid

APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                       "VTranslate")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
HISTORY_PATH = os.path.join(APP_DIR, "history.jsonl")

DEFAULTS = {
    # 运行模式: "direct"=直连DashScope(个人测试) / "server"=计费服务器
    "mode": "direct",
    "server_url": "http://127.0.0.1:8765",
    "device_id": "",            # 首次启动生成
    "token": "",
    "username": "",
    # ---- 直连模式 ----
    "dash_key": "",             # 你的 DashScope API Key（仅存本机）
    "dash_ws_url": "wss://llm-5gwthr14damb7ihz.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime",
    "dash_model": "qwen3.8-livetranslate-flash-realtime",
    # 自翻热键: {"device": "mouse", "key": "x1"} 或 {"device": "keyboard", "key": "f9"}
    "hotkey": {"device": "mouse", "key": "x1"},
    "target_mate": "zh",        # 队友语音翻译成什么语言（显示原文+译文）
    "target_self": "en",        # 自己说话翻译成什么语言
    "mate_enabled": True,       # 是否实时翻译队友
    "running": False,           # 翻译开关（开始/停止）
    "voice": "Cherry",          # 译文播报音色
    "play_audio": True,         # 播放译文声音（TTS）
    "auto_copy": False,         # 译文定稿自动复制到剪贴板
    "mate_filter": True,        # 队友流语音频段过滤（抑制枪声等噪声）
    "overlay_opacity": 0.85,
    "click_through": False,     # 悬浮窗鼠标穿透
    "max_bubbles": 8,
    "mic_device": None,         # None=默认
    "loopback_device": None,    # None=默认扬声器回环
    "output_device": None,      # None=默认扬声器（TTS 播报）
}


class Config:
    def __init__(self):
        os.makedirs(APP_DIR, exist_ok=True)
        self.data = dict(DEFAULTS)
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    self.data.update(json.load(f))
            except Exception:
                pass
        if not self.data["device_id"]:
            self.data["device_id"] = uuid.uuid4().hex
            self.save()

    def __getitem__(self, k):
        return self.data.get(k, DEFAULTS.get(k))

    def __setitem__(self, k, v):
        self.data[k] = v

    def save(self):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
