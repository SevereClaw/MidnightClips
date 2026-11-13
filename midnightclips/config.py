"""Настройки: хранятся в %APPDATA%/MidnightClips/settings.json."""
import json
import os
import sys
import threading

from . import APP_NAME


def data_dir():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def cache_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".cache")
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def resource_dir():
    """Папка с ui/, assets/ и ffmpeg.exe (и в сборке PyInstaller, и из исходников)."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_clips_dir():
    return os.path.join(os.path.expanduser("~"), "Videos", APP_NAME)


DEFAULTS = {
    "clips_dir": "",            # пусто = Видео/MidnightClips
    "buffer_enabled": True,     # постоянная запись в буфер повтора
    "buffer_seconds": 60,       # сколько секунд сохраняет клип
    "fps": 60,
    "height": 0,                # 0 = как на экране, иначе 1080 / 720
    "bitrate_mbps": 12,
    "monitor": 0,
    "encoder": "auto",          # auto / h264_nvenc / h264_amf / h264_qsv / libx264
    "capture": "auto",          # auto / dda / gdi
    "audio": True,              # звук системы
    "mic": "",                  # имя микрофона (dshow), пусто = выключен
    "rec_volume_system": 100,   # громкость звука компьютера в записи, 0–200 %
    "rec_volume_mic": 100,      # громкость микрофона в записи, 0–200 %
    "audio_offset_ms": 0,       # сдвиг звука, если он отстаёт или спешит
    "hotkey_clip": "Alt+F10",
    "hotkey_record": "Alt+F9",
    "folder_per_game": True,
    "sound": True,
    "toast": True,
    "autostart": False,
    "auto_update": True,
    "sound_volume": 100,        # громкость сигналов, 0–200 %
    "sound_saved": "",          # свои файлы сигналов; пусто = стандартный
    "sound_rec_on": "",
    "sound_rec_off": "",
    "sound_error": "",
    "indicator": True,          # красная точка поверх экрана, пока идёт запись
    "indicator_corner": "tl",   # tl / tr / bl / br
    "cleanup_days": 0,          # удалять клипы старше N дней; 0 = не удалять
    "cleanup_gb": 0,            # держать папку клипов в пределах N ГБ; 0 = без предела
    "discord_webhook": "",      # ссылка вебхука канала
    "discord_limit_mb": 10,     # предел размера файла при сжатии и отправке
}

# Изменение этих ключей требует перезапуска захвата.
CAPTURE_KEYS = {
    "buffer_enabled", "buffer_seconds", "fps", "height", "bitrate_mbps", "monitor",
    "encoder", "capture", "audio", "mic", "audio_offset_ms", "rec_volume_system", "rec_volume_mic",
}


class Config:
    def __init__(self, path=None):
        self.path = path or os.path.join(data_dir(), "settings.json")
        self._lock = threading.Lock()
        self.data = dict(DEFAULTS)
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            for key, value in saved.items():
                if key in DEFAULTS and isinstance(value, type(DEFAULTS[key])):
                    self.data[key] = value
        except (OSError, ValueError):
            pass

    def __getitem__(self, key):
        return self.data[key]

    def clips_dir(self):
        path = self.data["clips_dir"] or default_clips_dir()
        os.makedirs(path, exist_ok=True)
        return path

    def update(self, values):
        """Возвращает множество реально изменившихся ключей."""
        changed = set()
        with self._lock:
            for key, value in values.items():
                if key not in DEFAULTS:
                    continue
                default = DEFAULTS[key]
                try:
                    if isinstance(default, bool):
                        value = bool(value)
                    elif isinstance(default, int):
                        value = int(value)
                    else:
                        value = str(value)
                except (TypeError, ValueError):
                    continue
                if self.data[key] != value:
                    self.data[key] = value
                    changed.add(key)
            if changed:
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
        return changed
