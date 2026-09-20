"""翻译记录：本地 JSONL 存储（不上传服务器，隐私友好）。"""
import json
import os
import threading
import time

from config import HISTORY_PATH

MAX_KEEP = 500


class History:
    def __init__(self, path: str = HISTORY_PATH):
        self.path = path
        self._lock = threading.Lock()
        self.items: list[dict] = []
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self.items.append(json.loads(line))
            self.items = self.items[-MAX_KEEP:]
        except Exception:
            self.items = []

    def add(self, speaker: str, src: str, dst: str, stream: str):
        if not (src or dst):
            return
        rec = {"t": int(time.time()), "speaker": speaker,
               "src": src, "dst": dst, "stream": stream}
        with self._lock:
            self.items.append(rec)
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except Exception:
                pass
            if len(self.items) > MAX_KEEP:
                self.items = self.items[-MAX_KEEP:]

    def list(self, limit=200) -> list[dict]:
        with self._lock:
            return list(reversed(self.items[-limit:]))

    def clear(self):
        with self._lock:
            self.items.clear()
            try:
                open(self.path, "w").close()
            except Exception:
                pass
