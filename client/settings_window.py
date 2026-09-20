"""设置窗口：服务器/热键/目标语言/悬浮窗外观/音频设备。"""
import threading

from PySide6.QtCore import Qt, Signal, QObject, QEvent
from PySide6.QtWidgets import (QDialog, QWidget, QVBoxLayout, QFormLayout,
                               QHBoxLayout, QLineEdit, QPushButton, QComboBox,
                               QSlider, QCheckBox, QLabel, QSpinBox)


class _NoWheel(QObject):
    """吞掉滚轮事件：滚动设置页时不会误改 ComboBox/SpinBox/Slider 的值。"""

    def eventFilter(self, obj, ev):
        return ev.type() == QEvent.Wheel

import audio
from hotkeys import HotkeyRecorder

_LANGS = [("zh", "中文"), ("en", "English"), ("ja", "日本語"), ("ko", "한국어"),
          ("ru", "Русский"), ("fr", "Français"), ("de", "Deutsch"),
          ("es", "Español"), ("pt", "Português")]


class SettingsDialog(QDialog):
    saved = Signal()
    hotkey_recording = Signal(str)   # 提示文案
    _captured = Signal(dict)         # 录入线程 -> UI 线程
    _test_result = Signal(str, bool) # 测试线程 -> UI 线程
    test_audio = Signal()            # 试听播报按钮

    def __init__(self, cfg, parent=None, embedded=False):
        super().__init__(parent)
        self.cfg = cfg
        self._embedded = embedded
        if embedded:
            # 嵌入主窗作为页面：去掉对话框属性/置顶/取消按钮
            self.setWindowFlags(Qt.Widget)
        else:
            self.setWindowTitle("VTranslate 设置")
            self.setWindowFlags(
                self.windowFlags() | Qt.WindowStaysOnTopHint)
            self.setMinimumWidth(420)
        self._recorder = None
        self._captured.connect(self._hotkey_captured)
        self._test_result.connect(self._on_test_result)

        lay = QVBoxLayout(self)
        form = QFormLayout()
        self._form = form
        lay.addLayout(form)

        # ---- 运行模式 ----
        self.mode = QComboBox()
        self.mode.addItem("直连 DashScope（个人测试，填自己的 Key）", "direct")
        self.mode.addItem("计费服务器（发行版）", "server")
        self.mode.setCurrentIndex(0 if cfg["mode"] == "direct" else 1)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        form.addRow("运行模式", self.mode)

        # ---- 直连参数 ----
        self.dash_key = QLineEdit(cfg["dash_key"])
        self.dash_key.setEchoMode(QLineEdit.Password)
        self.dash_key.setPlaceholderText("sk-...（百炼/DashScope 控制台获取）")
        self.dash_url = QLineEdit(cfg["dash_ws_url"])
        self.dash_model = QLineEdit(cfg["dash_model"])
        self._direct_rows = [
            ("API Key", self.dash_key),
            ("模型地址", self.dash_url),
            ("模型名", self.dash_model),
        ]
        for label, w in self._direct_rows:
            form.addRow(label, w)

        self.server_edit = QLineEdit(cfg["server_url"])
        self._server_row = self.server_edit
        form.addRow("服务器地址", self.server_edit)

        # ---- 连接测试 ----
        self.test_btn = QPushButton("测试连接")
        self.test_btn.clicked.connect(self._test_conn)
        self.test_lbl = QLabel("")
        self.test_lbl.setWordWrap(True)
        trow = QHBoxLayout()
        trow.addWidget(self.test_btn)
        trow.addWidget(self.test_lbl, 1)
        form.addRow("连通性", trow)
        self._mode_changed(self.mode.currentIndex())

        # ---- 热键 ----
        hk_row = QHBoxLayout()
        self.hk_lbl = QLabel(self._hk_text(cfg["hotkey"]))
        rec = QPushButton("点击录入")
        rec.clicked.connect(self._record_hotkey)
        hk_row.addWidget(self.hk_lbl, 1)
        hk_row.addWidget(rec)
        form.addRow("自翻热键(按住说话)", hk_row)

        # ---- 目标语言 ----
        self.mate_target = QComboBox()
        self.self_target = QComboBox()
        for code, name in _LANGS:
            self.mate_target.addItem(name, code)
            self.self_target.addItem(name, code)
        self.mate_target.setCurrentIndex(
            max(0, [c for c, _ in _LANGS].index(cfg["target_mate"]))
            if cfg["target_mate"] in [c for c, _ in _LANGS] else 0)
        self.self_target.setCurrentIndex(
            max(0, [c for c, _ in _LANGS].index(cfg["target_self"]))
            if cfg["target_self"] in [c for c, _ in _LANGS] else 1)
        form.addRow("队友语音翻译成", self.mate_target)
        form.addRow("我的语音翻译成", self.self_target)

        # ---- 音频设备 ----
        import pyaudiowpatch as pyaudio
        pa = pyaudio.PyAudio()
        self.mic_combo = QComboBox()
        self.mic_combo.addItem("默认麦克风", None)
        for idx, name in audio.list_input_devices(pa):
            self.mic_combo.addItem(name, idx)
        self.lb_combo = QComboBox()
        self.lb_combo.addItem("默认扬声器回环", None)
        for idx, name in audio.list_loopback_devices(pa):
            self.lb_combo.addItem(name, idx)
        self.out_combo = QComboBox()
        self.out_combo.addItem("默认扬声器", None)
        for idx, name in audio.list_output_devices(pa):
            self.out_combo.addItem(name, idx)
        pa.terminate()
        self._select_device(self.mic_combo, cfg["mic_device"])
        self._select_device(self.lb_combo, cfg["loopback_device"])
        self._select_device(self.out_combo, cfg["output_device"])
        form.addRow("麦克风", self.mic_combo)
        form.addRow("回环设备", self.lb_combo)
        form.addRow("播报输出设备", self.out_combo)

        # ---- 播报与复制 ----
        self.play_audio = QCheckBox("播放译文声音（TTS 播报）")
        self.play_audio.setChecked(bool(cfg["play_audio"]))
        prow = QHBoxLayout()
        prow.addWidget(self.play_audio)
        audition = QPushButton("试听播报")
        audition.clicked.connect(self.test_audio.emit)
        prow.addWidget(audition)
        prow.addStretch(1)
        form.addRow("", prow)
        self.auto_copy = QCheckBox("译文定稿后自动复制到剪贴板")
        self.auto_copy.setChecked(bool(cfg["auto_copy"]))
        form.addRow("", self.auto_copy)

        self.mate_filter = QCheckBox("队友流语音频段过滤（抑制枪声/脚步声噪声）")
        self.mate_filter.setChecked(bool(cfg["mate_filter"]))
        form.addRow("", self.mate_filter)

        # ---- 悬浮窗 ----
        op_row = QHBoxLayout()
        self.opacity = QSlider(Qt.Horizontal)
        self.opacity.setRange(30, 100)
        self.opacity.setValue(int(float(cfg["overlay_opacity"]) * 100))
        self.op_val = QLabel(f"{self.opacity.value()}%")
        self.opacity.valueChanged.connect(
            lambda v: self.op_val.setText(f"{v}%"))
        op_row.addWidget(self.opacity, 1)
        op_row.addWidget(self.op_val)
        form.addRow("悬浮窗不透明度", op_row)

        self.max_lines = QSpinBox()
        self.max_lines.setRange(3, 30)
        self.max_lines.setValue(int(cfg["max_bubbles"]))
        form.addRow("最多保留条数", self.max_lines)

        self.through = QCheckBox("鼠标穿透悬浮窗（游戏中点不到字幕）")
        self.through.setChecked(bool(cfg["click_through"]))
        form.addRow("", self.through)

        # ---- 保存 ----
        btns = QHBoxLayout()
        if not embedded:
            save = QPushButton("保存")
            save.clicked.connect(self._save)
            cancel = QPushButton("取消")
            cancel.clicked.connect(self.reject)
            btns.addStretch(1)
            btns.addWidget(save)
            btns.addWidget(cancel)
        else:
            hint = QLabel("✓ 改动即时生效，无需重启")
            hint.setStyleSheet("color:#7a8494")
            btns.addStretch(1)
            btns.addWidget(hint)
        lay.addLayout(btns)

        # 滚轮误触保护：滚动页面时 ComboBox/SpinBox/Slider 不变值
        self._no_wheel = _NoWheel(self)
        for w in self.findChildren(QWidget):
            if isinstance(w, (QComboBox, QSpinBox, QSlider)):
                w.installEventFilter(self._no_wheel)

        if embedded:
            self._wire_live_apply()

    def _wire_live_apply(self):
        """嵌入模式：任何控件变更立即保存并生效（无需点保存/重启）。"""
        for w in (self.mode, self.mate_target, self.self_target,
                  self.mic_combo, self.lb_combo, self.out_combo):
            w.currentIndexChanged.connect(lambda *_: self._apply())
        for w in (self.dash_key, self.dash_url, self.dash_model,
                  self.server_edit):
            w.editingFinished.connect(self._apply)
        for w in (self.play_audio, self.auto_copy, self.mate_filter,
                  self.through):
            w.toggled.connect(lambda _: self._apply())
        self.opacity.sliderReleased.connect(self._apply)
        self.max_lines.valueChanged.connect(lambda _: self._apply())

    @staticmethod
    def _select_device(combo, value):
        for i in range(combo.count()):
            if combo.itemData(i) == value:
                combo.setCurrentIndex(i)
                return

    @staticmethod
    def _hk_text(hk):
        dev = "鼠标" if hk.get("device") == "mouse" else "键盘"
        return f"{dev} · {hk.get('key', '')}"

    def _record_hotkey(self):
        self.hk_lbl.setText("请按下按键或鼠标键…")
        self._recorder = HotkeyRecorder(self._captured.emit)
        self._recorder.start()

    def _hotkey_captured(self, spec):
        self.cfg["hotkey"] = spec
        self.hk_lbl.setText(self._hk_text(spec))
        if self._embedded:
            self._apply()
        self.hotkey_recording.emit("changed")

    def _test_conn(self):
        """后台线程测试连通性，测的是输入框当前的值（不要求先保存）。"""
        self.test_btn.setEnabled(False)
        self.test_lbl.setStyleSheet("color:#888")
        self.test_lbl.setText("测试中…")
        args = (self.mode.currentData(),
                self.dash_key.text().strip(),
                self.dash_url.text().strip().rstrip("/"),
                self.dash_model.text().strip(),
                self.server_edit.text().strip().rstrip("/"))
        threading.Thread(target=self._do_test, args=args, daemon=True).start()

    def _do_test(self, mode, key, url, model, server):
        try:
            if mode == "direct":
                if not key:
                    raise ValueError("API Key 为空")
                import asyncio, json
                import websockets

                async def probe():
                    full = f"{url}?model={model}"
                    async with websockets.connect(
                            full,
                            additional_headers={
                                "Authorization": f"Bearer {key}"},
                            open_timeout=8) as ws:
                        await ws.send(json.dumps({
                            "type": "session.update",
                            "session": {
                                "output_modalities": ["text"],
                                "audio": {"input": {"format": {
                                    "type": "pcm", "sample_rate": 16000}}},
                            }}))
                        async for raw in ws:
                            ev = json.loads(raw)
                            t = ev.get("type")
                            if t in ("session.created", "session.updated"):
                                return True, f"连接成功（{t}）"
                            if t == "error" or ev.get("code"):
                                msg = (ev.get("error") or {}).get("message") \
                                    or str(ev)[:200]
                                return False, f"服务端拒绝: {msg}"
                        return False, "连接后无响应"
                ok, msg = asyncio.run(asyncio.wait_for(probe(), 15))
            else:
                import httpx
                r = httpx.get(f"{server}/api/meta", timeout=8)
                if r.status_code == 200:
                    ok, msg = True, f"服务器可达（/api/meta 200）"
                else:
                    ok, msg = False, f"HTTP {r.status_code}: {r.text[:150]}"
        except Exception as e:
            ok, msg = False, f"失败: {e}"
        # 注意：此处仍在后台线程，用信号回 UI
        self._test_result.emit(msg, ok)

    def _on_test_result(self, msg, ok):
        self.test_btn.setEnabled(True)
        self.test_lbl.setStyleSheet(f"color:{'#2a7' if ok else '#d33'}")
        self.test_lbl.setText(("✓ " if ok else "✗ ") + msg)

    def _mode_changed(self, idx):
        direct = self.mode.currentData() == "direct"
        form = self._form
        hide = {w for _, w in getattr(self, "_direct_rows", [])}
        hide.add(self.server_edit)
        for i in range(form.rowCount()):
            field = form.itemAt(i, QFormLayout.FieldRole)
            label = form.itemAt(i, QFormLayout.LabelRole)
            if not field or not field.widget():
                continue
            if field.widget() in hide:
                vis = direct if field.widget() is not self.server_edit \
                    else not direct
                field.widget().setVisible(vis)
                if label and label.widget():
                    label.widget().setVisible(vis)

    def _apply(self):
        """把控件值写入 cfg 并落盘，emit saved 让主程序即时应用。"""
        c = self.cfg
        c["mode"] = self.mode.currentData()
        c["dash_key"] = self.dash_key.text().strip()
        c["dash_ws_url"] = self.dash_url.text().strip().rstrip("/")
        c["dash_model"] = self.dash_model.text().strip()
        c["server_url"] = self.server_edit.text().strip().rstrip("/")
        c["target_mate"] = self.mate_target.currentData()
        c["target_self"] = self.self_target.currentData()
        c["mic_device"] = self.mic_combo.currentData()
        c["loopback_device"] = self.lb_combo.currentData()
        c["output_device"] = self.out_combo.currentData()
        c["play_audio"] = self.play_audio.isChecked()
        c["auto_copy"] = self.auto_copy.isChecked()
        c["mate_filter"] = self.mate_filter.isChecked()
        c["overlay_opacity"] = self.opacity.value() / 100.0
        c["max_bubbles"] = self.max_lines.value()
        c["click_through"] = self.through.isChecked()
        c.save()
        self.saved.emit()

    def _save(self):
        """模态对话框的「保存」按钮路径。"""
        self._apply()
        self.accept()

    def reject(self):
        if self._recorder:
            self._recorder.stop()
        super().reject()
