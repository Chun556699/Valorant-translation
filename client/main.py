"""VTranslate 客户端入口。

架构：
  队友语音 = 系统扬声器回环 -> 16k PCM -> 服务器中转 WS -> 悬浮窗 + TTS播报
  自己语音 = 麦克风（按住热键才发送） -> 服务器中转 WS -> 悬浮窗 + TTS播报
  全程只读音频设备，不触碰游戏进程/数据。

界面：瓦语通式主窗（导航+页面）+ 游戏内悬浮字幕窗。
"""
import logging
import os
import queue
import sys
import threading
import time

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtGui import QAction, QPixmap, QPainter, QColor, QFont, QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QSystemTrayIcon, QMenu,
                               QMessageBox, QStyle)

import audio
import valorant_terms
from config import Config
from direct import DashScopeDirect, DEFAULT_WS_URL, DEFAULT_MODEL
from history import History
from hotkeys import PTTListener
from login import LoginDialog
from main_window import MainWindow
from net import Api, RelayClient
from overlay import Overlay
from player import PCMPlayer
from settings_window import SettingsDialog
from speakers import SpeakerNamer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.FileHandler(
            os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                         "VTranslate", "app.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ])
log = logging.getLogger("main")

DEFAULT_VOICES = ["Cherry", "Tina", "Ethan", "Serena", "Kiki", "Anna"]


class Bridge(QObject):
    """后台线程 -> Qt UI 的消息桥。"""
    seg = Signal(str, object, str, dict)     # stream, raw_speaker, name, ev
    status = Signal(str)
    ent = Signal(dict)                        # 权益刷新
    ptt = Signal(bool)                        # 热键按下/松开（任何状态下都发）
    level = Signal(str, float)                # "mic"/"mate" RMS 电平
    mate_tick = Signal()                      # 队友音频活动脉冲
    stream_state = Signal(str, str)           # stream, state（测试页用）
    copy_text = Signal(str)                   # 写剪贴板（UI线程执行）


class App:
    def __init__(self):
        self.cfg = Config()
        self.api = Api(self.cfg["server_url"])
        self.namer = SpeakerNamer()
        self.history = History()
        self.bridge = Bridge()
        self.ptt_on = threading.Event()
        self.running = bool(self.cfg["running"])
        self._mate_on = bool(self.cfg["mate_enabled"])
        self.ent: dict = {}                    # 最近一次权益快照
        self._hist_acc: dict = {}              # (stream,speaker)->src/dst 累计
        self.meta: dict = {}

        self.mate_q: queue.Queue = queue.Queue(maxsize=200)
        self.mic_q: queue.Queue = queue.Queue(maxsize=200)
        self.loopback = audio.LoopbackCapture(self.mate_q)
        self.mic = audio.MicCapture(self.mic_q)
        self.player = PCMPlayer(self.cfg["output_device"])

        self.mate_rc: RelayClient | None = None
        self.self_rc: RelayClient | None = None
        self.ptt: PTTListener | None = None
        self._state = {"mate": "off", "self": "off"}
        self._direct = self.cfg["mode"] == "direct"
        self._test_self = False               # 测试页单独拉起自翻通道
        self._lvl_t = {"mic": 0.0, "mate": 0.0}   # 电平发射节流
        self._mate_filter = (audio.SpeechBandFilter()
                             if self.cfg["mate_filter"] else None)

    # ================= 初始化 =================
    def init(self, app: QApplication) -> bool:
        self._app = app

        if self._direct:
            # 直连模式：无需登录，但要有 API Key
            if not self.cfg["dash_key"]:
                QMessageBox.information(
                    None, "VTranslate",
                    "请先配置 DashScope API Key\n（设置 → API Key，阿里云百炼控制台申请）")
                if not self.show_settings_modal():
                    return False
                if not self.cfg["dash_key"]:
                    return False
            self.ent = {"username": "本地直连", "money_yuan": "0.00",
                        "money_cents": 0, "money_minutes": 0,
                        "credit_seconds": 0, "trial_seconds": 0,
                        "trial_expire_at": 0, "trial_active": False,
                        "sub_active": True, "sub_expire_at": 0,
                        "price_fen_per_minute": 0}
            self.meta = {"voices": DEFAULT_VOICES, "trial_seconds": 0}
        else:
            if not self._ensure_login():
                return False
            try:
                self.meta = self.api.meta()
            except Exception as e:
                log.warning("meta 获取失败: %s", e)
                self.meta = {}

        # ---- UI ----
        self.overlay = Overlay(self.cfg)
        self.win = MainWindow(self.cfg, self.api, self.history)
        self.bridge.seg.connect(self.overlay.add_seg)
        self.bridge.status.connect(self.overlay.set_status)
        self.bridge.status.connect(self.win.home.set_status_text)
        self.bridge.ent.connect(self.win.update_entitlement)
        self.bridge.ptt.connect(self.overlay.set_ptt_active)
        self.bridge.ptt.connect(self.win.test_page.set_pressed)
        self.bridge.level.connect(self.win.test_page.set_level)
        self.bridge.mate_tick.connect(self.overlay.mate_tick)
        self.bridge.stream_state.connect(self._sync_test_status)
        # QClipboard 属于 UI 线程，连接即排队投递到 UI 线程执行
        self.bridge.copy_text.connect(QApplication.clipboard().setText)
        self.win.test_self.connect(self.set_test_self)
        self.overlay.open_settings.connect(self.show_settings)
        self.overlay.open_recharge.connect(
            lambda: (self.win.show(), self.win._goto_purchase("money")))
        self.overlay.toggle_mate.connect(self.set_mate)
        self.win.start_stop.connect(self.set_running)
        self.win.show_overlay.connect(lambda _: self.overlay.show())
        self.win.apply_overlay.connect(self._apply_overlay_cfg)
        self.win.open_settings.connect(self.show_settings)
        self.win.logout.connect(self._logout)

        if self.meta.get("announcement"):
            self.win.set_announcement(self.meta["announcement"])
        if self.meta.get("voices"):
            self.win.set_voices(self.meta["voices"])
        self.win.home.set_trial_total(self.meta.get("trial_seconds", 300))
        self.win.set_user(self.ent.get("username", ""))
        self.win.home.play_cb.setChecked(bool(self.cfg["play_audio"]))
        if self._direct:
            self.win.side_balance.setText("本地直连")
            self.win.update_entitlement(self.ent)

        self._start_audio_threads()
        self._start_relays()
        self._start_hotkey()
        self._make_tray(app)
        if self.cfg["play_audio"]:
            self.player.start()

        self.win.show()
        self.overlay.show()
        if self.running:
            self.win.home.toggle_btn.setChecked(True)
        self._update_status()
        return True

    def show_settings_modal(self) -> bool:
        """弹设置框（登录前也可用）。返回是否保存。"""
        dlg = SettingsDialog(self.cfg)
        return bool(dlg.exec())

    # ---------- 登录 ----------
    def _ensure_login(self) -> bool:
        # 已有 token 先校验
        if self.cfg["token"]:
            self.api.token = self.cfg["token"]
            try:
                me = self.api.me()
                if me.get("code") == 1:
                    self.ent = me
                    return True
            except Exception as e:
                log.warning("me 校验失败: %s", e)
        # 弹出登录框
        dlg = LoginDialog(self.api, self.cfg)
        if dlg.exec() and dlg.user_info:
            self.ent = dlg.user_info
            return True
        return False

    def _logout(self):
        self.cfg["token"] = ""
        self.cfg["username"] = ""
        self.cfg.save()
        QMessageBox.information(self.win, "已退出", "已退出登录，请重启客户端。")
        self._app.quit()

    # ================= 音频线程 =================
    def _start_audio_threads(self):
        threading.Thread(target=self._mate_feeder, daemon=True,
                         name="mate-feeder").start()
        threading.Thread(target=self._mic_feeder, daemon=True,
                         name="mic-feeder").start()

    @staticmethod
    def _rms(pcm: bytes) -> float:
        import numpy as np
        if not pcm:
            return 0.0
        a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        return float(np.abs(a).mean()) / 32768.0

    def _emit_level(self, which: str, pcm: bytes):
        now = time.time()
        if now - self._lvl_t[which] < 0.08:   # ~12fps
            return
        self._lvl_t[which] = now
        self.bridge.level.emit(which, self._rms(pcm))

    def _mate_feeder(self):
        """回环 -> mate 中转（仅 running 时采集发送）。"""
        started = False
        while True:
            try:
                want = self.running and self._mate_on
                if want and not started:
                    self.loopback.start(self.cfg["loopback_device"])
                    started = True
                elif not want and started:
                    self.loopback.stop()
                    started = False
                if not started:
                    time.sleep(0.3)
                    continue
                chunk = audio.drain_queue(self.mate_q, timeout=0.5)
                self._emit_level("mate", chunk)
                if self._rms(chunk) > 0.02:   # 有声音 → 悬浮窗绿点
                    self.bridge.mate_tick.emit()
                # 语音频段过滤：削掉枪声低频/高频杂音
                if self._mate_filter:
                    chunk = self._mate_filter.process(chunk)
                # TTS 播报回声抑制：播报设备是默认输出时，播报期间给
                # 回环送静音，避免译文被采回去再翻译形成环路
                if (self.cfg["output_device"] is None
                        and self.player.is_active()):
                    chunk = b"\x00" * len(chunk)
                if self.mate_rc:
                    self.mate_rc.send_audio(chunk)
            except Exception as e:
                log.warning("回环采集异常: %s", e)
                started = False
                time.sleep(1)

    def _mic_feeder(self):
        """麦克风 -> self 中转（仅 running + 按住热键时发送）。"""
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
            self._emit_level("mic", chunk)
            if (self.running or self._test_self) and self.ptt_on.is_set() \
                    and self.self_rc:
                self.self_rc.send_audio(chunk)

    # ================= 中转连接 =================
    def _new_relay(self, stream: str):
        target = (self.cfg["target_mate"] if stream == "mate"
                  else self.cfg["target_self"])
        if self._direct:
            return DashScopeDirect(
                self.cfg["dash_key"],
                self.cfg["dash_ws_url"] or DEFAULT_WS_URL,
                self.cfg["dash_model"] or DEFAULT_MODEL,
                target,
                on_event=lambda ev: self._on_relay_event(stream, ev),
                on_state=lambda s: self._on_state(stream, s),
                voice=self.cfg["voice"], want_audio=bool(self.cfg["play_audio"]),
                on_audio=self.player.play, stream=stream,
                phrases=valorant_terms.for_target(target))
        return RelayClient(
            self.cfg["server_url"], self.cfg["token"], stream, target,
            on_event=lambda ev: self._on_relay_event(stream, ev),
            on_state=lambda s: self._on_state(stream, s),
            voice=self.cfg["voice"],
            want_audio=bool(self.cfg["play_audio"]),
            on_audio=self.player.play)

    def _start_relays(self):
        self.mate_rc = self._new_relay("mate")
        self.self_rc = self._new_relay("self")
        if self.running:
            self.self_rc.start()
            if self._mate_on:
                self.mate_rc.start()

    def _on_state(self, stream, s):
        self._state[stream] = s
        self.bridge.stream_state.emit(stream, s)
        self._update_status()

    def _sync_test_status(self, stream, s):
        if stream != "self":
            return
        text = {"ready": "通道就绪，按住热键说话", "connecting": "连接中…",
                "reconnecting": "断线重连中…", "closed": "通道已关闭",
                "off": ""}.get(s, s)
        self.win.test_page.set_status(text)

    def _on_relay_event(self, stream, ev: dict):
        t = ev.get("type")
        if t == "seg":
            name = self.namer.name(ev.get("speaker"), stream)
            self.bridge.seg.emit(stream, ev.get("speaker"), name, ev)
            if stream == "mate":
                self.bridge.mate_tick.emit()
            # 先取累计译文（_record_history 定稿后会清空缓冲）
            key = (stream, ev.get("speaker") or name)
            acc_dst = (self._hist_acc.get(key, {}).get("dst", "")
                       + ev.get("dst_delta", "") or "")
            if ev.get("dst"):
                acc_dst = ev["dst"]
            self._record_history(stream, name, ev)
            # 自翻译文定稿总是复制（按住说话→Ctrl+V 的闭环）；
            # 队友译文由"自动复制"开关控制
            if ev.get("dst_final") and (stream == "self"
                                        or self.cfg["auto_copy"]):
                if acc_dst:
                    self.bridge.copy_text.emit(acc_dst)
        elif t == "turn_done":
            # 一句结束：通知悬浮窗闭合当前气泡（此前该事件被漏掉了，
            # 导致同一人连续发言都合并进一个气泡）
            self.bridge.seg.emit(stream, ev.get("speaker"),
                                 self.namer.name(ev.get("speaker"), stream), ev)
        elif t == "usage" or t == "ready":
            if not self._direct:
                for k in ("credit_seconds", "trial_seconds",
                          "money_cents", "sub"):
                    if k in ev:
                        self.ent[k] = ev[k]
                self._push_ent()
            self._update_status()
        elif t == "error":
            log.warning("[%s] %s: %s", stream, ev.get("code"), ev.get("msg"))
            if ev.get("code") == "insufficient_balance":
                self.bridge.status.emit("额度不足，请到购买中心充值")
        elif t == "closed":
            self._update_status()

    def _record_history(self, stream, name, ev):
        """积累一句完成后写入本地记录。"""
        key = (stream, ev.get("speaker") or name)
        acc = self._hist_acc.setdefault(key, {"src": "", "dst": ""})
        if "src_delta" in ev:
            acc["src"] += ev["src_delta"]
        elif ev.get("src"):
            acc["src"] = ev["src"]
        if "dst_delta" in ev:
            acc["dst"] += ev["dst_delta"]
        elif ev.get("dst"):
            acc["dst"] = ev["dst"]
        if ev.get("dst_final") or ev.get("src_final"):
            if acc["src"] or acc["dst"]:
                self.history.add(name, acc["src"], acc["dst"], stream)
            self._hist_acc[key] = {"src": "", "dst": ""}

    # ================= 开关 =================
    def set_running(self, on: bool):
        self.running = on
        self.cfg["running"] = on
        self.cfg.save()
        if on:
            self.namer.reset()
            self.self_rc.start()
            if self._mate_on:
                self.mate_rc.start()
        else:
            if not self._test_self:
                self.self_rc.stop()
            self.mate_rc.stop()
            self.loopback.stop()
        self._update_status()

    def set_mate(self, on: bool):
        self._mate_on = on
        if on and self.running and self.mate_rc:
            self.mate_rc.start()
        elif not on and self.mate_rc:
            self.mate_rc.stop()
        self._update_status()

    # ================= 热键 =================
    def _start_hotkey(self):
        if self.ptt:
            self.ptt.stop()
        self.ptt = PTTListener(self.cfg["hotkey"],
                               self._ptt_press, self._ptt_release)
        self.ptt.start()

    def _ptt_press(self):
        self.ptt_on.set()
        self.bridge.ptt.emit(True)   # 任何状态下都给 UI 反馈
        if not (self.running or self._test_self):
            self.bridge.status.emit("未开启翻译/测试 — 先点「开始翻译」或测试页")
            return
        if self.self_rc and not self.self_rc.ready.is_set():
            self.bridge.status.emit("自翻通道未就绪，检查 Key/网络")
            return
        if self.self_rc:
            self.self_rc.clear()   # 清掉可能残留未提交的缓冲
        self.bridge.status.emit("说话中…（松开完成）")

    def _ptt_release(self):
        self.ptt_on.clear()
        self.bridge.ptt.emit(False)
        if self.self_rc:
            # Manual 断句：commit 把按住期间的全部音频作为一整句提交
            self.self_rc.commit()
        self._update_status()

    # ================= 测试页通道 =================
    def set_test_self(self, on: bool):
        """测试页拉起/断开自翻通道（不需要「开始翻译」也生效）。"""
        self._test_self = on
        if on:
            self.self_rc.start()
        elif not self.running:
            self.self_rc.stop()

    # ================= UI =================
    def _push_ent(self):
        e = dict(self.ent)
        price = e.get("price_fen_per_minute") or 30
        e.setdefault("money_yuan", f"{e.get('money_cents',0)/100:.2f}")
        e.setdefault("money_minutes", int(e.get("money_cents", 0) / price)
                     if price else 0)
        e.setdefault("sub_active", e.get("sub", False))
        e.setdefault("trial_expire_at", self.ent.get("trial_expire_at", 0))
        self.bridge.ent.emit(e)

    def _apply_overlay_cfg(self):
        self.overlay._apply_opacity()
        self.overlay._on_pin_toggle(self.cfg["click_through"])

    def show_settings(self):
        dlg = SettingsDialog(self.cfg, self.win)
        dlg.hotkey_recording.connect(lambda _: self._start_hotkey())
        if dlg.exec():
            self._apply_overlay_cfg()
            self._start_hotkey()
            self.win.home.refresh_chips()
            # 模式/Key/音色变化需重建会话；模式切换提示重启更稳妥
            if self.cfg["mode"] == "direct" != self._direct:
                QMessageBox.information(
                    self.win, "模式已切换", "运行模式已更改，请重启客户端生效。")
            elif self.running:
                self.set_running(False)
                self.set_running(True)

    def _update_status(self):
        mate = "队友✓" if self._mate_on and self._state["mate"] == "ready" else \
               ("队友…" if self._mate_on else "队友关")
        state = "翻译中" if self.running else "就绪"
        self.bridge.status.emit(f"{state} | {mate}")
        try:
            self.win.set_conn(self.running)
        except AttributeError:
            pass

    # ================= 托盘 =================
    @staticmethod
    def _tray_icon() -> QIcon:
        """画一个 'V' 徽标图标（不依赖外部资源文件）。"""
        pm = QPixmap(64, 64)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor("#4f8cff"))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(4, 4, 56, 56, 14, 14)
        p.setPen(QColor("#ffffff"))
        p.setFont(QFont("Microsoft YaHei UI", 30, QFont.Bold))
        p.drawText(pm.rect(), Qt.AlignCenter, "V")
        p.end()
        return QIcon(pm)

    def _make_tray(self, app):
        tray = QSystemTrayIcon(self.win)
        icon = self._tray_icon()
        tray.setIcon(icon)
        self.win.setWindowIcon(icon)   # 任务栏也用同一图标
        tray.setToolTip("VTranslate 语音翻译")

        menu = QMenu()
        self._act_run = QAction("开始翻译")
        self._act_run.triggered.connect(
            lambda: self.set_running(not self.running))
        self._act_ov = QAction("显示字幕窗")
        self._act_ov.setCheckable(True)
        self._act_ov.triggered.connect(
            lambda on: self.overlay.show() if on else self.overlay.hide())
        self._act_through = QAction("字幕穿透")
        self._act_through.setCheckable(True)
        self._act_through.triggered.connect(
            lambda on: self.overlay._on_pin_toggle(on))
        act_main = QAction("打开主窗口")
        act_main.triggered.connect(self._raise_main)
        act_settings = QAction("设置")
        act_settings.triggered.connect(self.show_settings)
        act_quit = QAction("退出")
        act_quit.triggered.connect(app.quit)
        for a in (act_main, self._act_run, self._act_ov, self._act_through):
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction(act_settings)
        menu.addSeparator()
        menu.addAction(act_quit)

        def _sync():
            self._act_run.setText("停止翻译" if self.running else "开始翻译")
            self._act_ov.setChecked(self.overlay.isVisible())
            self._act_through.setChecked(self.cfg["click_through"])
        menu.aboutToShow.connect(_sync)

        tray.setContextMenu(menu)
        tray.activated.connect(
            lambda r: self._raise_main()
            if r == QSystemTrayIcon.Trigger else None)
        tray.show()
        self.tray = tray

    def _raise_main(self):
        self.win.show()
        self.win.raise_()
        self.win.activateWindow()


_SINGLE_KEY = "vtranslate-single-instance"


def _try_raise_existing() -> bool:
    """已有实例在跑：通知它弹出窗口后退出本进程。"""
    sock = QLocalSocket()
    sock.connectToServer(_SINGLE_KEY)
    if not sock.waitForConnected(300):
        return False
    sock.write(b"raise")
    sock.flush()
    sock.waitForBytesWritten(300)
    return True


def _listen_single(app_obj: "App") -> QLocalServer:
    srv = QLocalServer()
    QLocalServer.removeServer(_SINGLE_KEY)
    srv.listen(_SINGLE_KEY)

    def _on_conn():
        while srv.hasPendingConnections():
            c = srv.nextPendingConnection()
            c.waitForReadyRead(100)
            c.close()
        app_obj._raise_main()
        app_obj.overlay.show()
    srv.newConnection.connect(_on_conn)
    return srv


def main():
    import ctypes
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "chunfff.VTranslate")
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("VTranslate")

    if _try_raise_existing():
        sys.exit(0)

    from main_window import APP_QSS
    app.setStyleSheet(APP_QSS)

    a = App()
    a._single_srv = _listen_single(a)   # 防 GC
    if not a.init(app):
        sys.exit(0)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
