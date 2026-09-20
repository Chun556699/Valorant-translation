"""译文 TTS 播报：24kHz mono PCM 队列 -> 扬声器。"""
import logging
import queue
import threading

import pyaudiowpatch as pyaudio

log = logging.getLogger("player")

RATE = 24000


class PCMPlayer:
    def __init__(self, device_index=None):
        self.pa = pyaudio.PyAudio()
        self.device_index = device_index
        self.q: queue.Queue = queue.Queue(maxsize=300)
        self._running = False
        self._playing = False
        self._stream = None
        self._thread = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="pcm-player")
        self._thread.start()

    def _loop(self):
        try:
            self._stream = self.pa.open(
                format=pyaudio.paInt16, channels=1, rate=RATE,
                output=True, output_device_index=self.device_index,
                frames_per_buffer=2400)
        except Exception as e:
            log.warning("播放设备打开失败: %s", e)
            self._running = False
            return
        while self._running:
            try:
                chunk = self.q.get(timeout=0.3)
            except queue.Empty:
                self._playing = False
                continue
            if chunk is None:
                break
            self._playing = True
            try:
                self._stream.write(chunk)
            except Exception as e:
                log.warning("播放失败: %s", e)
                break
        try:
            self._stream.stop_stream()
            self._stream.close()
        except Exception:
            pass
        self._stream = None

    def is_active(self) -> bool:
        """是否正在播放（用于抑制 TTS 回声进入回环）。"""
        return self._playing or not self.q.empty()

    def play(self, pcm24k: bytes):
        if not self._running:
            return
        try:
            self.q.put_nowait(pcm24k)
        except queue.Full:
            try:
                self.q.get_nowait()   # 丢最旧的一帧，避免积压爆音
                self.q.put_nowait(pcm24k)
            except queue.Empty:
                pass

    def stop(self):
        self._running = False
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass
        if self._thread:
            self._thread.join(timeout=1.5)
            self._thread = None

    def terminate(self):
        self.stop()
        try:
            self.pa.terminate()
        except Exception:
            pass
