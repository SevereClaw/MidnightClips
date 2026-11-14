"""Наборы настроек: готовые и свои. Свои хранятся в presets.json рядом с настройками."""
import json
import os
import threading

KEYS = ("height", "fps", "bitrate_mbps", "buffer_seconds")   # что входит в набор
VIDEO_KEYS = ("height", "fps", "bitrate_mbps")               # что влияет на саму запись

BUILTIN = [
    {"name": "Слабый компьютер", "height": 720, "fps": 30, "bitrate_mbps": 6, "buffer_seconds": 30},
    {"name": "Для Discord", "height": 720, "fps": 60, "bitrate_mbps": 6, "buffer_seconds": 30},
    {"name": "Стандарт", "height": 1080, "fps": 60, "bitrate_mbps": 12, "buffer_seconds": 60},
    {"name": "Высокое качество", "height": 0, "fps": 60, "bitrate_mbps": 20, "buffer_seconds": 60},
    {"name": "Плавность", "height": 0, "fps": 120, "bitrate_mbps": 50, "buffer_seconds": 60},
    {"name": "Долгие моменты", "height": 1080, "fps": 30, "bitrate_mbps": 12, "buffer_seconds": 600},
]


class Presets:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "presets.json")
        self._lock = threading.Lock()
        self.custom = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for item in json.load(f):
                    clean = self._clean(item.get("name", ""), item)
                    if clean:
                        self.custom.append(clean)
        except (OSError, ValueError, AttributeError, TypeError):
            pass

    @staticmethod
    def _clean(name, values):
        name = " ".join(str(name).split())[:40]
        if not name:
            return None
        try:
            return {"name": name, "height": max(0, int(values["height"])),
                    "fps": max(15, min(240, int(values["fps"]))),
                    "bitrate_mbps": max(2, min(100, int(values["bitrate_mbps"]))),
                    "buffer_seconds": max(5, min(1800, int(values["buffer_seconds"])))}
        except (KeyError, TypeError, ValueError):
            return None

    def all(self):
        return [dict(p, builtin=True) for p in BUILTIN] + [dict(p, builtin=False) for p in self.custom]

    def get(self, name):
        for preset in BUILTIN + self.custom:
            if preset["name"] == name:
                return preset
        return None

    def _write(self):
        try:
            with open(self.path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(self.custom, f, ensure_ascii=False, indent=1)
            os.replace(self.path + ".tmp", self.path)
        except OSError:
            pass

    def save(self, name, values):
        """Сохранить свой набор. Возвращает текст ошибки или пустую строку."""
        clean = self._clean(name, values)
        if not clean:
            return "Введите название набора."
        if any(p["name"].lower() == clean["name"].lower() for p in BUILTIN):
            return "Такое название у готового набора — выберите другое."
        with self._lock:
            self.custom = [p for p in self.custom if p["name"].lower() != clean["name"].lower()] + [clean]
            if len(self.custom) > 30:
                return "Слишком много наборов — удалите ненужные."
            self._write()
        return ""

    def delete(self, name):
        with self._lock:
            before = len(self.custom)
            self.custom = [p for p in self.custom if p["name"] != name]
            if len(self.custom) != before:
                self._write()
                return True
        return False
