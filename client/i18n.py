"""界面多语言：tr(中文) -> 当前界面语言的文案。

约定：源代码里的用户可见字符串保持中文，tr() 按启动时选择的语言映射。
目前支持 zh / en。新增语言在 _MAP 里加词条即可；未覆盖的词条回退中文。
"""
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QLabel, QComboBox,
                               QPushButton)

_LANG = "zh"

# zh -> en
_MAP = {
    # 导航 / 通用
    "翻译": "Translate", "字幕": "Subtitle", "字幕窗口": "Subtitle Window",
    "测试": "Test", "按键测试": "Key Test", "记录": "History",
    "翻译记录": "History", "推广": "Referral", "购买": "Store",
    "购买中心": "Store", "教程": "Guide", "设置教程": "Guide",
    "设置": "Settings", "联系我们": "Contact", "联系": "Contact",
    "退出": "Quit", "退出登录": "Log out", "保存": "Save", "取消": "Cancel",
    "复制": "Copy", "关闭": "Close",
    # 翻译页
    "准备就绪": "Ready", "翻译中…": "Translating…",
    "开始翻译": "Start", "停止翻译": "Stop",
    "播报译文": "Speak translation", "播报译文: 开": "TTS: On",
    "播报译文: 关": "TTS: Off", "当前音色": "Voice",
    "余额": "Balance", "免费额度": "Trial", "时长卡 / 订阅": "Card / Sub",
    "新用户免费额度": "Trial quota", "开通订阅": "Subscribe",
    "购买时长卡": "Buy card", "时长卡": "Time card",
    "订阅未开通": "No subscription", "订阅中": "Subscribed",
    "免费额度已用完": "Trial used up",
    "开通订阅或购买时长卡继续使用": "Subscribe or buy a card to continue",
    "到期": "expires", "优先抵扣翻译时长": "deducted first",
    # 字幕页
    "字幕悬浮窗": "Subtitle Overlay", "显示/隐藏字幕窗": "Show / Hide overlay",
    "穿透: 开": "Click-through: On", "穿透: 关": "Click-through: Off",
    "不透明度": "Opacity",
    "提示：开启穿透后游戏中鼠标点击会直接穿过字幕，可在托盘菜单重新打开设置。":
        "Tip: with click-through on, clicks pass through subtitles; "
        "reopen settings via the tray menu.",
    "预览": "Preview", "原文颜色": "Source color", "译文颜色": "Translation color",
    "选择颜色": "Pick color",
    # 测试页
    "按住绑定的热键说话": "Hold your bound key to talk",
    "说话中（松开完成）": "Speaking… (release to finish)",
    "麦克风": "Mic", "队友回环": "Teammate loopback",
    "开始测试（拉起自翻通道）": "Start test (self channel)",
    "停止测试": "Stop test", "通道": "Channel",
    # 设置页
    "运行模式": "Mode",
    "直连 DashScope（个人测试，填自己的 Key）": "Direct DashScope (your own key)",
    "计费服务器（发行版）": "Billing server (release)",
    "API Key": "API Key", "模型地址": "Model URL", "模型名": "Model",
    "服务器地址": "Server", "连通性": "Connectivity", "测试连接": "Test",
    "测试中…": "Testing…", "连接成功": "Connected", "服务器可达": "Server reachable",
    "自翻热键(按住说话)": "PTT key (hold to talk)", "点击录入": "Record",
    "请按下按键或鼠标键…": "Press a key or mouse button…",
    "队友语音翻译成": "Teammates →", "我的语音翻译成": "Me →",
    "回环设备": "Loopback device", "播报输出设备": "TTS output",
    "默认麦克风": "Default mic", "默认扬声器回环": "Default loopback",
    "默认扬声器": "Default speaker",
    "播放译文声音（TTS 播报）": "Play translated audio (TTS)",
    "试听播报": "Test audio",
    "译文定稿后自动复制到剪贴板": "Auto-copy finished translations",
    "队友流语音频段过滤（抑制枪声/脚步声噪声）":
        "Speech-band filter for teammates (reduce gunshot/footstep noise)",
    "悬浮窗不透明度": "Overlay opacity", "最多保留条数": "Max lines",
    "鼠标穿透悬浮窗（游戏中点不到字幕）": "Click-through overlay",
    "界面语言": "UI language", "✓ 改动即时生效，无需重启": "✓ Changes apply instantly",
    # 悬浮窗
    "队友✓": "Mate✓", "队友✗": "Mate✗", "穿透": "Pin", "充值": "Top up",
    "收起为顶栏 / 展开": "Collapse / expand",
    "就绪": "ready", "翻译中": "live",
    # 托盘
    "打开主窗口": "Open main window", "显示字幕窗": "Show overlay",
    "字幕穿透": "Overlay click-through",
    "已最小化到托盘，右键托盘图标可退出":
        "Minimized to tray — right-click the tray icon to quit",
    # 登录
    "登录": "Login", "注册": "Register", "游客模式": "Guest",
    "用户名": "Username", "密码": "Password", "推广码(选填)": "Ref code (optional)",
    "邀请返利": "Referral bonus",
    # 其他
    "清空记录": "Clear", "我的推广码": "My ref code", "累计返利": "Total bonus",
    "邀请人数": "Invited", "商品": "Products", "订单": "Order",
    "扫码支付": "Scan to pay", "复制支付链接": "Copy pay link",
    "等待支付…": "Waiting for payment…", "支付成功": "Paid",
    "充值成功": "Top-up success", "支付成功，权益已到账！":
        "Payment received, credits added!",
    "额度不足，请到购买中心充值": "Balance low — please top up",
    "打开设置面板": "Open settings", "点击下方按钮打开设置面板。":
        "Click below to open settings.",
    "模式已切换": "Mode changed", "运行模式已更改，请重启客户端生效。":
        "Mode changed — restart required.",
    "运行模式已切换": "Mode switched",
    "直连/服务器模式切换需要重启客户端。\n现在立即重启？":
        "Switching modes needs a restart.\nRestart now?",
    "已退出登录，请重启客户端。": "Logged out. Please restart.",
    "已退出": "Logged out",
    "说话中…（松开完成）": "Speaking… (release to finish)",
    "未开启翻译/测试 — 先点「开始翻译」或测试页":
        "Not translating — press Start or use the Test page",
    "自翻通道未就绪，检查 Key/网络": "Self channel not ready — check key/network",
    "未登录": "Not signed in", "本地直连": "Direct mode",
    "选择语言 / Choose language": "选择语言 / Choose language",
}


def set_lang(lang: str):
    global _LANG
    _LANG = lang or "zh"


def lang() -> str:
    return _LANG


def tr(s: str) -> str:
    """中文原文 -> 界面语言。en 未覆盖时回退中文。"""
    if _LANG == "zh" or not s:
        return s
    return _MAP.get(s, s)


class LanguageDialog(QDialog):
    """首次启动的语言选择框。"""

    def __init__(self, cur: str = "zh"):
        super().__init__()
        self.setWindowTitle("VTranslate")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("选择界面语言 / Choose language"))
        self.combo = QComboBox()
        self.combo.addItem("中文", "zh")
        self.combo.addItem("English", "en")
        self.combo.setCurrentIndex(0 if cur == "zh" else 1)
        lay.addWidget(self.combo)
        ok = QPushButton("OK")
        ok.clicked.connect(self.accept)
        lay.addWidget(ok)
        self.setFixedWidth(240)

    def selected(self) -> str:
        return self.combo.currentData()
