"""Обрезка, сжатие под лимит Discord и отправка клипа по вебхуку."""
import json
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import uuid

from . import APP_NAME, __version__

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
WEBHOOK_RE = re.compile(r"^https://(?:[a-z]+\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/[\w-]+", re.I)


class Fail(Exception):
    """Ошибка с понятным пользователю текстом."""


def probe(ffmpeg, path):
    """(длительность в секундах, кадров в секунду, высота)."""
    try:
        result = subprocess.run([ffmpeg, "-hide_banner", "-i", path], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30,
                                creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        raise Fail("Не удалось прочитать клип.")
    text = result.stderr.decode("utf-8", "replace")
    found = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not found:
        raise Fail("Не удалось прочитать клип.")
    duration = int(found.group(1)) * 3600 + int(found.group(2)) * 60 + float(found.group(3))
    fps = re.search(r"Video:.*?(\d+(?:\.\d+)?) fps", text)
    size = re.search(r"Video:.*?\b(\d{2,5})x(\d{2,5})\b", text)
    return duration, float(fps.group(1)) if fps else 30.0, int(size.group(2)) if size else 1080


def plan(limit_mb, duration):
    """Подобрать битрейт, разрешение и кадры под нужный размер файла."""
    total = limit_mb * 8 * 1024 * 1024 / duration / 1000 * 0.955   # кбит/с, с запасом на контейнер
    audio = 128 if total > 3000 else 96 if total > 700 else 64 if total > 300 else 48
    video = total - audio
    if video < 110:
        raise Fail("В %d МБ столько не уместить — обрежьте клип покороче." % limit_mb)
    if video >= 4500:
        height, fps = 1080, 60
    elif video >= 2200:
        height, fps = 720, 60
    elif video >= 900:
        height, fps = 720, 30
    elif video >= 420:
        height, fps = 480, 30
    else:
        height, fps = 360, 30
    return int(video), audio, height, fps


def unique(path):
    base, ext = os.path.splitext(path)
    n, out = 2, path
    while os.path.exists(out):
        out = "%s %d%s" % (base, n, ext)
        n += 1
    return out


def post_file(url, path, filename):
    """Отправить файл на вебхук Discord (multipart/form-data)."""
    boundary = "----MidnightClips" + uuid.uuid4().hex
    with open(path, "rb") as f:
        data = f.read()
    safe = filename.replace('"', "'").replace("\r", " ").replace("\n", " ")
    body = b"".join([
        ("--%s\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
         "Content-Type: application/json\r\n\r\n%s\r\n" % (boundary, json.dumps({"attachments": [{"id": 0, "filename": safe}]}))).encode("utf-8"),
        ("--%s\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"%s\"\r\n"
         "Content-Type: video/mp4\r\n\r\n" % (boundary, safe)).encode("utf-8"),
        data,
        ("\r\n--%s--\r\n" % boundary).encode("utf-8"),
    ])
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "multipart/form-data; boundary=" + boundary,
        "User-Agent": "%s (https://github.com/SevereClaw/MidnightClips, %s)" % (APP_NAME, __version__),
    })
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            if not 200 <= response.status < 300:
                raise Fail("Discord ответил кодом %d." % response.status)
    except urllib.error.HTTPError as exc:
        reasons = {
            401: "Discord не принял вебхук — проверьте ссылку в настройках.",
            403: "Discord не принял вебхук — проверьте ссылку в настройках.",
            404: "Такого вебхука нет — возможно, его удалили. Проверьте ссылку в настройках.",
            413: "Файл больше, чем разрешает этот сервер. Уменьшите лимит в настройках.",
            429: "Discord просит подождать — слишком много отправок подряд.",
        }
        raise Fail(reasons.get(exc.code, "Discord ответил ошибкой %d." % exc.code))
    except (urllib.error.URLError, OSError) as exc:
        raise Fail("Нет связи с Discord: %s" % getattr(exc, "reason", exc))


class ClipTools:
    def __init__(self, cfg, ffmpeg, tmp_dir, on_event):
        self.cfg = cfg
        self.ffmpeg = ffmpeg
        self.tmp_dir = tmp_dir
        self.on_event = on_event      # "job" {stage, percent} / "job_done" {ok, text, action, path}
        self.busy = False
        self._cancel = False
        self._proc = None
        os.makedirs(tmp_dir, exist_ok=True)

    def start(self, path, action, start=None, end=None):
        if self.busy or action not in ("trim", "compress", "send"):
            return False
        self.busy = True
        self._cancel = False
        threading.Thread(target=self._run, args=(path, action, start, end), daemon=True, name="clip-tools").start()
        return True

    def cancel(self):
        self._cancel = True
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    # ---------------------------------------------------------------- работа

    def _stage(self, text, percent=None):
        self.on_event("job", {"stage": text, "percent": percent})

    def _run(self, path, action, start, end):
        temps = []
        try:
            if not self.ffmpeg:
                raise Fail("Не найден ffmpeg.")
            duration, fps, _height = probe(self.ffmpeg, path)
            s = max(0.0, float(start or 0))
            e = min(duration, float(end) if end else duration)
            if e - s < 0.3:
                raise Fail("Отрезок слишком короткий.")
            ranged = s > 0.05 or e < duration - 0.05
            base = os.path.splitext(path)[0]
            limit = max(1, int(self.cfg["discord_limit_mb"]))
            limit_bytes = limit * 1024 * 1024

            if action == "trim":
                if not ranged:
                    raise Fail("Сначала выберите отрезок ползунками под видео.")
                out = unique(base + " (обрезка).mp4")
                self._trim(path, out, s, e)
                self._done(action, "Обрезка сохранена", out)
            elif action == "compress":
                out = unique("%s (%d МБ).mp4" % (base, limit))
                temps.append(out)
                self._compress(path, out, limit, s, e, fps)
                temps.remove(out)
                self._done(action, "Сжатый клип сохранён: %.1f МБ" % (os.path.getsize(out) / 1048576), out)
            else:
                url = self.cfg["discord_webhook"].strip()
                if not WEBHOOK_RE.match(url):
                    raise Fail("Сначала вставьте ссылку вебхука: Настройки → Discord.")
                source = path
                if ranged:
                    source = os.path.join(self.tmp_dir, "send_trim.mp4")
                    temps.append(source)
                    self._trim(path, source, s, e)
                if os.path.getsize(source) > limit_bytes:
                    source = os.path.join(self.tmp_dir, "send_small.mp4")
                    temps.append(source)
                    self._compress(path, source, limit, s, e, fps)
                self._stage("Отправляю в Discord…")
                if self._cancel:
                    raise Fail("Отменено.")
                post_file(url, source, os.path.basename(base) + ".mp4")
                self._done(action, "Клип отправлен в Discord", "")
        except Fail as exc:
            self.on_event("job_done", {"ok": False, "action": action,
                                       "text": "Отменено." if self._cancel else str(exc), "path": ""})
        except Exception as exc:  # не оставлять интерфейс в «занято»
            self.on_event("job_done", {"ok": False, "action": action, "text": "Ошибка: %s" % exc, "path": ""})
        finally:
            for temp in temps:
                try:
                    os.remove(temp)
                except OSError:
                    pass
            self.busy = False
            self._proc = None

    def _done(self, action, text, path):
        self.on_event("job_done", {"ok": True, "action": action, "text": text, "path": path})

    def _ffmpeg(self, args, duration=None, base=0, span=100, label=""):
        """Запустить ffmpeg и передавать ход работы в интерфейс."""
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostats", "-y",
               "-progress", "pipe:1"] + args
        err_path = os.path.join(self.tmp_dir, "tools_err.log")
        with open(err_path, "w", encoding="utf-8", errors="replace") as err:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err,
                                    creationflags=NO_WINDOW, cwd=self.tmp_dir)
            self._proc = proc
            last = -1
            for raw in proc.stdout:
                line = raw.decode("ascii", "replace").strip()
                if duration and (line.startswith("out_time_us=") or line.startswith("out_time_ms=")):
                    try:
                        done = int(line.split("=", 1)[1]) / 1e6
                    except ValueError:
                        continue
                    percent = int(base + span * max(0.0, min(1.0, done / duration)))
                    if percent != last:
                        last = percent
                        self._stage(label, percent)
            proc.wait()
        if self._cancel:
            raise Fail("Отменено.")
        if proc.returncode != 0:
            try:
                detail = open(err_path, encoding="utf-8", errors="replace").read().strip().splitlines()[-1]
            except (OSError, IndexError):
                detail = ""
            raise Fail("ffmpeg не справился. " + detail[:200])

    def _trim(self, src, out, s, e):
        """Быстрая обрезка без перекодирования (начало встаёт на ближайший опорный кадр, до секунды раньше)."""
        self._stage("Обрезаю…")
        self._ffmpeg(["-ss", "%.3f" % s, "-i", src, "-t", "%.3f" % (e - s), "-c", "copy",
                      "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", out])
        if not os.path.isfile(out) or os.path.getsize(out) == 0:
            raise Fail("Обрезка не получилась.")

    def _compress(self, src, out, limit_mb, s, e, src_fps):
        duration = e - s
        video, audio, height, fps = plan(limit_mb, duration)
        limit_bytes = limit_mb * 1024 * 1024
        passlog = os.path.join(self.tmp_dir, "pass")
        for attempt in range(3):
            filters = "scale=-2:'trunc(min(ih,%d)/2)*2'" % height
            if src_fps > fps + 1:
                filters += ",fps=%d" % fps
            common = ["-ss", "%.3f" % s, "-i", src, "-t", "%.3f" % duration, "-vf", filters,
                      "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                      "-b:v", "%dk" % video, "-passlogfile", passlog]
            label = "Сжимаю до %d МБ…" % limit_mb
            self._ffmpeg(common + ["-pass", "1", "-an", "-f", "null", "-"], duration, 0, 45, label)
            self._ffmpeg(common + ["-pass", "2", "-c:a", "aac", "-b:a", "%dk" % audio, "-ac", "2",
                                   "-movflags", "+faststart", out], duration, 45, 55, label)
            size = os.path.getsize(out)
            if size <= limit_bytes:
                break
            video = int(video * limit_bytes / size * 0.94)   # не уложились — ещё раз, чуть скромнее
            if video < 80:
                raise Fail("В %d МБ столько не уместить — обрежьте клип покороче." % limit_mb)
        else:
            raise Fail("Не удалось уложиться в %d МБ — обрежьте клип покороче." % limit_mb)
        for name in os.listdir(self.tmp_dir):
            if name.startswith("pass"):
                try:
                    os.remove(os.path.join(self.tmp_dir, name))
                except OSError:
                    pass
