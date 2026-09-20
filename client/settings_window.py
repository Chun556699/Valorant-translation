"""设置窗口：服务器/热键/目标语言/悬浮窗外观/音频设备。"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QFormLayout, QHBoxLayout,
                               QLineEdit, QPushButton, QComboBox, QSlider,
                               QCheckBox, QLabel, QSpinBox)

import audio
from hotkeys import HotkeyRecorder

_LANGS = [("zh", "中文"), ("en", "English"), ("ja", "日本語"), ("ko", "한국어"),
          ("ru", "Русский"), ("fr", "Français"), ("de", "Deutsch"),
          ("es", "Español"), ("pt", "Português")]


class SettingsDialog(QDialog):
    saved = Signal()
    hotkey_recording = Signal(str)   # 提示文案
    _captured = Signal(dict)         # 录入线程 -> UI 线程

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("VTranslate 设置")
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.setMinimumWidth(420)
        self._recorder = None
        self._captured.connect(self._hotkey_captured)

        lay = QVBoxLayout(self)
        form = QFormLayout()
        lay.addLayout(form)

        self.server_edit = QLineEdit(cfg["server_url"])
        form.addRow("服务器地址", self.server_edit)

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
        form.addRow("", self.play_audio)
        self.auto_copy = QCheckBox("译文定稿后自动复制到剪贴板")
        self.auto_copy.setChecked(bool(cfg["auto_copy"]))
        form.addRow("", self.auto_copy)

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
        save = QPushButton("保存")
        save.clicked.connect(self._save)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(save)
        btns.addWidget(cancel)
        lay.addLayout(btns)

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
        self.hotkey_recording.emit("changed")

    def _save(self):
        c = self.cfg
        c["server_url"] = self.server_edit.text().strip().rstrip("/")
        c["target_mate"] = self.mate_target.currentData()
        c["target_self"] = self.self_target.currentData()
        c["mic_device"] = self.mic_combo.currentData()
        c["loopback_device"] = self.lb_combo.currentData()
        c["output_device"] = self.out_combo.currentData()
        c["play_audio"] = self.play_audio.isChecked()
        c["auto_copy"] = self.auto_copy.isChecked()
        c["overlay_opacity"] = self.opacity.value() / 100.0
        c["max_bubbles"] = self.max_lines.value()
        c["click_through"] = self.through.isChecked()
        c.save()
        self.saved.emit()
        self.accept()

    def reject(self):
        if self._recorder:
            self._recorder.stop()
        super().reject()
