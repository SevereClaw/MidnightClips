"""Звук системы (WASAPI loopback) -> ровный PCM-поток в stdin ffmpeg.

Loopback-захват молчит, когда в системе ничего не играет, поэтому AudioPump
сам держит темп по часам и дописывает тишину — иначе звук в клипе «съедет».
"""
import math
import queue
import struct
import threading
import time


class LoopbackSource:
    """Захват того, что играет на устройстве вывода по умолчанию."""

    def __init__(self):
        self._pa = None
        self._stream = None
        self._queue = queue.Queue()
        self.rate = 48000
        self.channels = 2
        self.name = ""

    def open(self):
        import pyaudiowpatch as pyaudio  # только Windows

        self._pa = pyaudio.PyAudio()
        try:
            device = self._pa.get_default_wasapi_loopback()
        except Exception:
            device = None
        if device is None:
            api = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            speakers = self._pa.get_device_info_by_index(api["defaultOutputDevice"])
            for candidate in self._pa.get_loopback_device_info_generator():
                if speakers["name"] in candidate["name"]:
                    device = candidate
                    break
        if device is None:
            raise RuntimeError("не найдено устройство вывода звука")
        self.rate = int(device["defaultSampleRate"])
        self.channels = max(1, int(device["maxInputChannels"]))
        self.name = device["name"]

        def callback(in_data, frame_count, time_info, status):
            self._queue.put(in_data)
            return (None, pyaudio.paContinue)

        self._stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=self.rate,
            input=True,
            input_device_index=device["index"],
            frames_per_buffer=max(256, self.rate // 100),
            stream_callback=callback,
        )

    def read_available(self):
        chunks = []
        while True:
            try:
                chunks.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return b"".join(chunks)

    def alive(self):
        try:
            return self._stream is not None and self._stream.is_active()
        except Exception:
            return False

    def close(self):
        for action in (
            lambda: self._stream and self._stream.stop_stream(),
            lambda: self._stream and self._stream.close(),
            lambda: self._pa and self._pa.terminate(),
        ):
            try:
                action()
            except Exception:
                pass
        self._stream = None
        self._pa = None


class FakeSource:
    """Тестовый источник: тон 440 Гц с паузой тишины (для проверки без Windows)."""

    def __init__(self, rate=48000, channels=2, gap=(3.0, 4.0)):
        self.rate, self.channels, self.name = rate, channels, "fake"
        self._t0 = None
        self._sent = 0
        self._gap = gap

    def open(self):
        self._t0 = time.monotonic()

    def read_available(self):
        now = time.monotonic() - self._t0
        if self._gap[0] <= now < self._gap[1]:
            self._sent = int(self._gap[1] * self.rate)  # как loopback: в тишине данных нет
            return b""
        target = int(now * self.rate)
        out = bytearray()
        for i in range(self._sent, target):
            v = int(9000 * math.sin(2 * math.pi * 440 * i / self.rate))
            out += struct.pack("<h", v) * self.channels
        self._sent = max(self._sent, target)
        return bytes(out)

    def alive(self):
        return True

    def close(self):
        pass


class AudioPump(threading.Thread):
    """Пишет звук в pipe строго со скоростью реального времени."""

    def __init__(self, source, pipe):
        super().__init__(daemon=True, name="audio-pump")
        self.source = source
        self.pipe = pipe
        self._stop_event = threading.Event()
        self.frames_written = 0
        self.frames_padded = 0

    def stop(self):
        self._stop_event.set()

    def run(self):
        rate = self.source.rate
        block = self.source.channels * 2
        slack = int(rate * 0.12)
        try:
            # Первая запись блокируется, пока ffmpeg не начнёт читать pipe,
            # то есть пока не откроется захват экрана — это точка отсчёта.
            self.pipe.write(bytes(block * int(rate * 0.1)))
            self.pipe.flush()
        except (OSError, ValueError):
            return
        self.source.read_available()  # отбросить накопившееся до старта
        t0 = time.monotonic()
        buf = bytearray()
        while not self._stop_event.is_set():
            buf += self.source.read_available()
            due = int((time.monotonic() - t0) * rate) - self.frames_written
            if due > 0:
                take = min(len(buf) // block, due)
                out = bytes(buf[: take * block])
                del buf[: take * block]
                deficit = due - take
                if deficit > slack:  # настоящая тишина, а не дрожание буфера
                    pad = deficit - slack // 2
                    out += bytes(pad * block)
                    take += pad
                    self.frames_padded += pad
                if out:
                    try:
                        self.pipe.write(out)
                    except (OSError, ValueError):
                        return
                    self.frames_written += take
            extra = len(buf) // block - int(rate * 0.5)
            if extra > 0:  # часы устройства убежали вперёд — срезаем лишнее
                del buf[: (extra + int(rate * 0.4)) * block]
            time.sleep(0.01)
