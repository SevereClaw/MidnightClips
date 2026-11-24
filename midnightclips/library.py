"""Список сохранённых клипов, превью и длительность."""
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
VIDEO_EXT = (".mp4", ".mkv", ".mov", ".webm")
MARKS_FILE = ".midnightclips.json"   # избранное и теги, лежит в папке клипов


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
        self._marks = {}
        self._marks_dir = None

    # ------------------------------------------------- избранное и теги

    def _load_marks(self):
        """Пометки хранятся рядом с клипами: {относительный путь: {"fav": bool, "tags": [...]}}."""
        root = self.cfg.clips_dir()
        if self._marks_dir != root:
            self._marks_dir = root
            try:
                with open(os.path.join(root, MARKS_FILE), "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._marks = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                self._marks = {}
        return self._marks

    def _save_marks(self):
        path = os.path.join(self._marks_dir, MARKS_FILE)
        try:
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(self._marks, f, ensure_ascii=False, indent=1)
            os.replace(path + ".tmp", path)
        except OSError:
            pass

    def _rel(self, path):
        return os.path.relpath(os.path.realpath(path), os.path.realpath(self.cfg.clips_dir())).replace(os.sep, "/")

    def _set_mark(self, path, **values):
        if not self.inside(path):
            return False
        with self._lock:
            marks = self._load_marks()
            entry = dict(marks.get(self._rel(path), {}))
            entry.update(values)
            if any(entry.get(k) for k in ("fav", "tags", "markers", "voices", "voices_off")):
                clean = {"fav": bool(entry.get("fav")), "tags": entry.get("tags") or []}
                for key in ("markers", "voices", "voices_off"):
                    if entry.get(key):
                        clean[key] = entry[key]
                marks[self._rel(path)] = clean
            else:
                marks.pop(self._rel(path), None)
            self._save_marks()
        return True

    def set_favorite(self, path, value):
        return self._set_mark(path, fav=bool(value))

    def set_tags(self, path, tags):
        clean, seen = [], set()
        for tag in tags or []:
            tag = " ".join(str(tag).replace("#", " ").replace(",", " ").split())[:24]
            if tag and tag.lower() not in seen:
                seen.add(tag.lower())
                clean.append(tag)
        return self._set_mark(path, tags=clean[:8])

    def set_markers(self, path, seconds):
        """Отметки в клипе (секунды от начала): поставлены клавишей во время игры или в окне клипа."""
        clean = []
        for value in list(seconds or [])[:300]:
            try:
                value = round(float(value), 2)
            except (TypeError, ValueError):
                continue
            if value >= 0 and all(abs(value - v) > 0.3 for v in clean):
                clean.append(value)
        return self._set_mark(path, markers=sorted(clean))

    def set_voices(self, path, voices):
        """Кто и когда говорил в Discord (из Steal Your Voice): [{"name", "s", "e"}] — секунды от начала клипа."""
        clean = []
        for v in list(voices or [])[:5000]:
            try:
                s, e = round(float(v["s"]), 2), round(float(v["e"]), 2)
                name = str(v["name"]).strip()[:40] or "Друг"
            except (KeyError, TypeError, ValueError):
                continue
            if e - s >= 0.05:
                clean.append({"name": name, "s": s, "e": e})
        return self._set_mark(path, voices=clean)

    def set_voices_off(self, path, names):
        """Друзья, которых в этом клипе не должно быть слышно (Discord приглушается в их фразы)."""
        return self._set_mark(path, voices_off=sorted({str(n)[:40] for n in names or [] if str(n).strip()}))

    def marks_of(self, path):
        with self._lock:
            return dict(self._load_marks().get(self._rel(path), {})) if self.inside(path) else {}

    def forget(self, path):
        """Клип удалён — убрать его пометки."""
        with self._lock:
            if self._load_marks().pop(self._rel(path), None) is not None:
                self._save_marks()

    def _move_mark(self, old, new):
        with self._lock:
            marks = self._load_marks()
            entry = marks.pop(self._rel(old), None)
            if entry is not None:
                marks[self._rel(new)] = entry
                self._save_marks()

    # -------------------------------------------------------- автоочистка

    def cleanup_plan(self, days, gigabytes, now=None):
        """Какие клипы уйдут при таких настройках. Избранное и только что записанное не трогаем."""
        now = now or time.time()
        clips = self.list()
        doomed = []
        candidates = [c for c in clips if not c["favorite"] and now - c["mtime"] > 600]
        if days > 0:
            doomed = [c for c in candidates if now - c["mtime"] > days * 86400]
        if gigabytes > 0:
            total = sum(c["size"] for c in clips) - sum(c["size"] for c in doomed)
            limit = gigabytes * 1024 ** 3
            gone = {c["path"] for c in doomed}
            for clip in sorted(candidates, key=lambda c: c["mtime"]):   # от самых старых
                if total <= limit:
                    break
                if clip["path"] not in gone:
                    doomed.append(clip)
                    total -= clip["size"]
        return doomed

    def cleanup(self, delete):
        """Выполнить автоочистку по настройкам. delete(path) -> bool. Возвращает (сколько, байт)."""
        days, gigabytes = int(self.cfg["cleanup_days"]), int(self.cfg["cleanup_gb"])
        if days <= 0 and gigabytes <= 0:
            return 0, 0
        count = freed = 0
        for clip in self.cleanup_plan(days, gigabytes):
            if self.inside(clip["path"]) and delete(clip["path"]):
                count += 1
                freed += clip["size"]
                self.forget(clip["path"])
                folder = os.path.dirname(clip["path"])
                try:
                    if self.inside(folder) and not os.listdir(folder):
                        os.rmdir(folder)
                except OSError:
                    pass
        return count, freed

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
        with self._lock:
            marks = dict(self._load_marks())
        real_root = os.path.realpath(root)
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
                mark = marks.get(os.path.relpath(os.path.realpath(path), real_root).replace(os.sep, "/"), {})
                clips.append({
                    "favorite": bool(mark.get("fav")),
                    "tags": list(mark.get("tags") or []),
                    "markers": list(mark.get("markers") or []),
                    "voices": list(mark.get("voices") or []),
                    "voices_off": list(mark.get("voices_off") or []),
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

    def thumb_file(self, path):
        """Готовый кадр-превью (JPEG) клипа или None, если его ещё не делали."""
        try:
            jpg = os.path.join(self.thumbs, self._key(path, os.stat(path)) + ".jpg")
        except OSError:
            return None
        return jpg if os.path.isfile(jpg) else None

    def thumb(self, path, force=False):
        """Вернуть {'image': data-URL, 'duration': секунды} — кадр делает ffmpeg и кладёт в кэш.
        force — файл вне папки клипов, но его можно показывать (видео, добавленное в монтаж)."""
        if not force and not self.inside(path):
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
        self._move_mark(path, target)
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
