"""悬浮翻译窗：无边框/置顶/半透明，队友与自己语音的原文+译文双语气泡。"""
from PySide6.QtCore import Qt, Signal, QPoint
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QScrollArea, QFrame)

_SPEAKER_COLORS = {
    "我": "#7ecfff", "队友": "#ffd479",
    "好友1": "#9dff9d", "好友2": "#ff9dc4", "好友3": "#c9a7ff",
    "好友4": "#ffb27e", "好友5": "#8ef0e4",
}


def _color_for(name: str) -> str:
    return _SPEAKER_COLORS.get(name, "#ffd479")


class Bubble(QFrame):
    """一条发言：说话人 + 原文(灰) + 译文(亮)"""

    def __init__(self, speaker: str, parent=None):
        super().__init__(parent)
        self.setObjectName("bubble")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(1)
        self.speaker_lbl = QLabel(speaker)
        self.speaker_lbl.setStyleSheet(
            f"color:{_color_for(speaker)}; font-weight:bold; font-size:12px;")
        self.src_lbl = QLabel("")
        self.src_lbl.setWordWrap(True)
        self.src_lbl.setStyleSheet("color:#bbbbbb; font-size:12px;")
        self.dst_lbl = QLabel("")
        self.dst_lbl.setWordWrap(True)
        self.dst_lbl.setStyleSheet("color:#ffffff; font-size:14px; font-weight:600;")
        lay.addWidget(self.speaker_lbl)
        lay.addWidget(self.src_lbl)
        lay.addWidget(self.dst_lbl)

    def set_text(self, src: str, dst: str):
        self.src_lbl.setText(src)
        self.dst_lbl.setText(dst)
        self.src_lbl.setVisible(bool(src))
        self.dst_lbl.setVisible(bool(dst))


class Overlay(QWidget):
    """主悬浮窗。seg 消息经 Qt 信号进来，UI 线程内更新。"""

    open_settings = Signal()
    open_recharge = Signal()
    toggle_mate = Signal(bool)

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.resize(380, 260)
        self._drag_pos: QPoint | None = None
        self._click_through = bool(cfg["click_through"])

        # 状态: (stream, raw_speaker) -> {"bubble": Bubble, "src": "", "dst": "", "open": True}
        self._segs: dict = {}
        self._bubbles: list[Bubble] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- 顶栏 ----
        bar = QFrame()
        bar.setObjectName("topbar")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(8, 2, 4, 2)
        bl.setSpacing(4)
        self.status_lbl = QLabel("初始化…")
        self.status_lbl.setStyleSheet("color:#9fd0ff; font-size:11px;")
        self.mate_btn = QPushButton()
        self.mate_btn.setCheckable(True)
        self.mate_btn.setChecked(bool(cfg["mate_enabled"]))
        self.mate_btn.setText("队友✓" if cfg["mate_enabled"] else "队友✗")
        self.mate_btn.toggled.connect(self._on_mate_toggle)
        self.pin_btn = QPushButton("穿透")
        self.pin_btn.setCheckable(True)
        self.pin_btn.setChecked(self._click_through)
        self.pin_btn.toggled.connect(self._on_pin_toggle)
        buttons = [("设置", self.open_settings.emit)]
        if cfg["mode"] != "direct":   # 直连模式没有充值概念
            buttons.insert(0, ("充值", self.open_recharge.emit))
        for text, fn in buttons:
            b = QPushButton(text)
            b.clicked.connect(fn)
            bl.addWidget(b)
        close = QPushButton("✕")
        close.clicked.connect(self.hide)
        for w in (self.mate_btn, self.pin_btn, close):
            w.setStyleSheet("QPushButton{color:#ddd;background:#3a3a3a;border:0;"
                            "padding:2px 8px;font-size:11px;border-radius:3px}"
                            "QPushButton:checked{background:#2266aa;color:#fff}")
        bl.insertWidget(0, self.status_lbl)
        bl.addWidget(self.mate_btn)
        bl.addWidget(self.pin_btn)
        bl.addWidget(close)
        root.addWidget(bar)

        # ---- 消息区 ----
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea{border:0;background:transparent}")
        self._container = QWidget()
        self._container.setStyleSheet("background:transparent")
        self._list = QVBoxLayout(self._container)
        self._list.setContentsMargins(4, 4, 4, 4)
        self._list.setSpacing(6)
        self._list.addStretch(1)
        self.scroll.setWidget(self._container)
        root.addWidget(self.scroll, 1)

        self.setStyleSheet("""
            Overlay { background: transparent; }
            #topbar { background: rgba(20,22,28,220); border-top-left-radius:8px;
                      border-top-right-radius:8px; }
            #bubble { background: rgba(20,22,28,190); border-radius:6px; }
            QScrollBar:vertical { width:0px; }
        """)
        self._apply_opacity()

    # ---- 外观 ----
    def _apply_opacity(self):
        self.setWindowOpacity(float(self.cfg["overlay_opacity"]))

    def _on_pin_toggle(self, on: bool):
        self._click_through = on
        self.cfg["click_through"] = on
        self.cfg.save()
        self.setAttribute(Qt.WA_TransparentForMouseEvents, on)
        # 切穿透需重显才生效
        self.hide()
        self.show()

    def _on_mate_toggle(self, on: bool):
        self.mate_btn.setText("队友✓" if on else "队友✗")
        self.cfg["mate_enabled"] = on
        self.cfg.save()
        self.toggle_mate.emit(on)

    # ---- 拖动 ----
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, e):
        if self._drag_pos is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None

    # ---- 状态栏 ----
    def set_status(self, text: str):
        self.status_lbl.setText(text)

    # ---- 消息 ----
    def add_seg(self, stream: str, raw_speaker: str | None, name: str, ev: dict):
        key = (stream, raw_speaker or name)
        seg = self._segs.get(key)
        if ev.get("turn_done"):
            if seg:
                seg["open"] = False
            return
        if seg is None or not seg["open"]:
            bubble = Bubble(name)
            self._list.insertWidget(self._list.count() - 1, bubble)
            self._bubbles.append(bubble)
            seg = {"bubble": bubble, "src": "", "dst": "", "open": True}
            self._segs[key] = seg
            self._trim()

        if "src_delta" in ev:
            seg["src"] += ev["src_delta"]
        elif "src" in ev:
            seg["src"] = ev["src"] or seg["src"]
        if "dst_delta" in ev:
            seg["dst"] += ev["dst_delta"]
        elif "dst" in ev:
            seg["dst"] = ev["dst"] or seg["dst"]
        seg["bubble"].set_text(seg["src"], seg["dst"])

        if ev.get("src_final"):
            seg["src_done"] = True
        if ev.get("dst_final"):
            seg["dst_done"] = True
        if seg.get("src_done") and seg.get("dst_done"):
            seg["open"] = False
        self._autoscroll()

    def _trim(self):
        limit = int(self.cfg["max_bubbles"])
        while len(self._bubbles) > limit:
            old = self._bubbles.pop(0)
            old.setParent(None)
            old.deleteLater()

    def _autoscroll(self):
        sb = self.scroll.verticalScrollBar()
        sb.setValue(sb.maximum())
