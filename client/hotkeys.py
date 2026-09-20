"""全局热键：pynput 监听键盘与鼠标侧键，按住说话（PTT）。

hotkey 配置格式: {"device": "mouse", "key": "x1"|"x2"|"middle"}
             或 {"device": "keyboard", "key": "f9"|"caps_lock"|"a"...}
"""
import logging
import threading

from pynput import keyboard, mouse

log = logging.getLogger("hotkeys")

_MOUSE_KEYS = {
    "x1": mouse.Button.x1, "x2": mouse.Button.x2,
    "middle": mouse.Button.middle, "left": mouse.Button.left,
    "right": mouse.Button.right,
}


def _kb_key(name: str):
    try:
        return keyboard.Key[name]
    except KeyError:
        return keyboard.KeyCode.from_char(name)


class PTTListener:
    """on_press / on_release 回调。重复触发已去抖。"""

    def __init__(self, hotkey: dict, on_press, on_release):
        self.hotkey = hotkey or {"device": "mouse", "key": "x1"}
        self.on_press = on_press
        self.on_release = on_release
        self._held = False
        self._lock = threading.Lock()
        self._kb_listener = None
        self._ms_listener = None

    # ---- 命中判断 ----
    def _match_kb(self, key) -> bool:
        if self.hotkey.get("device") != "keyboard":
            return False
        want = _kb_key(self.hotkey.get("key", "f9"))
        if isinstance(want, keyboard.Key):
            return key == want
        return getattr(key, "char", None) == want.char

    def _match_mouse(self, button) -> bool:
        return (self.hotkey.get("device") == "mouse"
                and button == _MOUSE_KEYS.get(self.hotkey.get("key"), mouse.Button.x1))

    # ---- 事件 ----
    def _fire_press(self):
        with self._lock:
            if self._held:
                return
            self._held = True
        self.on_press()

    def _fire_release(self):
        with self._lock:
            if not self._held:
                return
            self._held = False
        self.on_release()

    def start(self):
        self._kb_listener = keyboard.Listener(
            on_press=lambda k: self._fire_press() if self._match_kb(k) else None,
            on_release=lambda k: self._fire_release() if self._match_kb(k) else None,
        )
        self._ms_listener = mouse.Listener(
            on_click=lambda x, y, b, p: (
                self._fire_press() if p else self._fire_release())
            if self._match_mouse(b) else None,
        )
        self._kb_listener.start()
        self._ms_listener.start()
        log.info("PTT 热键已绑定: %s", self.hotkey)

    def stop(self):
        for l in (self._kb_listener, self._ms_listener):
            try:
                if l:
                    l.stop()
            except Exception:
                pass


class HotkeyRecorder:
    """设置页"按下任意键录入"用：抓到第一个键盘/鼠标键后回调并停止。"""

    def __init__(self, on_captured):
        self.on_captured = on_captured
        self._ls = []

    def start(self):
        def kb_press(key):
            name = key.name if isinstance(key, keyboard.Key) else getattr(key, "char", None)
            if name:
                self._done({"device": "keyboard", "key": str(name)})

        def ms_click(x, y, button, pressed):
            if pressed:
                name = str(button).split(".")[-1]  # Button.x1 -> x1
                self._done({"device": "mouse", "key": name})

        kl = keyboard.Listener(on_press=kb_press)
        ml = mouse.Listener(on_click=ms_click)
        self._ls = [kl, ml]
        kl.start()
        ml.start()

    def _done(self, spec):
        self.stop()
        self.on_captured(spec)

    def stop(self):
        for l in self._ls:
            try:
                l.stop()
            except Exception:
                pass
        self._ls = []
