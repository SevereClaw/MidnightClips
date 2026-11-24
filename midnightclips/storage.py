"""Где лежат клипы: выбор папки (в том числе на другом диске) и перенос уже сохранённых клипов."""
import json
import os
import shutil
import threading
import time

from . import APP_NAME

VIDEO_EXT = (".mp4", ".mkv", ".mov", ".webm")
MARKS_FILE = ".midnightclips.json"   # избранное и теги — переезжают вместе с клипами
CHUNK = 8 * 1024 * 1024


def resolve_target(path):
    """Папка, в которую на самом деле пойдут клипы. Если выбран корень диска (D:\\) —
    клипы кладутся в D:\\MidnightClips, а не вперемешку с остальным диском."""
    path = os.path.abspath(os.path.expanduser(str(path or "").strip().strip('"')))
    drive, rest = os.path.splitdrive(path)
    if rest in ("", os.sep, "/", "\\") or os.path.dirname(path) == path:
        path = os.path.join(drive + os.sep if drive else path, APP_NAME)
    return path


def check_writable(path):
    """Пустая строка — сюда можно сохранять; иначе — понятная причина."""
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        return "Не удалось создать папку %s: %s" % (path, exc.strerror or exc)
    probe = os.path.join(path, ".midnightclips-check-%d" % os.getpid())
    try:
        with open(probe, "wb") as f:
            f.write(b"ok")
        os.remove(probe)
    except OSError as exc:
        return "В папку %s нельзя записывать: %s" % (path, exc.strerror or exc)
    return ""


def same(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def inside(child, parent):
    child, parent = os.path.normcase(os.path.realpath(child)), os.path.normcase(os.path.realpath(parent))
    try:
        return os.path.commonpath([child, parent]) == parent and child != parent
    except ValueError:   # разные диски
        return False


def clip_files(root, skip=None):
    """Все клипы в папке (с подпапками игр). skip — подпапка, которую не трогать (новая папка внутри старой)."""
    out = []
    if not os.path.isdir(root):
        return out
    for folder, dirs, files in os.walk(root):
        if skip:
            dirs[:] = [d for d in dirs if not same(os.path.join(folder, d), skip)]
        for name in files:
            if name.lower().endswith(VIDEO_EXT):
                path = os.path.join(folder, name)
                try:
                    out.append((path, os.path.getsize(path)))
                except OSError:
                    pass
    return out


def summary(root, skip=None):
    files = clip_files(root, skip)
    return {"count": len(files), "bytes": sum(size for _p, size in files)}


def free_space(path):
    """Свободное место на диске папки (байты) или None."""
    probe = path
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def _unique(path):
    base, ext = os.path.splitext(path)
    n, out = 2, path
    while os.path.exists(out) or os.path.exists(out + ".part"):
        out = "%s (%d)%s" % (base, n, ext)
        n += 1
    return out


class Mover:
    """Переносит клипы из старой папки в новую в отдельном потоке.

    На том же диске файл просто переименовывается (мгновенно). На другой диск — копируется
    кусками с ходом работы, проверяется размер, и только потом исходный файл удаляется:
    если что-то пойдёт не так, клип останется на старом месте.
    """

    def __init__(self, on_event):
        self.on_event = on_event   # "move" {done, total, percent, name} / "move_done" {ok, moved, failed, text}
        self.busy = False
        self._cancel = False

    def start(self, old_root, new_root):
        if self.busy:
            return False
        self.busy = True
        self._cancel = False
        threading.Thread(target=self._run, args=(old_root, new_root), daemon=True, name="move-clips").start()
        return True

    def cancel(self):
        self._cancel = True

    def _run(self, old_root, new_root):
        moved, failed, renamed = 0, [], {}
        try:
            skip = new_root if inside(new_root, old_root) else None
            files = clip_files(old_root, skip)
            total = sum(size for _p, size in files) or 1
            need = sum(size for _p, size in files)
            free = free_space(new_root)
            other_drive = (os.path.splitdrive(os.path.realpath(old_root))[0].upper()
                           != os.path.splitdrive(os.path.realpath(new_root))[0].upper())
            if other_drive and free is not None and free < need + 200 * 1024 ** 2:
                self.on_event("move_done", {"ok": False, "moved": 0, "failed": len(files), "text":
                                            "На новом диске не хватает места: нужно %.1f ГБ, свободно %.1f ГБ." % (
                                                need / 1024 ** 3, free / 1024 ** 3)})
                return
            done = 0
            last = 0.0
            for path, size in files:
                if self._cancel:
                    break
                rel = os.path.relpath(path, old_root)
                target = _unique(os.path.join(new_root, rel))
                name = os.path.basename(path)

                def progress(extra, _done=done, _name=name):
                    nonlocal last
                    now = time.monotonic()
                    if now - last > 0.25:
                        last = now
                        self.on_event("move", {"done": _done + extra, "total": total,
                                               "percent": int((_done + extra) * 100 / total), "name": _name})

                progress(0)
                try:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    self._move_one(path, target, size, progress)
                    moved += 1
                    new_rel = os.path.relpath(target, new_root)
                    if new_rel != rel:
                        renamed[rel.replace(os.sep, "/")] = new_rel.replace(os.sep, "/")
                except InterruptedError:
                    break
                except OSError as exc:
                    failed.append("%s: %s" % (name, exc.strerror or exc))
                done += size
            self._move_marks(old_root, new_root, renamed)
            self._remove_empty(old_root, skip)
            if self._cancel:
                text = "Перенос остановлен. Перенесено клипов: %d, остальные остались на старом месте." % moved
            elif failed:
                text = "Перенесено клипов: %d. Не удалось перенести %d: %s" % (moved, len(failed), "; ".join(failed[:3]))
            else:
                text = "Клипы перенесены: %d" % moved
            self.on_event("move_done", {"ok": not failed and not self._cancel, "moved": moved,
                                        "failed": len(failed), "text": text})
        except Exception as exc:   # интерфейс не должен остаться в «переношу…»
            self.on_event("move_done", {"ok": False, "moved": moved, "failed": len(failed), "text": "Ошибка переноса: %s" % exc})
        finally:
            self.busy = False

    def _move_one(self, src, dst, size, progress):
        try:
            os.rename(src, dst)   # тот же диск — мгновенно
            return
        except OSError:
            pass   # другой диск (или переименовать нельзя) — копируем
        part = dst + ".part"
        try:
            copied = 0
            with open(src, "rb") as fin, open(part, "wb") as fout:
                while True:
                    if self._cancel:
                        raise InterruptedError
                    block = fin.read(CHUNK)
                    if not block:
                        break
                    fout.write(block)
                    copied += len(block)
                    progress(copied)
                fout.flush()
                os.fsync(fout.fileno())
            if os.path.getsize(part) != size:
                raise OSError("скопировался не целиком")
            shutil.copystat(src, part)   # дата клипа в галерее остаётся прежней
            os.replace(part, dst)
        except BaseException:
            try:
                os.remove(part)
            except OSError:
                pass
            raise
        try:
            os.remove(src)
        except OSError:
            pass   # копия уже на новом месте; исходник занят плеером — останется на старом месте

    @staticmethod
    def _move_marks(old_root, new_root, renamed):
        """Избранное и теги — в новую папку (с учётом уже лежащих там)."""
        old_file = os.path.join(old_root, MARKS_FILE)
        try:
            with open(old_file, "r", encoding="utf-8") as f:
                old = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(old, dict) or not old:
            return
        new_file = os.path.join(new_root, MARKS_FILE)
        try:
            with open(new_file, "r", encoding="utf-8") as f:
                merged = json.load(f)
            if not isinstance(merged, dict):
                merged = {}
        except (OSError, ValueError):
            merged = {}
        for key, value in old.items():
            if os.path.isfile(os.path.join(new_root, renamed.get(key, key))):
                merged[renamed.get(key, key)] = value
        try:
            with open(new_file + ".tmp", "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False, indent=1)
            os.replace(new_file + ".tmp", new_file)
            left = {k: v for k, v in old.items() if os.path.isfile(os.path.join(old_root, k))}
            if left:
                with open(old_file, "w", encoding="utf-8") as f:
                    json.dump(left, f, ensure_ascii=False, indent=1)
            else:
                os.remove(old_file)
        except OSError:
            pass

    @staticmethod
    def _remove_empty(root, skip):
        """Убрать опустевшие папки игр в старом месте (саму старую папку не трогаем)."""
        for folder, _dirs, _files in os.walk(root, topdown=False):
            if same(folder, root) or (skip and (same(folder, skip) or inside(folder, skip))):
                continue
            try:
                os.rmdir(folder)
            except OSError:
                pass
