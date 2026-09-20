"""瓦语通风格主窗：左侧导航 + 页面堆栈 + 底部余额栏。

页面：翻译 / 字幕窗口 / 翻译记录 / 推广 / 购买中心 / 设置教程 / 设置 / 联系我们
"""
import io
import logging
import time
import webbrowser

import qrcode
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QWidget, QMainWindow, QVBoxLayout, QHBoxLayout,
                               QLabel, QPushButton, QComboBox, QRadioButton,
                               QButtonGroup, QStackedWidget, QFrame,
                               QProgressBar, QListWidget, QListWidgetItem,
                               QScrollArea, QMessageBox, QApplication)

from history import History

log = logging.getLogger("main_window")

ACCENT = "#5cb8d9"


def _fmt_ts(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def _card() -> QFrame:
    f = QFrame()
    f.setObjectName("card")
    f.setStyleSheet("#card{background:#ffffff;border-radius:10px;"
                    "border:1px solid #eee}")
    return f


# ================= 翻译页 =================
class HomePage(QWidget):
    start_stop = Signal(bool)
    goto_purchase = Signal(str)      # "sub" | "card" | "money"

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self._running = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(14)

        row = QHBoxLayout()
        row.setSpacing(14)

        # ---- 翻译控制卡 ----
        card = _card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(20, 16, 20, 16)
        head = QHBoxLayout()
        head.addWidget(QLabel("翻译"))
        head.addStretch(1)
        cl.addLayout(head)

        self.chips = QLabel("")
        self.chips.setStyleSheet(f"color:{ACCENT};background:#eaf7fc;"
                                 "border-radius:9px;padding:4px 10px;"
                                 "font-size:12px")
        self.chips.setMaximumWidth(360)
        cl.addWidget(self.chips)

        self.status = QLabel("准备就绪")
        self.status.setStyleSheet("font-size:22px;font-weight:bold;"
                                  "padding:10px 0")
        cl.addWidget(self.status)

        self.toggle_btn = QPushButton("开始翻译")
        self.toggle_btn.setMinimumHeight(44)
        self.toggle_btn.setStyleSheet(
            f"QPushButton{{background:{ACCENT};color:#fff;font-size:16px;"
            "border:0;border-radius:6px}"
            "QPushButton:checked{background:#e0685f}")
        self.toggle_btn.setCheckable(True)
        self.toggle_btn.toggled.connect(self._on_toggle)
        cl.addWidget(self.toggle_btn)

        self.balance_lbl = QLabel("余额 ¥0.00  约 0 分钟")
        self.balance_lbl.setStyleSheet("color:#888;padding-top:10px")
        cl.addWidget(self.balance_lbl)
        row.addWidget(card, 3)

        # ---- 音色卡 ----
        vc = _card()
        vl = QVBoxLayout(vc)
        vl.setContentsMargins(20, 16, 20, 16)
        vl.addWidget(QLabel("当前音色"))
        self.voice_combo = QComboBox()
        self.voice_combo.currentTextChanged.connect(self._voice_changed)
        vl.addWidget(self.voice_combo)
        self.play_cb = QPushButton("播报译文: 开")
        self.play_cb.setCheckable(True)
        self.play_cb.setChecked(bool(cfg["play_audio"]))
        self.play_cb.toggled.connect(self._play_toggled)
        vl.addWidget(self.play_cb)
        vl.addStretch(1)
        row.addWidget(vc, 2)
        lay.addLayout(row)

        # ---- 权益卡 ----
        erow = QHBoxLayout()
        erow.setSpacing(14)

        ec = _card()
        el = QVBoxLayout(ec)
        el.setContentsMargins(20, 16, 20, 16)
        eh = QHBoxLayout()
        eh.addWidget(QLabel("免费额度"))
        eh.addStretch(1)
        sub_btn = QPushButton("开通订阅")
        sub_btn.setStyleSheet(f"color:{ACCENT};border:1px solid {ACCENT};"
                              "border-radius:4px;padding:2px 10px;background:#fff")
        sub_btn.clicked.connect(lambda: self.goto_purchase.emit("sub"))
        eh.addWidget(sub_btn)
        el.addLayout(eh)
        self.trial_title = QLabel("新用户免费额度")
        self.trial_title.setStyleSheet("font-weight:bold;font-size:15px")
        el.addWidget(self.trial_title)
        self.trial_bar = QProgressBar()
        self.trial_bar.setRange(0, 100)
        self.trial_bar.setTextVisible(False)
        self.trial_bar.setFixedHeight(8)
        self.trial_bar.setStyleSheet(
            f"QProgressBar{{background:#eee;border-radius:4px}}"
            f"QProgressBar::chunk{{background:{ACCENT};border-radius:4px}}")
        el.addWidget(self.trial_bar)
        self.trial_info = QLabel("")
        self.trial_info.setStyleSheet("color:#999;font-size:12px")
        el.addWidget(self.trial_info)
        erow.addWidget(ec)

        cc = _card()
        cl2 = QVBoxLayout(cc)
        cl2.setContentsMargins(20, 16, 20, 16)
        ch = QHBoxLayout()
        ch.addWidget(QLabel("时长卡 / 订阅"))
        ch.addStretch(1)
        ch.addWidget(QLabel(""))
        cl2.addLayout(ch)
        self.card_lbl = QLabel("时长卡 0 分钟")
        self.card_lbl.setStyleSheet("font-weight:bold;font-size:15px")
        self.sub_lbl = QLabel("订阅未开通")
        self.sub_lbl.setStyleSheet("color:#999;font-size:12px")
        buy = QPushButton("购买时长卡")
        buy.setStyleSheet(f"background:{ACCENT};color:#fff;border:0;"
                          "border-radius:4px;padding:6px")
        buy.clicked.connect(lambda: self.goto_purchase.emit("card"))
        cl2.addWidget(self.card_lbl)
        cl2.addWidget(self.sub_lbl)
        cl2.addStretch(1)
        cl2.addWidget(buy)
        erow.addWidget(cc)
        lay.addLayout(erow)
        lay.addStretch(1)

        self.refresh_chips()

    def refresh_chips(self):
        hk = self.cfg["hotkey"]
        dev = "鼠标" if hk.get("device") == "mouse" else "键盘"
        play = "播报译文" if self.cfg["play_audio"] else "仅文本"
        self.chips.setText(
            f"队友→{self.cfg['target_mate']} · 按住{dev}{hk.get('key','')}自翻"
            f"→{self.cfg['target_self']} · {play}")

    def set_voices(self, voices: list):
        self.voice_combo.blockSignals(True)
        self.voice_combo.clear()
        self.voice_combo.addItems(voices)
        if self.cfg["voice"] in voices:
            self.voice_combo.setCurrentText(self.cfg["voice"])
        self.voice_combo.blockSignals(False)

    def _voice_changed(self, v):
        self.cfg["voice"] = v
        self.cfg.save()

    def _play_toggled(self, on):
        self.cfg["play_audio"] = on
        self.cfg.save()
        self.play_cb.setText(f"播报译文: {'开' if on else '关'}")
        self.refresh_chips()

    def _on_toggle(self, on):
        self._running = on
        self.toggle_btn.setText("停止翻译" if on else "开始翻译")
        self.status.setText("翻译中…" if on else "准备就绪")
        self.start_stop.emit(on)

    def set_status_text(self, s: str):
        if not self._running:
            self.status.setText(s)

    def stop_toggle(self):
        if self._running:
            self.toggle_btn.setChecked(False)

    def set_trial_total(self, seconds: int):
        self._trial_total = max(1, seconds)

    def update_entitlement(self, e: dict):
        self.balance_lbl.setText(
            f"余额 ¥{e['money_yuan']}  约 {e['money_minutes']} 分钟")
        total = getattr(self, "_trial_total", 300)
        if e.get("trial_seconds", 0) > 0 and e.get("trial_expire_at", 0) > 0:
            pct = min(100, int(e["trial_seconds"] / total * 100))
            self.trial_title.setText(
                f"新用户免费额度  剩余 {e['trial_seconds']} 秒")
            self.trial_info.setText(
                f"{_fmt_ts(e['trial_expire_at'])} 到期 · 优先抵扣翻译时长")
        else:
            pct = 0
            self.trial_title.setText("免费额度已用完")
            self.trial_info.setText("开通订阅或购买时长卡继续使用")
        self.trial_bar.setValue(pct)
        self.card_lbl.setText(f"时长卡 {e['credit_seconds']//60} 分钟")
        if e["sub_active"]:
            self.sub_lbl.setText(f"订阅中 · {_fmt_ts(e['sub_expire_at'])} 到期")
        else:
            self.sub_lbl.setText("订阅未开通")


# ================= 字幕窗口页 =================
class SubtitlePage(QWidget):
    show_overlay = Signal(bool)
    apply_overlay = Signal()

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(12)
        card = _card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(20, 16, 20, 16)
        cl.addWidget(QLabel("字幕悬浮窗"))

        row = QHBoxLayout()
        self.show_btn = QPushButton("显示/隐藏字幕窗")
        self.show_btn.clicked.connect(lambda: self.show_overlay.emit(True))
        row.addWidget(self.show_btn)

        self.through_btn = QPushButton()
        self.through_btn.setCheckable(True)
        self.through_btn.setChecked(bool(cfg["click_through"]))
        self.through_btn.setText("穿透: 开" if cfg["click_through"] else "穿透: 关")
        self.through_btn.toggled.connect(self._through)
        row.addWidget(self.through_btn)
        row.addStretch(1)
        cl.addLayout(row)

        op_row = QHBoxLayout()
        op_row.addWidget(QLabel("不透明度"))
        self.opacity = QComboBox()
        for v in (40, 55, 70, 85, 100):
            self.opacity.addItem(f"{v}%", v / 100)
        self.opacity.setCurrentIndex(
            min(range(5), key=lambda i: abs((40 + i * 15)
                 - float(cfg["overlay_opacity"]) * 100)))
        self.opacity.currentIndexChanged.connect(self._opacity)
        op_row.addWidget(self.opacity)
        op_row.addStretch(1)
        cl.addLayout(op_row)

        tip = QLabel("提示：开启穿透后游戏中鼠标点击会直接穿过字幕，"
                     "可在托盘菜单重新打开设置。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#999;font-size:12px")
        cl.addWidget(tip)
        lay.addWidget(card)
        lay.addStretch(1)

    def _through(self, on):
        self.through_btn.setText(f"穿透: {'开' if on else '关'}")
        self.cfg["click_through"] = on
        self.cfg.save()
        self.apply_overlay.emit()

    def _opacity(self, _):
        self.cfg["overlay_opacity"] = self.opacity.currentData()
        self.cfg.save()
        self.apply_overlay.emit()


# ================= 翻译记录页 =================
class HistoryPage(QWidget):
    def __init__(self, history: History):
        super().__init__()
        self.history = history
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        head = QHBoxLayout()
        head.addWidget(QLabel("翻译记录"))
        head.addStretch(1)
        clr = QPushButton("清空")
        clr.clicked.connect(self._clear)
        head.addWidget(clr)
        lay.addLayout(head)
        self.listw = QListWidget()
        self.listw.setStyleSheet("QListWidget{background:#fff;border-radius:8px;"
                                 "border:1px solid #eee}")
        lay.addWidget(self.listw)

    def refresh(self):
        self.listw.clear()
        for r in self.history.list(200):
            t = time.strftime("%H:%M:%S", time.localtime(r["t"]))
            item = QListWidgetItem(
                f"[{t}] {r['speaker']}\n{r['src']}\n{r['dst']}")
            self.listw.addItem(item)

    def _clear(self):
        self.history.clear()
        self.refresh()

    def showEvent(self, e):
        self.refresh()
        super().showEvent(e)


# ================= 推广页 =================
class ReferralPage(QWidget):
    def __init__(self, api):
        super().__init__()
        self.api = api
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        card = _card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(20, 16, 20, 16)
        cl.addWidget(QLabel("邀请好友"))
        tip = QLabel("好友注册时填写你的推广码，其每笔充值你将获得 "
                     "实付金额一定比例的返利（返到余额）。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#888")
        cl.addWidget(tip)

        row = QHBoxLayout()
        self.code_lbl = QLabel("推广码: -")
        self.code_lbl.setStyleSheet(
            "font-size:18px;font-weight:bold;color:#333")
        cp = QPushButton("复制")
        cp.clicked.connect(self._copy)
        row.addWidget(self.code_lbl)
        row.addWidget(cp)
        row.addStretch(1)
        cl.addLayout(row)

        self.stats_lbl = QLabel("")
        self.stats_lbl.setStyleSheet("color:#666")
        cl.addWidget(self.stats_lbl)
        lay.addWidget(card)
        lay.addStretch(1)

    def _copy(self):
        code = self.code_lbl.text().replace("推广码: ", "")
        QApplication.clipboard().setText(code)

    def refresh(self):
        try:
            s = self.api.ref_stats()
            if s.get("code") == 1:
                self.code_lbl.setText(f"推广码: {s['ref_code']}")
                self.stats_lbl.setText(
                    f"已邀请 {s['invited']} 人 · 累计返利 ¥{s['earned_yuan']}"
                    f"（比例 {s['bonus_percent']}%）")
        except Exception:
            pass

    def showEvent(self, e):
        self.refresh()
        super().showEvent(e)


# ================= 购买中心页 =================
class PurchasePage(QWidget):
    paid = Signal(dict)

    KIND_LABEL = {"money": "余额充值", "card": "时长卡", "sub": "订阅"}

    def __init__(self, api):
        super().__init__()
        self.api = api
        self.out_trade_no = None
        self._page_url = ""
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)

        card = _card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(20, 16, 20, 16)
        cl.addWidget(QLabel("购买中心"))

        self.product_combo = QComboBox()
        try:
            prods = self.api.products().get("products", [])
        except Exception as e:
            prods = []
            cl.addWidget(QLabel(f"获取商品失败: {e}"))
        self._prods = prods
        for p in prods:
            kind = self.KIND_LABEL.get(p["kind"], p["kind"])
            if p["kind"] == "money":
                unit = "余额"
            elif p["kind"] == "card":
                unit = f"{p['seconds']//60}分钟"
            else:
                unit = f"{p['days']}天"
            self.product_combo.addItem(
                f"[{kind}] {p['name']}  ¥{p['money']}（{unit}）", p["code"])
        cl.addWidget(self.product_combo)

        pay_row = QHBoxLayout()
        pay_row.addWidget(QLabel("支付方式"))
        self.pay_group = QButtonGroup(self)
        for i, (val, text) in enumerate((("alipay", "支付宝"), ("wxpay", "微信"))):
            r = QRadioButton(text)
            r.setChecked(i == 0)
            self.pay_group.addButton(r)
            r.setProperty("val", val)
            pay_row.addWidget(r)
        pay_row.addStretch(1)
        cl.addLayout(pay_row)

        create_btn = QPushButton("创建订单")
        create_btn.setStyleSheet(f"background:{ACCENT};color:#fff;border:0;"
                                 "border-radius:5px;padding:8px")
        create_btn.clicked.connect(self._create)
        cl.addWidget(create_btn)

        self.qr_lbl = QLabel("")
        self.qr_lbl.setAlignment(Qt.AlignCenter)
        self.qr_lbl.setMinimumHeight(230)
        cl.addWidget(self.qr_lbl)

        row = QHBoxLayout()
        self.open_btn = QPushButton("浏览器打开收银台")
        self.open_btn.clicked.connect(self._open_page)
        self.open_btn.setEnabled(False)
        self.status_lbl = QLabel("")
        row.addWidget(self.open_btn)
        row.addWidget(self.status_lbl, 1)
        cl.addLayout(row)
        lay.addWidget(card)
        lay.addStretch(1)

        self._poll = QTimer(self)
        self._poll.setInterval(3000)
        self._poll.timeout.connect(self._check_status)

    def select_kind(self, kind: str):
        for i in range(self.product_combo.count()):
            code = self.product_combo.itemData(i)
            prod = next((p for p in self._prods if p["code"] == code), None)
            if prod and prod["kind"] == kind:
                self.product_combo.setCurrentIndex(i)
                return

    def _create(self):
        code = self.product_combo.currentData()
        if not code:
            return
        pay_type = self.pay_group.checkedButton().property("val")
        try:
            resp = self.api.pay_create(code, pay_type)
        except Exception as e:
            self.status_lbl.setText(f"下单失败: {e}")
            return
        if resp.get("code") != 1:
            self.status_lbl.setText(f"下单失败: {resp.get('msg')}")
            return
        self.out_trade_no = resp["out_trade_no"]
        self._page_url = resp.get("page_url", "")
        self.open_btn.setEnabled(bool(self._page_url))
        qr_content = resp.get("qrcode") or resp.get("payurl") or self._page_url
        if qr_content:
            self._show_qr(qr_content)
            self.status_lbl.setText("请扫码支付")
        else:
            self.status_lbl.setText("请用浏览器打开收银台支付")
        self._poll.start()

    def _show_qr(self, content: str):
        img = qrcode.make(content)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        qimg = QImage.fromData(buf.getvalue())
        self.qr_lbl.setPixmap(QPixmap.fromImage(qimg).scaled(
            220, 220, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _open_page(self):
        if self._page_url:
            webbrowser.open(self._page_url)

    def _check_status(self):
        if not self.out_trade_no:
            return
        try:
            resp = self.api.pay_status(self.out_trade_no)
        except Exception:
            return
        if resp.get("status") == "paid":
            self._poll.stop()
            self.status_lbl.setText("支付成功，已到账")
            self.paid.emit(resp)


# ================= 静态页 =================
class _TextPage(QWidget):
    def __init__(self, title: str, text: str):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        card = _card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(20, 16, 20, 16)
        t = QLabel(title)
        t.setStyleSheet("font-size:16px;font-weight:bold")
        cl.addWidget(t)
        body = QLabel(text)
        body.setWordWrap(True)
        body.setStyleSheet("color:#555;line-height:1.6")
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        cl.addWidget(body)
        lay.addWidget(card)
        lay.addStretch(1)


TUTORIAL_TEXT = """\
═══════ VTranslate 配置说明书 ═══════

【一、运行模式】（设置页顶部切换，保存到本机 %APPDATA%\\VTranslate\\config.json）

● 直连 DashScope（个人测试，推荐现在用）
  - 需要去阿里云百炼控制台申请 API Key：
    1) 打开 https://bailian.console.aliyun.com/ 注册/登录
    2) 左侧「API-KEY 管理」→ 创建新的 API-KEY（形如 sk-xxxx）
    3) 粘贴到 设置 → 「API Key」，保存
  - 模型地址默认 wss://dashscope.aliyuncs.com/api-ws/v1/realtime（国内）
    海外可用 wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime
  - 模型名默认 qwen3.8-livetranslate-flash-realtime
  - 直连模式不计费：你按 DashScope 官方单价自负 token 费用
    （输入音频约 7 token/秒，文本识别另计）

● 计费服务器（发行版）
  - 部署 server/ 目录下的 FastAPI 服务后，在此填服务器地址
  - 该模式下需登录账号，翻译时长由服务器计费

【二、音频】
- 队友语音：通过 WASAPI 系统扬声器回环采集（游戏语音从哪出声就采哪）
  设置页「回环设备」默认自动选默认扬声器；用耳机/外接声卡时可手动指定
- 自翻语音：默认麦克风，可在设置页指定输入设备
- 播报输出：译文 TTS 从「播报输出设备」播放，默认系统扬声器

【三、使用】
1. 点主窗「开始翻译」→ 字幕悬浮窗出现并实时双语显示队友发言
   （多人说话时自动区分 好友1/好友2…）
2. 按住自翻热键（默认鼠标侧键 X1）说话，松开显示你的译文
   改键：设置 → 自翻热键 → 点击录入 → 按下任意键盘/鼠标键
3. 悬浮窗可拖动；「穿透」开启后鼠标点不到字幕（游戏内推荐开）
4. 译文可设置自动复制到剪贴板；开启「播放译文声音」后按所选音色播报
5. 翻译记录页保存最近 500 条（仅存本机，不上传）

【四、计费说明（服务器模式）】
扣费优先级：订阅 > 免费额度 > 时长卡 > 余额按量
余额按量单价见首页提示；仅对实际说话的时长计费，静音不计。

【五、常见问题】
Q 没有翻译输出？  A 检查 API Key 是否正确、余额/试用是否耗尽；
  回环设备是否选对了出声的那个；服务器模式检查服务器地址
Q 游戏内看不到字幕？  A 游戏需以无边框/窗口化运行；悬浮窗被全屏独占
  游戏盖住是 Windows 限制，请切换无边框窗口模式
Q 鼠标侧键在游戏里没反应？  A 换个键录入；部分游戏会吃掉鼠标按键事件
Q 会不会封号？  A 不会。本程序只读写音频设备，不接触游戏进程、
  内存、文件与网络数据，与普通录屏/语音软件同级

开源仓库：https://gitee.com/chunfff/v-translation
"""

CONTACT_TEXT = """\
问题反馈 / 商务合作：
  QQ 群：在官网或推广页获取最新群号
  邮箱：support@example.com（请替换为你的联系方式）

开源仓库：https://gitee.com/chunfff/v-translation
"""


class TutorialPage(_TextPage):
    def __init__(self):
        super().__init__("设置教程", TUTORIAL_TEXT)


class ContactPage(_TextPage):
    def __init__(self):
        super().__init__("联系我们", CONTACT_TEXT)


# ================= 主窗 =================
class MainWindow(QMainWindow):
    start_stop = Signal(bool)
    show_overlay = Signal(bool)
    apply_overlay = Signal()
    open_settings = Signal()
    logout = Signal()

    NAV = [
        ("翻译", "home"), ("字幕窗口", "subtitle"), ("翻译记录", "history"),
        ("推广", "ref"), ("购买中心", "buy"), ("设置教程", "tutorial"),
        ("设置", "settings"), ("联系我们", "contact"),
    ]

    def __init__(self, cfg, api, history):
        super().__init__()
        self.cfg = cfg
        self.api = api
        self._direct = cfg["mode"] == "direct"
        self.setWindowTitle("VTranslate")
        self.resize(960, 640)

        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- 侧边栏 ----
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(170)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(10, 14, 10, 10)
        sl.setSpacing(2)
        logo = QLabel("VTranslate")
        logo.setStyleSheet(f"font-size:18px;font-weight:bold;color:{ACCENT};"
                           "padding:4px 8px 12px")
        sl.addWidget(logo)

        self._nav_btns: list[QPushButton] = []
        self._nav_keys: list[str] = []
        self.pages = QStackedWidget()
        for i, (name, key) in enumerate(self.NAV):
            # 直连模式隐藏依赖服务器的页面
            if self._direct and key in ("ref", "buy"):
                continue
            b = QPushButton(name)
            b.setObjectName("nav")
            b.setCheckable(True)
            b.clicked.connect(lambda _, k=key: self.switch_to(k))
            sl.addWidget(b)
            self._nav_btns.append(b)
            self._nav_keys.append(key)
        sl.addStretch(1)

        self.side_balance = QLabel("余额 ¥0.00")
        self.side_balance.setStyleSheet("font-weight:bold;padding:4px 8px")
        self.side_user = QLabel("未登录")
        self.side_user.setStyleSheet("color:#888;padding:0 8px")
        out = QPushButton("退出登录")
        out.setObjectName("nav")
        out.clicked.connect(self.logout.emit)
        sl.addWidget(self.side_balance)
        sl.addWidget(self.side_user)
        sl.addWidget(out)
        root.addWidget(side)

        # ---- 右侧 ----
        right = QFrame()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)

        self.banner = QLabel("")
        self.banner.setObjectName("banner")
        self.banner.setWordWrap(True)
        self.banner.setVisible(False)
        rl.addWidget(self.banner)

        # 页面
        self._page_map = {}
        self.home = HomePage(cfg)
        self.subtitle = SubtitlePage(cfg)
        self.history_page = HistoryPage(history)
        self.tutorial = TutorialPage()
        self.contact = ContactPage()
        self.settings_holder = _TextPage(
            "设置", "点击下方按钮打开设置面板。")
        open_btn = QPushButton("打开设置面板")
        open_btn.setStyleSheet(f"background:{ACCENT};color:#fff;border:0;"
                               "border-radius:5px;padding:8px;max-width:200px")
        open_btn.clicked.connect(self.open_settings.emit)
        self.settings_holder.layout().itemAt(0).widget().layout()\
            .addWidget(open_btn)

        self._page_map = {
            "home": self.home, "subtitle": self.subtitle,
            "history": self.history_page, "tutorial": self.tutorial,
            "settings": self.settings_holder, "contact": self.contact,
        }
        if not self._direct:
            self.ref_page = ReferralPage(api)
            self.buy_page = PurchasePage(api)
            self._page_map["ref"] = self.ref_page
            self._page_map["buy"] = self.buy_page
        for key in self._nav_keys:
            self.pages.addWidget(self._page_map[key])
        rl.addWidget(self.pages, 1)
        root.addWidget(right, 1)
        self.setCentralWidget(central)

        # 信号
        self.home.start_stop.connect(self.start_stop)
        self.home.goto_purchase.connect(self._goto_purchase)
        self.subtitle.show_overlay.connect(self.show_overlay)
        self.subtitle.apply_overlay.connect(self.apply_overlay)
        if not self._direct:
            self.buy_page.paid.connect(self._on_paid)

        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ background:#f6f3ee; color:#333;
                font-family:'Microsoft YaHei UI'; }}
            #sidebar {{ background:#fff; border-right:1px solid #eee; }}
            #nav {{ text-align:left; padding:9px 12px; border:0;
                border-radius:6px; color:#444; font-size:14px; }}
            #nav:hover {{ background:#f0f7fa; }}
            #nav:checked {{ background:{ACCENT}; color:#fff; }}
            #banner {{ background:#fff6e5; color:#9a7b2d; padding:8px 14px;
                border-bottom:1px solid #f0e3c0; }}
            QPushButton {{ font-family:'Microsoft YaHei UI'; }}
        """)
        self.switch(0)

    def switch(self, idx: int):
        self.pages.setCurrentIndex(idx)
        for i, b in enumerate(self._nav_btns):
            b.setChecked(i == idx)

    def switch_to(self, key: str):
        if key in self._nav_keys:
            self.switch(self._nav_keys.index(key))

    def _goto_purchase(self, kind: str):
        if self._direct:
            return
        self.switch_to("buy")
        self.buy_page.select_kind(kind)

    def _on_paid(self, ent: dict):
        self.update_entitlement(ent)
        QMessageBox.information(self, "充值成功", "支付成功，权益已到账！")

    # ---- 数据刷新 ----
    def set_announcement(self, text: str):
        self.banner.setText(f"📢 {text}")
        self.banner.setVisible(bool(text))

    def set_voices(self, voices: list):
        self.home.set_voices(voices)

    def update_entitlement(self, e: dict):
        self.home.update_entitlement(e)
        self.side_balance.setText(f"余额 ¥{e['money_yuan']}")
        self.side_user.setText(e.get("username", ""))

    def set_user(self, name: str):
        self.side_user.setText(name)
