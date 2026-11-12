"""Список сохранённых клипов, превью и длительность."""
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import threading

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
VIDEO_EXT = (".mp4", ".mkv", ".mov", ".webm")


class Library:
    def __init__(self, cfg, ffmpeg, cache_dir):
        self.cfg = cfg
        self.ffmpeg = ffmpeg
        self.thumbs = os.path.join(cache_dir, "thumbs")
        os.makedirs(self.thumbs, exist_ok=True)
        self._meta_path = os.path.join(cache_dir, "meta.json")
        self._lock = threading.Lock()
        try:
            with open(self._meta_path, "r", encoding="utf-8") as f:
                self._meta = json.load(f)
        except (OSError, ValueError):
            self._meta = {}

    def inside(self, path):
        """Путь должен лежать внутри папки клипов — интерфейсу нельзя трогать чужие файлы."""
        root = os.path.realpath(self.cfg.clips_dir())
        real = os.path.realpath(path)
        try:
            return os.path.commonpath([root, real]) == root and real != root
        except ValueError:
            return False

    def _key(self, path, stat):
        raw = "%s|%d|%d" % (os.path.normcase(path), stat.st_size, int(stat.st_mtime))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def list(self):
        root = self.cfg.clips_dir()
        clips = []
        for folder, _dirs, files in os.walk(root):
            for name in files:
                if not name.lower().endswith(VIDEO_EXT):
                    continue
                path = os.path.join(folder, name)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                rel = os.path.relpath(folder, root)
                key = self._key(path, stat)
                clips.append({
                    "path": path,
                    "name": os.path.splitext(name)[0],
                    "game": "" if rel == "." else rel.split(os.sep)[0],
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "duration": self._meta.get(key, {}).get("duration"),
                    "recording": name.startswith("Запись") or " запись " in name,
                })
        clips.sort(key=lambda c: c["mtime"], reverse=True)
        return clips

    def thumb(self, path):
        """Вернуть {'image': data-URL, 'duration': секунды} — кадр делает ffmpeg и кладёт в кэш."""
        if not self.inside(path):
            return None
        try:
            stat = os.stat(path)
        except OSError:
            return None
        key = self._key(path, stat)
        jpg = os.path.join(self.thumbs, key + ".jpg")
        duration = self._meta.get(key, {}).get("duration")
        if not os.path.isfile(jpg) or duration is None:
            try:
                result = subprocess.run(
                    [self.ffmpeg, "-hide_banner", "-y", "-ss", "1", "-i", path, "-frames:v", "1",
                     "-vf", "scale=400:-2", "-q:v", "5", jpg],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    timeout=30, creationflags=NO_WINDOW)
                text = result.stderr.decode("utf-8", "replace")
            except (OSError, subprocess.SubprocessError):
                text = ""
            found = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", text)
            if found:
                duration = int(found.group(1)) * 3600 + int(found.group(2)) * 60 + float(found.group(3))
                with self._lock:
                    self._meta[key] = {"duration": duration}
                    if len(self._meta) > 5000:
                        for old in list(self._meta)[:1000]:
                            del self._meta[old]
                    try:
                        with open(self._meta_path, "w", encoding="utf-8") as f:
                            json.dump(self._meta, f)
                    except OSError:
                        pass
        image = None
        try:
            with open(jpg, "rb") as f:
                image = "data:image/jpeg;base64," + base64.b64encode(f.read()).decode("ascii")
        except OSError:
            pass
        return {"image": image, "duration": duration}

    def rename(self, path, new_name):
        if not self.inside(path):
            return None
        clean = "".join(c for c in new_name if c not in '<>:"/\\|?*' and ord(c) >= 32).strip(" .")[:120]
        if not clean:
            return None
        target = os.path.join(os.path.dirname(path), clean + os.path.splitext(path)[1])
        if os.path.exists(target):
            return None
        try:
            os.rename(path, target)
        except OSError:
            return None
        return target

    def list_mics(self):
        """Имена микрофонов, как их видит ffmpeg (DirectShow)."""
        if sys.platform != "win32":
            return []
        try:
            result = subprocess.run(
                [self.ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=15, creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            return []
        text = result.stderr.decode("utf-8", "replace")
        return re.findall(r'"([^"@][^"]*)"\s+\(audio\)', text)
