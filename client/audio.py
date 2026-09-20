"""音频采集：麦克风（按住说话）+ WASAPI 系统回环（队友语音）。

只读音频设备，不触碰任何游戏进程/内存/数据。
统一输出 16kHz mono s16le PCM bytes，喂给中转服务器。
"""
import logging
import queue

import numpy as np
import pyaudiowpatch as pyaudio

log = logging.getLogger("audio")

TARGET_RATE = 16000
CHUNK_16K = 1600          # 100ms @16k


def resample_to_16k(pcm: bytes, src_rate: int, src_channels: int) -> bytes:
    """任意 (rate,channels) s16le -> 16k mono s16le。线性插值，对 ASR 足够。"""
    if src_rate == TARGET_RATE and src_channels == 1:
        return pcm
    data = np.frombuffer(pcm, dtype=np.int16)
    if src_channels > 1:
        data = data.reshape(-1, src_channels).mean(axis=1).astype(np.int16)
    if src_rate != TARGET_RATE:
        n_out = int(len(data) * TARGET_RATE / src_rate)
        if n_out <= 0:
            return b""
        x_old = np.linspace(0.0, 1.0, num=len(data), endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
        data = np.interp(x_new, x_old, data.astype(np.float32)).astype(np.int16)
    return data.tobytes()


def list_input_devices(pa: pyaudio.PyAudio):
    out = []
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        if info.get("maxInputChannels", 0) > 0 and not info.get("isLoopbackDevice", False):
            out.append((i, info["name"]))
    return out


def list_loopback_devices(pa: pyaudio.PyAudio):
    out = []
    try:
        for lb in pa.get_loopback_device_info_generator():
            out.append((lb["index"], lb["name"]))
    except Exception as e:
        log.warning("枚举回环设备失败: %s", e)
    return out


def default_loopback_index(pa: pyaudio.PyAudio):
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        return pa.get_device_info_by_index(wasapi["defaultOutputDevice"])["index"]
    except Exception:
        devs = list_loopback_devices(pa)
        return devs[0][0] if devs else None


class _BaseCapture:
    """把采集回调的 PCM 帧放进 out_q（16k mono s16le）。"""
    def __init__(self, out_q: queue.Queue):
        self.pa = pyaudio.PyAudio()
        self.out_q = out_q
        self.stream = None
        self.running = False

    def _enqueue_16k(self, pcm16k: bytes):
        try:
            self.out_q.put_nowait(pcm16k)
        except queue.Full:
            pass

    def stop(self):
        self.running = False
        try:
            if self.stream:
                self.stream.stop_stream()
                self.stream.close()
        except Exception:
            pass
        self.stream = None

    def terminate(self):
        self.stop()
        try:
            self.pa.terminate()
        except Exception:
            pass


class MicCapture(_BaseCapture):
    """麦克风采集：上层按住说话时才从队列取数据，平时丢弃即可。"""

    def start(self, device_index=None):
        self.stop()
        self.running = True

        def cb(in_data, frame_count, time_info, status):
            if self.running:
                self._enqueue_16k(in_data)
            return (None, pyaudio.paContinue)

        self.stream = self.pa.open(
            format=pyaudio.paInt16, channels=1, rate=TARGET_RATE,
            input=True, input_device_index=device_index,
            frames_per_buffer=CHUNK_16K, stream_callback=cb,
        )
        self.stream.start_stream()


class LoopbackCapture(_BaseCapture):
    """系统扬声器回环采集（队友语音随游戏音频一起从这里输出）。"""

    def start(self, device_index=None):
        self.stop()
        idx = device_index if device_index is not None else default_loopback_index(self.pa)
        if idx is None:
            raise RuntimeError("未找到 WASAPI 回环设备")
        info = self.pa.get_device_info_by_index(idx)
        src_rate = int(info["defaultSampleRate"])
        src_ch = max(1, int(info["maxInputChannels"]))
        self.running = True

        def cb(in_data, frame_count, time_info, status):
            if self.running and in_data:
                self._enqueue_16k(resample_to_16k(in_data, src_rate, src_ch))
            return (None, pyaudio.paContinue)

        self.stream = self.pa.open(
            format=pyaudio.paInt16, channels=src_ch, rate=src_rate,
            input=True, input_device_index=idx,
            frames_per_buffer=int(src_rate * 0.05),  # 50ms
            stream_callback=cb,
        )
        self.stream.start_stream()
        log.info("回环采集已启动: %s (%dHz %dch)", info["name"], src_rate, src_ch)


def drain_queue(q: queue.Queue, timeout=0.5):
    """阻塞取一帧；顺带清空积压防止延迟堆积。"""
    item = q.get(timeout=timeout)
    latest = item
    while True:
        try:
            latest += q.get_nowait()
        except queue.Empty:
            break
    return latest
