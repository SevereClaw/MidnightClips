"""Сигналы программы: свои файлы и громкость.

Windows умеет просто проигрывать только WAV и без регулировки громкости, поэтому
выбранный файл (wav, mp3, ogg…) один раз пересобирается через ffmpeg в WAV нужной
громкости и кладётся в кэш.
"""
import hashlib
import os
import subprocess
import sys
import threading

from . import winutil

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
EVENTS = ("saved", "rec_on", "rec_off", "error", "marker")
MAX_SECONDS = 8


class SoundBank:
    def __init__(self, cfg, ffmpeg, assets_dir, cache_dir):
        self.cfg = cfg
        self.ffmpeg = ffmpeg
        self.assets = assets_dir
        self.cache = os.path.join(cache_dir, "sounds")
        os.makedirs(self.cache, exist_ok=True)
        self._lock = threading.Lock()

    def default(self, event):
        return os.path.join(self.assets, event + ".wav")

    def source(self, event):
        custom = self.cfg["sound_" + event]
        return custom if custom and os.path.isfile(custom) else self.default(event)

    def prepared(self, event):
        """Путь к готовому WAV с учётом выбранного файла и громкости."""
        src = self.source(event)
        volume = max(0, min(200, int(self.cfg["sound_volume"])))
        if not self.ffmpeg or (volume == 100 and src == self.default(event)):
            return src
        try:
            stat = os.stat(src)
        except OSError:
            return self.default(event)
        key = hashlib.sha1(("%s|%d|%d|%d" % (src, stat.st_size, int(stat.st_mtime), volume)).encode("utf-8")).hexdigest()[:16]
        out = os.path.join(self.cache, "%s_%s.wav" % (event, key))
        with self._lock:
            if os.path.isfile(out) and os.path.getsize(out) > 44:
                return out
            try:
                ok = subprocess.run(
                    [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-vn",
                     "-t", str(MAX_SECONDS), "-af", "volume=%.2f" % (volume / 100.0),
                     "-ar", "44100", "-c:a", "pcm_s16le", out],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=30, creationflags=NO_WINDOW).returncode == 0
            except (OSError, subprocess.SubprocessError):
                ok = False
            if not ok or not os.path.isfile(out) or os.path.getsize(out) <= 44:
                try:
                    os.remove(out)
                except OSError:
                    pass
                return self.default(event)   # файл не читается — играем стандартный
            for name in os.listdir(self.cache):   # старые версии этого сигнала больше не нужны
                if name.startswith(event + "_") and name != os.path.basename(out):
                    try:
                        os.remove(os.path.join(self.cache, name))
                    except OSError:
                        pass
            return out

    def play(self, event, force=False):
        if event not in EVENTS or not (force or self.cfg["sound"]):
            return
        if int(self.cfg["sound_volume"]) <= 0:
            return
        threading.Thread(target=lambda: winutil.play_sound(self.prepared(event)), daemon=True).start()
