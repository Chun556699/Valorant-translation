"""VTranslate 客户端入口。

架构：
  队友语音 = 系统扬声器回环 -> 16k PCM -> 服务器中转 WS -> 悬浮窗
  自己语音 = 麦克风（按住热键才发送） -> 服务器中转 WS -> 悬浮窗
  全程只读音频设备，不触碰游戏进程/数据。
"""
import logging
import queue
import sys
import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PySide6.QtGui import QAction

import audio
from config import Config
from hotkeys import PTTListener
from net import Api, RelayClient
from overlay import Overlay
from recharge import RechargeDialog
from settings_window import SettingsDialog
from speakers import SpeakerNamer

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("main")


class Bridge(QObject):
    """后台线程 -> Qt UI 的消息桥。"""
    seg = Signal(str, object, str, dict)     # stream, raw_speaker, name, ev
    status = Signal(str)


class App:
    def __init__(self):
        self.cfg = Config()
        self.api = Api(self.cfg["server_url"])
        self.namer = SpeakerNamer()
        self.bridge = Bridge()
        self.ptt_on = threading.Event()
        self._mate_on = bool(self.cfg["mate_enabled"])

        self.mate_q: queue.Queue = queue.Queue(maxsize=200)
        self.mic_q: queue.Queue = queue.Queue(maxsize=200)
        self.loopback = audio.LoopbackCapture(self.mate_q)
        self.mic = audio.MicCapture(self.mic_q)

        self.mate_rc: RelayClient | None = None
        self.self_rc: RelayClient | None = None
        self.ptt: PTTListener | None = None
        self._state = {"mate": "off", "self": "off", "credits": None, "sub": False}

    # ---------- 初始化 ----------
    def init(self, app: QApplication):
        self.overlay = Overlay(self.cfg)
        self.bridge.seg.connect(self.overlay.add_seg)
        self.bridge.status.connect(self.overlay.set_status)
        self.overlay.open_settings.connect(self.show_settings)
        self.overlay.open_recharge.connect(self.show_recharge)
        self.overlay.toggle_mate.connect(self.set_mate)

        self._register()
        self._start_audio_threads()
        self._start_relays()
        self._start_hotkey()
        self._make_tray(app)
        self.overlay.show()
        self._update_status()

    def _register(self):
        try:
            resp = self.api.register(self.cfg["device_id"])
            if resp.get("code") == 1:
                self.api.token = resp["token"]
                self.cfg["token"] = resp["token"]
                self.cfg.save()
                self._state["credits"] = resp.get("credit_seconds", 0)
                self._state["sub"] = resp.get("sub_active", False)
                log.info("注册成功 余额=%ss sub=%s",
                         resp.get("credit_seconds"), resp.get("sub_active"))
            else:
                log.warning("注册失败: %s", resp)
        except Exception as e:
            log.warning("无法连接服务器 %s: %s", self.cfg["server_url"], e)
        if self.cfg["token"]:
            self.api.token = self.cfg["token"]

    # ---------- 音频线程 ----------
    def _start_audio_threads(self):
        threading.Thread(target=self._mate_feeder, daemon=True,
                         name="mate-feeder").start()
        threading.Thread(target=self._mic_feeder, daemon=True,
                         name="mic-feeder").start()

    def _mate_feeder(self):
        """回环 -> mate 中转。"""
        started = False
        while True:
            try:
                if self._mate_on and not started:
                    self.loopback.start(self.cfg["loopback_device"])
                    started = True
                elif not self._mate_on and started:
                    self.loopback.stop()
                    started = False
                if not started:
                    import time
                    time.sleep(0.3)
                    continue
                chunk = audio.drain_queue(self.mate_q, timeout=0.5)
                if self.mate_rc:
                    self.mate_rc.send_audio(chunk)
            except Exception as e:
                log.warning("回环采集异常: %s", e)
                started = False
                import time
                time.sleep(1)

    def _mic_feeder(self):
        """麦克风 -> self 中转（仅按住热键时发送）。"""
        try:
            self.mic.start(self.cfg["mic_device"])
        except Exception as e:
            log.warning("麦克风启动失败: %s", e)
            return
        while True:
            try:
                chunk = audio.drain_queue(self.mic_q, timeout=0.5)
            except queue.Empty:
                continue
            if self.ptt_on.is_set() and self.self_rc:
                self.self_rc.send_audio(chunk)

    # ---------- 中转连接 ----------
    def _start_relays(self):
        self.mate_rc = RelayClient(
            self.cfg["server_url"], self.cfg["token"], "mate",
            self.cfg["target_mate"],
            on_event=lambda ev: self._on_relay_event("mate", ev),
            on_state=lambda s: self._on_state("mate", s))
        self.self_rc = RelayClient(
            self.cfg["server_url"], self.cfg["token"], "self",
            self.cfg["target_self"],
            on_event=lambda ev: self._on_relay_event("self", ev),
            on_state=lambda s: self._on_state("self", s))
        self.self_rc.start()
        if self._mate_on:
            self.mate_rc.start()

    def _on_state(self, stream, s):
        self._state[stream] = s
        self._update_status()

    def _on_relay_event(self, stream, ev: dict):
        t = ev.get("type")
        if t == "seg":
            name = self.namer.name(ev.get("speaker"), stream)
            self.bridge.seg.emit(stream, ev.get("speaker"), name, ev)
        elif t == "usage" or t == "ready":
            self._state["credits"] = ev.get("credit_seconds", self._state["credits"])
            self._state["sub"] = ev.get("sub", self._state["sub"])
            self._update_status()
        elif t == "error":
            log.warning("[%s] %s: %s", stream, ev.get("code"), ev.get("msg"))
            if ev.get("code") == "insufficient_balance":
                self.bridge.status.emit("点数不足，请充值")
        elif t == "closed":
            self._update_status()

    # ---------- 热键 ----------
    def _start_hotkey(self):
        if self.ptt:
            self.ptt.stop()
        self.ptt = PTTListener(self.cfg["hotkey"],
                               self._ptt_press, self._ptt_release)
        self.ptt.start()

    def _ptt_press(self):
        self.ptt_on.set()
        self.bridge.status.emit("🎙 说话中…（松开完成）")

    def _ptt_release(self):
        self.ptt_on.clear()
        if self.self_rc:
            # 补 0.8s 静音触发服务端断句，再兜底 commit
            silence = b"\x00" * int(16000 * 2 * 0.8)
            self.self_rc.send_audio(silence)
            self.self_rc.commit()
        self._update_status()

    # ---------- UI 动作 ----------
    def set_mate(self, on: bool):
        self._mate_on = on
        if on and self.mate_rc:
            self.mate_rc.start()
        elif not on and self.mate_rc:
            self.mate_rc.stop()
        self._update_status()

    def show_settings(self):
        dlg = SettingsDialog(self.cfg, self.overlay)
        dlg.hotkey_recording.connect(lambda _: self._start_hotkey())
        if dlg.exec():
            self.overlay._apply_opacity()
            self.overlay._on_pin_toggle(self.cfg["click_through"])
            self._start_hotkey()

    def show_recharge(self):
        RechargeDialog(self.api, self._on_paid, self.overlay).exec()

    def _on_paid(self, resp):
        self._state["credits"] = resp.get("credit_seconds", self._state["credits"])
        self._state["sub"] = resp.get("sub_active", self._state["sub"])
        self._update_status()

    def _update_status(self):
        if self._state["sub"]:
            acc = "订阅中"
        elif self._state["credits"] is not None:
            acc = f"余额 {self._state['credits']//60}分{self._state['credits']%60}秒"
        else:
            acc = "未连接服务器"
        mate = "队友✓" if self._mate_on and self._state["mate"] == "ready" else \
               ("队友…" if self._mate_on else "队友关")
        self.bridge.status.emit(f"{acc} | {mate}")

    # ---------- 托盘 ----------
    def _make_tray(self, app):
        tray = QSystemTrayIcon(self.overlay)
        tray.setIcon(self.overlay.style().standardIcon(
            self.overlay.style().SP_MediaPlay))
        tray.setToolTip("VTranslate 语音翻译")
        menu = QMenu()
        show = QAction("显示悬浮窗")
        show.triggered.connect(self.overlay.show)
        settings = QAction("设置")
        settings.triggered.connect(self.show_settings)
        quit_a = QAction("退出")
        quit_a.triggered.connect(app.quit)
        menu.addAction(show)
        menu.addAction(settings)
        menu.addAction(quit_a)
        tray.setContextMenu(menu)
        tray.activated.connect(
            lambda r: self.overlay.show() if r == QSystemTrayIcon.Trigger else None)
        tray.show()
        self.tray = tray


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("VTranslate")
    a = App()
    a.init(app)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
