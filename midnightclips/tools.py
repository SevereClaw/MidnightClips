"""Обрезка, сжатие под лимит Discord и отправка клипа по вебхуку."""
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
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


def video_codec(ffmpeg, path):
    """Формат видео в файле: h264, hevc... (пусто — не удалось узнать)."""
    try:
        text = subprocess.run([ffmpeg, "-hide_banner", "-i", path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, timeout=30, creationflags=NO_WINDOW).stderr.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return ""
    found = re.search(r"Video: (\w+)", text)
    return found.group(1).lower() if found else ""


def audio_tracks(ffmpeg, path):
    """Названия звуковых дорожек файла по порядку. У клипа с отдельными дорожками первая — «всё вместе»."""
    try:
        text = subprocess.run([ffmpeg, "-hide_banner", "-i", path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, timeout=30, creationflags=NO_WINDOW).stderr.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return []
    names, current = [], None
    for line in text.splitlines():
        if re.match(r"\s*Stream #0:\d+", line):
            current = None
            if "Audio:" in line:
                names.append("")
                current = len(names) - 1
        elif current is not None and not names[current]:
            found = re.match(r"\s*(?:handler_name|title)\s*:\s*(.+)", line)
            if found and found.group(1).strip() not in ("SoundHandler", ""):
                names[current] = found.group(1).strip()
    return [name or "Дорожка %d" % (i + 1) for i, name in enumerate(names)]


def audio_choice(tracks, total):
    """Какой звук положить в готовый файл. tracks — номера выбранных отдельных дорожек (1..total-1) или None.
    Возвращает (доп. параметры ffmpeg перед выходным файлом, копируется ли звук без перекодирования)."""
    if tracks is None or total < 2:
        return ["-map", "0:v", "-map", "0:a?"], True
    chosen = sorted({int(t) for t in tracks if 1 <= int(t) < total})
    if not chosen:
        return ["-map", "0:v", "-an"], True
    if len(chosen) == total - 1:
        return ["-map", "0:v", "-map", "0:a:0"], True          # выбрано всё — это и есть общая дорожка
    if len(chosen) == 1:
        return ["-map", "0:v", "-map", "0:a:%d" % chosen[0]], True
    mix = "".join("[0:a:%d]" % i for i in chosen) + "amix=inputs=%d:normalize=0[mcaudio]" % len(chosen)
    return ["-filter_complex", mix, "-map", "0:v", "-map", "[mcaudio]"], False


def split_tracks(ffmpeg, path, cache_dir):
    """Отдельные звуковые дорожки клипа — файлами, чтобы плеер в окне клипа мог играть только выбранные.
    Возвращает пути для дорожек 1..N-1 (нулевая — «всё вместе») или [] — если дорожка одна или не получилось."""
    total = len(audio_tracks(ffmpeg, path))
    if total < 2:
        return []
    stat = os.stat(path)
    key = hashlib.sha1(("%s|%d|%d" % (path, stat.st_mtime_ns, stat.st_size)).encode("utf-8")).hexdigest()[:16]
    os.makedirs(cache_dir, exist_ok=True)
    files = [os.path.join(cache_dir, "%s_%d.m4a" % (key, n)) for n in range(1, total)]
    if all(os.path.isfile(f) and os.path.getsize(f) > 0 for f in files):
        return files
    now = time.time()
    for name in os.listdir(cache_dir):   # старые дорожки от прошлых клипов не копим
        old = os.path.join(cache_dir, name)
        try:
            if name.endswith(".m4a") and now - os.path.getmtime(old) > 6 * 3600:
                os.remove(old)
        except OSError:
            pass
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", path]
    for n, out in enumerate(files, 1):
        args += ["-map", "0:a:%d" % n, "-c", "copy", "-movflags", "+faststart", "-f", "mp4", out + ".part"]
    try:
        done = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=120, creationflags=NO_WINDOW)
        if done.returncode != 0:
            return []
        for out in files:
            os.replace(out + ".part", out)
    except (OSError, subprocess.SubprocessError):
        return []
    return files


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
    def __init__(self, cfg, ffmpeg, tmp_dir, on_event, log_path=None):
        self.log_path = log_path
        self.cfg = cfg
        self.ffmpeg = ffmpeg
        self.tmp_dir = tmp_dir
        self.on_event = on_event      # "job" {stage, percent} / "job_done" {ok, text, action, path}
        self.busy = False
        self._cancel = False
        self._proc = None
        self._audio, self._audio_copy = ["-map", "0:v", "-map", "0:a?"], True
        os.makedirs(tmp_dir, exist_ok=True)

    def start(self, path, action, start=None, end=None, tracks=None):
        """tracks — номера отдельных звуковых дорожек, которые оставить (None — звук как есть)."""
        if self.busy or action not in ("trim", "compress", "send"):
            return False
        self.busy = True
        self._cancel = False
        threading.Thread(target=self._run, args=(path, action, start, end, tracks), daemon=True, name="clip-tools").start()
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

    def _run(self, path, action, start, end, tracks=None):
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
            total = len(audio_tracks(self.ffmpeg, path)) if tracks is not None else 0
            self._audio, self._audio_copy = audio_choice(tracks, total)
            picked = tracks is not None and total >= 2 and len({int(t) for t in tracks if 1 <= int(t) < total}) < total - 1
            base = os.path.splitext(path)[0]
            limit = max(1, int(self.cfg["discord_limit_mb"]))
            limit_bytes = limit * 1024 * 1024

            if action == "trim":
                if not ranged and not picked:
                    raise Fail("Сначала выберите отрезок ползунками под видео или снимите галочки с дорожек.")
                out = unique(base + (" (обрезка).mp4" if ranged else " (дорожки).mp4"))
                self._trim(path, out, s, e)
                self._done(action, "Обрезка сохранена" if ranged else "Клип с выбранными дорожками сохранён", out)
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
                source, kept = path, ""
                if ranged or picked:
                    source = os.path.join(self.tmp_dir, "send_trim.mp4")
                    temps.append(source)
                    self._trim(path, source, s, e)
                # HEVC в чате Discord не проигрывается — такой клип отправляем перекодированным в H.264
                if os.path.getsize(source) > limit_bytes or video_codec(self.ffmpeg, path) == "hevc":
                    # Сжатый клип остаётся рядом с исходным: если Discord не примет, его можно отправить самому.
                    ready = "%s (%d МБ).mp4" % (base, limit)
                    if not ranged and not picked and self._fresh(ready, path, limit_bytes):
                        source = kept = ready          # уже сжимали целиком — второй раз не нужно
                    else:
                        source = unique(ready)
                        temps.append(source)
                        self._compress(path, source, limit, s, e, fps)
                        temps.remove(source)
                        kept = source
                self._stage("Отправляю в Discord…")
                try:
                    if self._cancel:
                        raise Fail("Отменено.")
                    post_file(url, source, os.path.basename(base) + ".mp4")
                except Fail as exc:
                    if not kept:
                        raise
                    self.on_event("job_done", {
                        "ok": False, "action": action, "path": kept,
                        "text": "%s Сжатый клип сохранён рядом с исходным (%s) — его можно отправить самому." % (
                            "Отправка отменена." if self._cancel else str(exc), os.path.basename(kept))})
                    return
                self._done(action, "Клип отправлен в Discord" + (". Сжатая копия сохранена рядом" if kept else ""), kept)
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
            try:   # полный ответ ffmpeg — в общий журнал, чтобы причину было видно
                if self.log_path:
                    with open(self.log_path, "a", encoding="utf-8", errors="replace") as log:
                        log.write("\n*** работа с клипом не удалась\n%s\n%s\n" % (
                            " ".join(cmd)[:1500], open(err_path, encoding="utf-8", errors="replace").read()[-2500:]))
            except OSError:
                pass
            raise Fail("ffmpeg не справился. " + detail[:200])

    def _trim(self, src, out, s, e):
        """Быстрая обрезка без перекодирования (начало встаёт на ближайший опорный кадр, до секунды раньше)."""
        self._stage("Обрезаю…")
        codecs = ["-c", "copy"] if self._audio_copy else ["-c:v", "copy", "-c:a", "aac", "-b:a", "160k"]
        self._ffmpeg(["-ss", "%.3f" % s, "-i", src, "-t", "%.3f" % (e - s)] + self._audio + codecs + [
                      "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", out])
        if not os.path.isfile(out) or os.path.getsize(out) == 0:
            raise Fail("Обрезка не получилась.")

    def _fresh(self, ready, src, limit_bytes):
        """Готовый сжатый клип годится для отправки: не старее исходника, в пределах лимита, H.264
        и той же длины (под этим именем мог сохраниться и сжатый отрезок)."""
        try:
            return (os.path.isfile(ready) and 0 < os.path.getsize(ready) <= limit_bytes
                    and os.path.getmtime(ready) >= os.path.getmtime(src)
                    and video_codec(self.ffmpeg, ready) == "h264"
                    and abs(probe(self.ffmpeg, ready)[0] - probe(self.ffmpeg, src)[0]) < 0.5)
        except (OSError, Fail):
            return False

    def _compress(self, src, out, limit_mb, s, e, src_fps):
        duration = e - s
        video, audio, height, fps = plan(limit_mb, duration)
        limit_bytes = limit_mb * 1024 * 1024
        two_pass = True
        for attempt in range(4):
            filters = "scale=-2:'trunc(min(ih,%d)/2)*2'" % height
            if src_fps > fps + 1:
                filters += ",fps=%d" % fps
            sound = [] if self._audio == ["-map", "0:v", "-map", "0:a?"] else self._audio   # по умолчанию — первая дорожка
            # Журнал проходов — короткое имя в рабочей папке (ffmpeg запускается в ней): путь к папке
            # с нелатинскими буквами в имени пользователя кодировщику тогда не нужен.
            common = ["-ss", "%.3f" % s, "-i", src, "-t", "%.3f" % duration, "-vf", filters,
                      "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-b:v", "%dk" % video]
            tail = ["-c:a", "aac", "-b:a", "%dk" % audio, "-ac", "2", "-movflags", "+faststart", out]
            label = "Сжимаю до %d МБ…" % limit_mb
            if two_pass:
                try:
                    self._ffmpeg(common + ["-passlogfile", "pass", "-pass:v", "1", "-an", "-f", "null", "-"],
                                 duration, 0, 45, label)
                    self._ffmpeg(common + sound + ["-passlogfile", "pass", "-pass:v", "2"] + tail, duration, 45, 55, label)
                except Fail:
                    if self._cancel:
                        raise
                    two_pass = False   # два прохода не получились — сжимаем в один, без журнала проходов
            if not two_pass:
                self._ffmpeg(common + sound + ["-maxrate", "%dk" % int(video * 1.3), "-bufsize", "%dk" % (video * 2)] + tail,
                             duration, 0, 100, label)
            if not os.path.isfile(out) or os.path.getsize(out) == 0:
                raise Fail("Сжатие не получилось — подробности в Настройки → Журнал.")
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
        return True
