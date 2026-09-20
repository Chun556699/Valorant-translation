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
                               QScrollArea, QMessageBox, QApplication,
                               QSizeGrip)

from history import History

log = logging.getLogger("main_window")

# ---- 深色主题配色 ----
BG = "#14161b"
PANEL = "#191c22"
CARD = "#20242e"
BORDER = "#2b303c"
TEXT = "#e8ebf2"
MUT = "#98a0b3"
ACCENT = "#ff4655"          # Valorant 红
DANGER = "#e0655f"
OK = "#46c27e"

APP_QSS = f"""
    * {{ font-family:'Microsoft YaHei UI'; font-size:12px; }}
    QWidget {{ background:{BG}; color:{TEXT}; }}
    QDialog, QMessageBox {{ background:{BG}; }}
    QLabel {{ background:transparent; }}
    QPushButton {{ background:#2a2f3a; color:{TEXT}; border:1px solid {BORDER};
        border-radius:6px; padding:5px 10px; }}
    QPushButton:hover {{ background:#333947; }}
    QPushButton:checked {{ background:{ACCENT}; color:#fff; border-color:{ACCENT}; }}
    QPushButton:disabled {{ color:#5a6272; }}
    QLineEdit, QSpinBox, QComboBox, QTextEdit, QTextBrowser {{
        background:#1a1d24; color:{TEXT}; border:1px solid {BORDER};
        border-radius:6px; padding:5px 8px; }}
    QComboBox QAbstractItemView {{ background:#22262f; color:{TEXT};
        selection-background-color:{ACCENT}; border:1px solid {BORDER}; }}
    QCheckBox, QRadioButton {{ spacing:6px; background:transparent; }}
    QProgressBar {{ background:#262b36; border:0; border-radius:5px;
        text-align:center; }}
    QProgressBar::chunk {{ background:{ACCENT}; border-radius:5px; }}
    QListWidget {{ background:{CARD}; border:1px solid {BORDER};
        border-radius:8px; }}
    QListWidget::item {{ padding:8px; border-bottom:1px solid {BORDER}; }}
    QScrollArea, QScrollArea > QWidget > QWidget {{ background:transparent; }}
    QScrollBar:vertical {{ background:transparent; width:8px; }}
    QScrollBar::handle:vertical {{ background:#3a4150; border-radius:4px;
        min-height:30px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
    QMenu {{ background:#22262f; color:{TEXT}; border:1px solid {BORDER}; }}
    QMenu::item:selected {{ background:{ACCENT}; }}
    QTabWidget::pane {{ border:1px solid {BORDER}; border-radius:6px; }}
    QTabBar::tab {{ background:#1a1d24; color:{MUT}; padding:7px 16px;
        border-top-left-radius:6px; border-top-right-radius:6px; }}
    QTabBar::tab:selected {{ background:{CARD}; color:{TEXT}; }}
    QToolTip {{ background:#2a2f3a; color:{TEXT}; border:1px solid {BORDER}; }}
    QSizeGrip {{ background:transparent; }}
"""


def _fmt_ts(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def _card() -> QFrame:
    f = QFrame()
    f.setObjectName("card")
    f.setStyleSheet(f"#card{{background:{CARD};border-radius:12px;"
                    f"border:1px solid {BORDER}}}")
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
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(10)

        row = QHBoxLayout()
        row.setSpacing(10)

        # ---- 翻译控制卡 ----
        card = _card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 10, 14, 10)
        head = QHBoxLayout()
        head.addWidget(QLabel("翻译"))
        head.addStretch(1)
        cl.addLayout(head)

        self.chips = QLabel("")
        self.chips.setStyleSheet(f"color:{ACCENT};background:#3a2028;"
                                 "border-radius:9px;padding:4px 10px;"
                                 "font-size:12px")
        self.chips.setMaximumWidth(360)
        cl.addWidget(self.chips)

        self.status = QLabel("准备就绪")
        self.status.setStyleSheet("font-size:18px;font-weight:bold;"
                                  "padding:10px 0")
        cl.addWidget(self.status)

        self.toggle_btn = QPushButton("开始翻译")
        self.toggle_btn.setMinimumHeight(38)
        self.toggle_btn.setStyleSheet(
            f"QPushButton{{background:{ACCENT};color:#fff;font-size:14px;"
            "border:0;border-radius:8px}"
            "QPushButton:hover{{background:#e03a48}}"
            f"QPushButton:checked{{background:{DANGER}}}")
        self.toggle_btn.setCheckable(True)
        self.toggle_btn.toggled.connect(self._on_toggle)
        cl.addWidget(self.toggle_btn)

        self.balance_lbl = QLabel("余额 ¥0.00  约 0 分钟")
        self.balance_lbl.setStyleSheet(f"color:{MUT};padding-top:10px")
        cl.addWidget(self.balance_lbl)
        row.addWidget(card, 3)

        # ---- 音色卡 ----
        vc = _card()
        vl = QVBoxLayout(vc)
        vl.setContentsMargins(14, 10, 14, 10)
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
        erow.setSpacing(10)

        ec = _card()
        el = QVBoxLayout(ec)
        el.setContentsMargins(14, 10, 14, 10)
        eh = QHBoxLayout()
        eh.addWidget(QLabel("免费额度"))
        eh.addStretch(1)
        sub_btn = QPushButton("开通订阅")
        sub_btn.setStyleSheet(f"color:{ACCENT};border:1px solid {ACCENT};"
                              f"border-radius:4px;padding:2px 10px;background:{CARD}")
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
            "QProgressBar{background:#262b36;border-radius:4px}"
            f"QProgressBar::chunk{{background:{ACCENT};border-radius:4px}}")
        el.addWidget(self.trial_bar)
        self.trial_info = QLabel("")
        self.trial_info.setStyleSheet(f"color:{MUT};font-size:12px")
        el.addWidget(self.trial_info)
        erow.addWidget(ec)

        cc = _card()
        cl2 = QVBoxLayout(cc)
        cl2.setContentsMargins(14, 10, 14, 10)
        ch = QHBoxLayout()
        ch.addWidget(QLabel("时长卡 / 订阅"))
        ch.addStretch(1)
        ch.addWidget(QLabel(""))
        cl2.addLayout(ch)
        self.card_lbl = QLabel("时长卡 0 分钟")
        self.card_lbl.setStyleSheet("font-weight:bold;font-size:15px")
        self.sub_lbl = QLabel("订阅未开通")
        self.sub_lbl.setStyleSheet(f"color:{MUT};font-size:12px")
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
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(12)
        card = _card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 10, 14, 10)
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
        tip.setStyleSheet(f"color:{MUT};font-size:12px")
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


# ================= 按键测试页 =================
class TestPage(QWidget):
    test_self = Signal(bool)   # 拉起/断开自翻通道

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(10)

        card = _card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 10, 14, 10)
        cl.addWidget(QLabel("按键与麦克风测试"))

        # 大号按键指示灯
        self.lamp = QLabel("按住绑定的热键说话")
        self.lamp.setAlignment(Qt.AlignCenter)
        self.lamp.setMinimumHeight(64)
        self._lamp_off = (f"background:#262b36;color:{MUT};border-radius:10px;"
                          "font-size:14px;font-weight:bold")
        self._lamp_on = (f"background:{DANGER};color:#fff;border-radius:10px;"
                         "font-size:16px;font-weight:bold")
        self.lamp.setStyleSheet(self._lamp_off)
        cl.addWidget(self.lamp)

        # 电平条
        for label, attr in (("麦克风", "mic_bar"), ("队友回环", "mate_bar")):
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setTextVisible(False)
            bar.setFixedHeight(10)
            bar.setStyleSheet(
                "QProgressBar{background:#262b36;border-radius:5px}"
                f"QProgressBar::chunk{{background:{ACCENT};border-radius:5px}}")
            setattr(self, attr, bar)
            row.addWidget(bar, 1)
            cl.addLayout(row)

        # 测试开关 + 状态
        row = QHBoxLayout()
        self.test_btn = QPushButton("开始测试（拉起自翻通道）")
        self.test_btn.setCheckable(True)
        self.test_btn.setStyleSheet(
            f"QPushButton{{background:{ACCENT};color:#fff;border:0;"
            "border-radius:6px;padding:8px 14px}"
            f"QPushButton:checked{{background:{DANGER}}}")
        self.test_btn.toggled.connect(self._toggled)
        row.addWidget(self.test_btn)
        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet(f"color:{MUT}")
        row.addWidget(self.status_lbl, 1)
        cl.addLayout(row)

        tip = QLabel("说明：测试模式下按住热键说话，松开后悬浮窗会显示译文；"
                     "电平条验证采集是否正常（麦克风条应随说话跳动）。"
                     "热键在「设置」页录入。")
        tip.setWordWrap(True)
        tip.setStyleSheet(f"color:{MUT};font-size:12px")
        cl.addWidget(tip)
        lay.addWidget(card)
        lay.addStretch(1)

        # 电平衰减定时器：无新数据时缓慢回落
        self._decay = QTimer(self)
        self._decay.setInterval(120)
        self._decay.timeout.connect(self._do_decay)
        self._decay.start()

    def _toggled(self, on):
        self.test_btn.setText("停止测试" if on else "开始测试（拉起自翻通道）")
        self.status_lbl.setText("连接中…" if on else "")
        self.test_self.emit(on)

    def set_pressed(self, on: bool):
        self.lamp.setText("说话中…松开完成" if on else "按住绑定的热键说话")
        self.lamp.setStyleSheet(self._lamp_on if on else self._lamp_off)

    def set_level(self, which: str, v: float):
        bar = self.mic_bar if which == "mic" else self.mate_bar
        bar.setValue(min(100, int(v * 400)))   # RMS 放大显示

    def set_status(self, s: str):
        self.status_lbl.setText(s)

    def _do_decay(self):
        for bar in (self.mic_bar, self.mate_bar):
            if bar.value() > 0:
                bar.setValue(bar.value() - 6)


# ================= 翻译记录页 =================
class HistoryPage(QWidget):
    def __init__(self, history: History):
        super().__init__()
        self.history = history
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        head = QHBoxLayout()
        head.addWidget(QLabel("翻译记录"))
        head.addStretch(1)
        clr = QPushButton("清空")
        clr.clicked.connect(self._clear)
        head.addWidget(clr)
        lay.addLayout(head)
        self.listw = QListWidget()
        self.listw.setStyleSheet(
            f"QListWidget{{background:{CARD};border-radius:8px;"
            f"border:1px solid {BORDER}}}")
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
        lay.setContentsMargins(12, 10, 12, 10)
        card = _card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 10, 14, 10)
        cl.addWidget(QLabel("邀请好友"))
        tip = QLabel("好友注册时填写你的推广码，其每笔充值你将获得 "
                     "实付金额一定比例的返利（返到余额）。")
        tip.setWordWrap(True)
        tip.setStyleSheet(f"color:{MUT}")
        cl.addWidget(tip)

        row = QHBoxLayout()
        self.code_lbl = QLabel("推广码: -")
        self.code_lbl.setStyleSheet(
            f"font-size:18px;font-weight:bold;color:{TEXT}")
        cp = QPushButton("复制")
        cp.clicked.connect(self._copy)
        row.addWidget(self.code_lbl)
        row.addWidget(cp)
        row.addStretch(1)
        cl.addLayout(row)

        self.stats_lbl = QLabel("")
        self.stats_lbl.setStyleSheet(f"color:{MUT}")
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
        lay.setContentsMargins(12, 10, 12, 10)

        card = _card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 10, 14, 10)
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
        lay.setContentsMargins(12, 10, 12, 10)
        card = _card()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 10, 14, 10)
        t = QLabel(title)
        t.setStyleSheet("font-size:14px;font-weight:bold")
        cl.addWidget(t)
        body = QLabel(text)
        body.setWordWrap(True)
        body.setStyleSheet(f"color:{MUT};line-height:1.6")
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
  - 模型地址默认你的业务空间专属域名（已预填，勿改）：
    wss://llm-5gwthr14damb7ihz.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime
    对应 REST base https://llm-5gwthr14damb7ihz.cn-beijing.maas.aliyuncs.com/api/v1
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
2. 按住自翻热键（默认鼠标侧键 X1）说话，松开显示你的译文，
   译文会自动复制到剪贴板——游戏内按 Enter 打开聊天框后 Ctrl+V 粘贴
   改键：设置 → 自翻热键 → 点击录入 → 按下任意键盘/鼠标键
3. 悬浮窗可拖动；「穿透」开启后鼠标点不到字幕（游戏内推荐开）
4. 译文可设置自动复制到剪贴板；开启「播放译文声音」后按所选音色播报
   注意：播报设备与回环设备是同一个扬声器时，播报期间回环会自动
   静音（防止译文被采回去再翻译）；有多余输出设备可在设置里把
   播报输出指定到别的设备；不需要播报可关闭
5. 队友识别太杂？开启设置里的「语音频段过滤」，枪声/脚步声会被削弱；
   游戏内语音播报（如 Spike planted）属人声无法与队友区分
6. 已内置 Valorant 术语热词：特工（含外号如"奶妈→Sage""保安→Cypher"）、
   地图、枪械、战术术语（爆能器/下包/回防/eco等）会自动准确翻译
7. 翻译记录页保存最近 500 条（仅存本机，不上传）

【四、计费说明（服务器模式）】
扣费优先级：订阅 > 免费额度 > 时长卡 > 余额按量
余额按量单价见首页提示；仅对实际说话的时长计费，静音不计。

【五、常见问题】
Q 没有翻译输出？  A 检查 API Key 是否正确、余额/试用是否耗尽；
  回环设备是否选对了出声的那个；服务器模式检查服务器地址
Q 游戏内看不到字幕？  A 【必须】游戏画面设为「无边框窗口」模式——
  全屏独占会盖住所有置顶悬浮窗，这是 Windows 限制，无解
Q 进游戏后热键失效？  A Valorant 以管理员权限运行，本程序也需同级权限：
  exe 已内置管理员清单，启动时点「是」允许即可；用 python 源码跑的
  话请以管理员身份运行终端再启动
Q 鼠标侧键在游戏里没反应？  A 确认 exe 已管理员权限启动；仍无效则
  换个键录入（部分游戏会吃掉鼠标侧键事件，键盘键更稳）
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


# ================= 自定义标题栏 =================
class _TitleBar(QFrame):
    """无边框标题栏：logo 徽标 + 标题 + 运行状态点 + 最小化/关闭。
    按住拖动移动窗口，双击切换最大化。"""

    def __init__(self, win):
        super().__init__()
        self._win = win
        self._drag = None
        self.setObjectName("titlebar")
        self.setFixedHeight(36)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 4, 0)
        lay.setSpacing(6)

        badge = QLabel("V")
        badge.setFixedSize(20, 20)
        badge.setAlignment(Qt.AlignCenter)
        badge.setStyleSheet(f"background:{ACCENT};color:#fff;border-radius:5px;"
                            "font-weight:bold;font-size:12px")
        lay.addWidget(badge)
        title = QLabel("VTranslate")
        title.setStyleSheet("font-weight:bold;font-size:13px")
        lay.addWidget(title)
        self.dot = QLabel("●")
        self.dot.setStyleSheet(f"color:{MUT};font-size:10px")
        self.dot.setToolTip("灰=待机 绿=翻译中")
        lay.addWidget(self.dot)
        lay.addStretch(1)

        for txt, slot, obj in (("—", win.showMinimized, "min"),
                               ("✕", win.close, "close")):
            b = QPushButton(txt)
            b.setObjectName(f"tb_{obj}")
            b.setFixedSize(36, 24)
            b.clicked.connect(slot)
            lay.addWidget(b)

    def set_active(self, on: bool):
        self.dot.setStyleSheet(
            f"color:{OK if on else MUT};font-size:10px")

    # 拖动
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self._win.pos()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self._win.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _):
        self._drag = None

    def mouseDoubleClickEvent(self, _):
        if self._win.isMaximized():
            self._win.showNormal()
        else:
            # 无边框窗最大化会盖住任务栏，手动限制在可用区域内
            self._win.setGeometry(
                QApplication.primaryScreen().availableGeometry())


# ================= 主窗 =================
class MainWindow(QMainWindow):
    start_stop = Signal(bool)
    show_overlay = Signal(bool)
    apply_overlay = Signal()
    open_settings = Signal()
    logout = Signal()
    test_self = Signal(bool)

    # (图标, 名称, key)
    NAV = [
        ("🎧", "翻译", "home"), ("🪟", "字幕", "subtitle"),
        ("🎹", "测试", "test"), ("📜", "记录", "history"),
        ("📣", "推广", "ref"), ("🛒", "购买", "buy"),
        ("📖", "教程", "tutorial"), ("⚙", "设置", "settings"),
        ("💬", "联系", "contact"),
    ]

    def __init__(self, cfg, api, history):
        super().__init__()
        self.cfg = cfg
        self.api = api
        self._tray_ref = None     # main.py 注入，用于气泡提示
        self._direct = cfg["mode"] == "direct"
        self.setWindowTitle("VTranslate")
        self.resize(700, 440)
        self.setMinimumSize(580, 380)
        self.setWindowState(Qt.WindowNoState)   # 防止被记住为最大化/全屏
        # 无边框 + 圆角
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setAttribute(Qt.WA_TranslucentBackground)

        central = QWidget()
        central.setObjectName("rootwin")
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.titlebar = _TitleBar(self)
        outer.addWidget(self.titlebar)

        content = QWidget()
        root = QHBoxLayout(content)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- 侧边栏 ----
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(78)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(6, 6, 6, 6)
        sl.setSpacing(2)

        self._nav_btns: list[QPushButton] = []
        self._nav_keys: list[str] = []
        self.pages = QStackedWidget()
        for icon, name, key in self.NAV:
            # 直连模式隐藏依赖服务器的页面
            if self._direct and key in ("ref", "buy"):
                continue
            b = QPushButton(f"{icon}\n{name}")
            b.setObjectName("nav")
            b.setCheckable(True)
            b.clicked.connect(lambda _, k=key: self.switch_to(k))
            sl.addWidget(b)
            self._nav_btns.append(b)
            self._nav_keys.append(key)
        sl.addStretch(1)

        self.side_balance = QLabel("¥0.00")
        self.side_balance.setStyleSheet(f"font-weight:bold;color:{TEXT};"
                                        "padding:2px;font-size:11px")
        self.side_balance.setAlignment(Qt.AlignCenter)
        self.side_user = QLabel("")
        self.side_user.setStyleSheet(f"color:{MUT};padding:0 2px;font-size:11px")
        self.side_user.setAlignment(Qt.AlignCenter)
        self.side_user.setWordWrap(True)
        out = QPushButton("退出")
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
        self.test_page = TestPage()
        self.history_page = HistoryPage(history)
        self.tutorial = TutorialPage()
        self.contact = ContactPage()
        self.settings_holder = _TextPage(
            "设置", "点击下方按钮打开设置面板。")
        open_btn = QPushButton("打开设置面板")
        open_btn.setStyleSheet(f"background:{ACCENT};color:#fff;border:0;"
                               "border-radius:6px;padding:8px;max-width:200px")
        open_btn.clicked.connect(self.open_settings.emit)
        self.settings_holder.layout().itemAt(0).widget().layout()\
            .addWidget(open_btn)

        self._page_map = {
            "home": self.home, "subtitle": self.subtitle,
            "test": self.test_page, "history": self.history_page,
            "tutorial": self.tutorial,
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
        outer.addWidget(content, 1)

        # 右下角尺寸抓手
        grip_row = QHBoxLayout()
        grip_row.setContentsMargins(0, 0, 4, 2)
        grip_row.addStretch(1)
        grip_row.addWidget(QSizeGrip(self))
        outer.addLayout(grip_row)
        self.setCentralWidget(central)

        # 信号
        self.home.start_stop.connect(self.start_stop)
        self.home.goto_purchase.connect(self._goto_purchase)
        self.subtitle.show_overlay.connect(self.show_overlay)
        self.subtitle.apply_overlay.connect(self.apply_overlay)
        self.test_page.test_self.connect(self.test_self)
        if not self._direct:
            self.buy_page.paid.connect(self._on_paid)

        self.setStyleSheet(f"""
            #rootwin {{ background:{BG}; border-radius:12px;
                border:1px solid {BORDER}; }}
            #titlebar {{ background:{PANEL}; border-top-left-radius:12px;
                border-top-right-radius:12px;
                border-bottom:1px solid {BORDER}; }}
            #tb_min, #tb_close {{ background:transparent; border:0;
                color:{MUT}; font-size:14px; border-radius:5px; }}
            #tb_min:hover {{ background:#333947; color:{TEXT}; }}
            #tb_close:hover {{ background:{DANGER}; color:#fff; }}
            #sidebar {{ background:{PANEL}; border-right:1px solid {BORDER};
                border-bottom-left-radius:12px; }}
            #nav {{ text-align:center; padding:9px 2px; border:0;
                border-radius:8px; color:{MUT}; font-size:12px; }}
            #nav:hover {{ background:#262b36; color:{TEXT}; }}
            #nav:checked {{ background:#3a2028; color:{ACCENT}; }}
            #banner {{ background:#3a3220; color:#e8c46a; padding:8px 14px;
                border-bottom:1px solid #554726; }}
        """)
        self.switch(0)

    def set_conn(self, running: bool):
        self.titlebar.set_active(running)

    # 关闭主窗 = 最小化到托盘（程序在后台继续跑，右键托盘图标退出）
    def closeEvent(self, e):
        e.ignore()
        self.hide()
        try:
            tray = self._tray_ref() if callable(self._tray_ref) else None
        except Exception:
            tray = None
        if tray:
            tray.showMessage("VTranslate", "已最小化到托盘，右键托盘图标可退出",
                             tray.icon(), 2500)

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
