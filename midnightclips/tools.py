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


def media_info(ffmpeg, path):
    """Всё, что нужно для склейки: длительность, частота кадров, размер кадра и число звуковых дорожек."""
    try:
        result = subprocess.run([ffmpeg, "-hide_banner", "-i", path], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30,
                                creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        raise Fail("Не удалось прочитать клип %s." % os.path.basename(path))
    text = result.stderr.decode("utf-8", "replace")
    found = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", text)
    video = re.search(r"Stream #\d+:\d+.*?Video:.*", text)
    if not found or not video:
        raise Fail("Не удалось прочитать клип %s." % os.path.basename(path))
    line = video.group(0)
    size = re.search(r"\b(\d{2,5})x(\d{2,5})\b", line)
    fps = re.search(r"(\d+(?:\.\d+)?) fps", line)
    return {
        "duration": int(found.group(1)) * 3600 + int(found.group(2)) * 60 + float(found.group(3)),
        "fps": float(fps.group(1)) if fps else 30.0,
        "w": int(size.group(1)) if size else 1920,
        "h": int(size.group(2)) if size else 1080,
        "audio": len(re.findall(r"Stream #\d+:\d+.*?Audio:", text)),
    }


def film_strip(ffmpeg, path, cache_dir, count=24, height=56):
    """Полоса кадров для шкалы в окне клипа: count кадров подряд одной картинкой (data-URL JPEG).
    Берутся только опорные кадры — даже часовая запись разбирается за пару секунд."""
    import base64
    try:
        stat = os.stat(path)
    except OSError:
        return None
    key = hashlib.sha1(("%s|%d|%d|%d|%d" % (path, stat.st_mtime_ns, stat.st_size, count, height)).encode("utf-8")).hexdigest()[:20]
    os.makedirs(cache_dir, exist_ok=True)
    jpg = os.path.join(cache_dir, key + ".jpg")
    if not os.path.isfile(jpg):
        try:
            duration = media_info(ffmpeg, path)["duration"]
        except Fail:
            return None
        rate = max(0.001, count / max(0.5, duration))
        args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-skip_frame", "nokey", "-i", path, "-an",
                "-vf", "fps=%.5f,scale=-2:%d,tile=%dx1" % (rate, height, count), "-frames:v", "1", "-q:v", "6", jpg + ".tmp.jpg"]
        try:
            subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=90, creationflags=NO_WINDOW)
            os.replace(jpg + ".tmp.jpg", jpg)
        except (OSError, subprocess.SubprocessError):
            return None
        now = time.time()
        for name in os.listdir(cache_dir):   # старые полосы не копим
            old = os.path.join(cache_dir, name)
            try:
                if now - os.path.getmtime(old) > 7 * 86400:
                    os.remove(old)
            except OSError:
                pass
    try:
        with open(jpg, "rb") as f:
            return {"image": "data:image/jpeg;base64," + base64.b64encode(f.read()).decode("ascii"), "count": count}
    except OSError:
        return None


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


def gate_filter(spans, offset=0.0, pad=0.12):
    """Фильтр ffmpeg, глушащий звук в моменты spans [(начало, конец)] — секунды от начала клипа.
    offset — с какой секунды клипа начинается обрезка (время в фильтре идёт от неё). Пусто — глушить нечего."""
    merged = []
    for a, b in sorted((float(a) - pad, float(b) + pad) for a, b in spans or []):
        if merged and a <= merged[-1][1] + 0.3:   # фразы почти подряд — одним куском, выражение короче
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    parts = ["between(t,%.3f,%.3f)" % (max(0.0, a - offset), b - offset) for a, b in merged if b - offset > 0]
    return "volume=0:enable='%s'" % "+".join(parts[:400]) if parts else ""


CLEAN_VOICE = "Голос без эффекта"   # дорожка-запаска: голос до Steal Your Voice, в «Всё вместе» не входит


def extra_tracks(names):
    """Номера дорожек, которых нет в общей («Всё вместе»): чистый голос без эффекта."""
    return {i for i, name in enumerate(names) if i > 0 and name == CLEAN_VOICE}


def audio_choice(tracks, total, gate=None, offset=0.0, extra=()):
    """Какой звук положить в готовый файл. tracks — номера выбранных отдельных дорожек (1..total-1) или None.
    gate — {номер дорожки: [(начало, конец)]}: в эти моменты дорожка молчит (друг из Discord выключен).
    extra — дорожки, которых нет в общей (чистый голос): «выбрано всё» — это всё, кроме них.
    Возвращает (доп. параметры ffmpeg перед выходным файлом, копируется ли звук без перекодирования)."""
    full = [t for t in range(1, total) if t not in extra]
    if gate and total >= 2:
        chosen = full if tracks is None else sorted({int(t) for t in tracks if 1 <= int(t) < total})
        filters = {t: gate_filter(gate.get(t) or gate.get(str(t)), offset) for t in chosen}
        if chosen and any(filters.values()):
            # общий звук собирается заново из отдельных дорожек — в нём Discord уже приглушён где нужно
            chains = ["[0:a:%d]%s[mcg%d]" % (t, filters[t] or "anull", t) for t in chosen]
            mix = "".join("[mcg%d]" % t for t in chosen) + (
                "amix=inputs=%d:normalize=0[mcaudio]" % len(chosen) if len(chosen) > 1 else "anull[mcaudio]")
            return ["-filter_complex", ";".join(chains) + ";" + mix, "-map", "0:v", "-map", "[mcaudio]"], False
    if tracks is None or total < 2:
        return ["-map", "0:v", "-map", "0:a?"], True
    chosen = sorted({int(t) for t in tracks if 1 <= int(t) < total})
    if not chosen:
        return ["-map", "0:v", "-an"], True
    if chosen == full:
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


def plural(n, one, few, many):
    n = abs(int(n)) % 100
    if 11 <= n <= 19:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many


def clock(seconds):
    seconds = int(round(seconds))
    return "%d:%02d:%02d" % (seconds // 3600, seconds // 60 % 60, seconds % 60) if seconds >= 3600 else "%d:%02d" % (seconds // 60, seconds % 60)


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


def _setting(cfg, key):
    try:
        return str(cfg[key] or "")
    except (KeyError, TypeError):
        return ""


def channels(cfg):
    """Каналы Discord из настроек: [{"name", "url"}]. Старая одиночная ссылка — как единственный канал."""
    out, seen = [], set()
    try:
        data = json.loads(_setting(cfg, "discord_channels") or "[]")
    except ValueError:
        data = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({"name": str(item.get("name") or "").strip() or "Канал %d" % (len(out) + 1), "url": url})
    legacy = _setting(cfg, "discord_webhook").strip()
    if not out and legacy:
        out.append({"name": "Канал", "url": legacy})
    return out


def pick_channel(cfg, url=None):
    """Канал для отправки: названный, иначе последний выбранный, иначе первый. None — каналов нет."""
    found = channels(cfg)
    for want in (url, _setting(cfg, "discord_channel")):
        for ch in found:
            if want and ch["url"] == want:
                return ch
    return found[0] if found else None


def webhook_name(url, timeout=8):
    """Имя вебхука из Discord (по ссылке, без входа в аккаунт) — подсказка для названия канала."""
    if not WEBHOOK_RE.match(url or ""):
        return ""
    request = urllib.request.Request(url.strip(), headers={
        "User-Agent": "%s (https://github.com/SevereClaw/MidnightClips, %s)" % (APP_NAME, __version__)})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return str(json.loads(response.read().decode("utf-8", "replace")).get("name") or "")
    except (urllib.error.URLError, OSError, ValueError, AttributeError):
        return ""


class ClipTools:
    def __init__(self, cfg, ffmpeg, tmp_dir, on_event, log_path=None):
        self.log_path = log_path
        self.cfg = cfg
        self.ffmpeg = ffmpeg
        self.tmp_dir = tmp_dir
        # "job" {id, action, name, channel, stage, percent} / "job_done" {id, action, name, channel, ok, text, path}
        self.on_event = on_event
        self._cancel = False
        self._proc = None
        # Работы идут по очереди: отправка в Discord не занимает окно клипа — можно сразу браться за следующий.
        self._lock = threading.Lock()
        self._queue = []              # ждущие работы: {"id", "action", "name", "channel", "run": (функция, аргументы)}
        self._current = None          # работа, которая идёт сейчас
        self._next_id = 0
        self._job = None
        self._audio, self._audio_copy = ["-map", "0:v", "-map", "0:a?"], True
        self.encoders = lambda: []    # кодировщики, найденные при записи (видеокарта) — задаёт программа
        os.makedirs(tmp_dir, exist_ok=True)

    @property
    def busy(self):
        return self._current is not None or bool(self._queue)

    def jobs(self):
        """Идущая и ждущие работы — для панели отправок после перезагрузки окна."""
        with self._lock:
            items = ([self._current] if self._current else []) + list(self._queue)
            return [{k: job[k] for k in ("id", "action", "name", "channel")} for job in items]

    def start(self, path, action, start=None, end=None, tracks=None, segments=None, gate=None, channel=None):
        """tracks — номера отдельных звуковых дорожек, которые оставить (None — звук как есть).
        segments — несколько оставленных кусков [[начало, конец], ...]: они склеиваются по порядку.
        channel — ссылка вебхука канала для отправки (None — последний выбранный или первый).
        Возвращает номер работы (она встаёт в очередь) или 0."""
        if action not in ("trim", "compress", "send"):
            return 0
        name = os.path.splitext(os.path.basename(path))[0]
        url = label = ""
        if action == "send":
            ch = pick_channel(self.cfg, channel)
            if ch:
                url, label = ch["url"], ch["name"]
        return self._enqueue(action, name, label, self._run, (path, action, start, end, tracks, segments, gate, url, label))

    def montage(self, pieces, out_path):
        """Склеить куски разных клипов в один ролик: pieces = [{"path", "start", "end", "tracks"}, ...]."""
        if not pieces:
            return 0
        return self._enqueue("montage", os.path.splitext(os.path.basename(out_path))[0], "",
                             self._run_montage, (pieces, out_path))

    def _enqueue(self, action, name, channel, fn, args):
        with self._lock:
            self._next_id += 1
            job = {"id": self._next_id, "action": action, "name": name, "channel": channel, "run": (fn, args)}
            waiting = self._current is not None or bool(self._queue)
            self._queue.append(job)
            if self._current is None:
                self._current = self._queue.pop(0)
                threading.Thread(target=self._worker, daemon=True, name="clip-tools").start()
        if waiting:
            self._emit(job, "job", {"stage": "В очереди…", "percent": None})
        return job["id"]

    def _worker(self):
        while True:
            job = self._current
            self._cancel = False
            self._job = job
            fn, args = job["run"]
            try:
                fn(*args)
            except Exception as exc:  # не оставлять интерфейс в «занято»
                self._emit(job, "job_done", {"ok": False, "text": "Ошибка: %s" % exc, "path": ""})
            self._proc = None
            with self._lock:
                self._current = self._queue.pop(0) if self._queue else None
                if self._current is None:
                    return

    def _emit(self, job, kind, payload):
        data = {"id": job["id"], "action": job["action"], "name": job["name"], "channel": job["channel"]}
        data.update(payload)
        self.on_event(kind, data)

    def _run_montage(self, pieces, out_path):
        try:
            total = self._join(pieces, out_path, "Склеиваю…")
            self._done("montage", "Ролик склеен: %d %s, %s" % (len(pieces), plural(len(pieces), "кусок", "куска", "кусков"),
                                                             clock(total)), out_path)
        except Fail as exc:
            self._emit(self._job, "job_done", {"ok": False,
                                                 "text": "Отменено." if self._cancel else str(exc), "path": ""})
        except Exception as exc:  # не оставлять интерфейс в «занято»
            self._emit(self._job, "job_done", {"ok": False, "text": "Ошибка: %s" % exc, "path": ""})
        finally:
            self._proc = None

    def _join(self, pieces, out_path, label):
        """Склейка с перекодированием: каждый кусок режется точно по кадру, приводится к размеру и частоте
        первого куска (другое разрешение — с чёрными полями), звук — к 48 кГц стерео. Кодирует видеокарта,
        если она есть; не вышло — процессор."""
        if len(pieces) > 80:
            raise Fail("Слишком много кусков — больше 80 за раз не склеить.")
        infos = {}
        for p in pieces:
            if p["path"] not in infos:
                infos[p["path"]] = media_info(self.ffmpeg, p["path"])
        first = infos[pieces[0]["path"]]
        w, h = first["w"] & ~1, first["h"] & ~1
        fps = max(10, min(120, int(round(first["fps"])) or 60))
        args, chains, joins, total = [], [], "", 0.0
        for i, p in enumerate(pieces):
            info = infos[p["path"]]
            a = max(0.0, float(p["start"]))
            end = float(p["end"])
            b = info["duration"] if end <= 0 else min(info["duration"], end)   # 0 — до конца клипа
            if b - a < 0.1:
                raise Fail("Кусок из %s слишком короткий." % os.path.basename(p["path"]))
            dur = b - a
            total += dur
            args += ["-ss", "%.3f" % a, "-t", "%.3f" % dur, "-i", p["path"]]
            chains.append("[%d:v:0]scale=%d:%d:force_original_aspect_ratio=decrease,pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black,"
                          "setsar=1,fps=%d,format=yuv420p,setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration=1,"
                          "trim=duration=%.3f[v%d]" % (i, w, h, w, h, fps, dur, i))
            n = info["audio"]
            extra = extra_tracks(audio_tracks(self.ffmpeg, p["path"])) if n >= 2 else set()
            full = [t for t in range(1, n) if t not in extra]
            tracks = p.get("tracks")
            chosen = sorted({int(t) for t in tracks if 1 <= int(t) < n}) if tracks is not None and n >= 2 else None
            gate = p.get("gate") or {}
            gates = {}
            if gate and n >= 2:   # друзья из Discord выключены: собираем звук из отдельных дорожек, Discord приглушён
                every = chosen if chosen is not None else full
                gates = {t: gate_filter(gate.get(t) or gate.get(str(t)), a) for t in every}
                if any(gates.values()):
                    chosen = every
                else:
                    gates = {}
            fix = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,apad,atrim=duration=%.3f,asetpts=PTS-STARTPTS" % dur
            if n == 0 or (chosen is not None and not chosen):
                chains.append("anullsrc=r=48000:cl=stereo,atrim=duration=%.3f[a%d]" % (dur, i))
            elif gates:
                for t in chosen:
                    chains.append("[%d:a:%d]%s[g%d_%d]" % (i, t, gates.get(t) or "anull", i, t))
                mix = "".join("[g%d_%d]" % (i, t) for t in chosen)
                chains.append("%samix=inputs=%d:normalize=0,%s[a%d]" % (mix, len(chosen), fix, i) if len(chosen) > 1
                              else "%sanull,%s[a%d]" % (mix, fix, i))
            elif chosen is None or chosen == full:
                chains.append("[%d:a:0]%s[a%d]" % (i, fix, i))
            elif len(chosen) == 1:
                chains.append("[%d:a:%d]%s[a%d]" % (i, chosen[0], fix, i))
            else:
                mix = "".join("[%d:a:%d]" % (i, t) for t in chosen)
                chains.append("%samix=inputs=%d:normalize=0,%s[a%d]" % (mix, len(chosen), fix, i))
            joins += "[v%d][a%d]" % (i, i)
        graph = ";".join(chains) + ";%sconcat=n=%d:v=1:a=1[v][a]" % (joins, len(pieces))
        mbps = max(6, min(80, int(self.cfg["bitrate_mbps"])))
        part = out_path + ".part"
        last = None
        for enc in self._join_encoders():
            if enc == "libx264":
                video = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19"]
            elif enc.endswith("nvenc"):
                video = ["-c:v", enc, "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "%dM" % mbps, "-maxrate", "%dM" % (mbps * 2)]
            elif enc.endswith("amf"):
                video = ["-c:v", enc, "-quality", "quality", "-rc", "vbr_peak", "-b:v", "%dM" % mbps, "-maxrate", "%dM" % (mbps * 2)]
            else:
                video = ["-c:v", enc, "-preset", "medium", "-b:v", "%dM" % mbps, "-maxrate", "%dM" % (mbps * 2)]
            cmd = args + ["-filter_complex", graph, "-map", "[v]", "-map", "[a]"] + video + [
                "-g", str(fps * 2), "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-f", "mp4", part]
            try:
                self._ffmpeg(cmd, total, 0, 100, label)
                if os.path.isfile(part) and os.path.getsize(part) > 0:
                    os.replace(part, out_path)
                    return total
                last = Fail("Склейка не получилась.")
            except Fail as exc:
                last = exc
                if self._cancel:
                    break
            finally:
                try:
                    os.remove(part)
                except OSError:
                    pass
        raise last or Fail("Склейка не получилась.")

    def _join_encoders(self):
        """Кодировщики для склейки: сначала видеокарта (быстро), последним — процессор (работает всегда)."""
        try:
            found = [e for e in (self.encoders() or []) if e.startswith("h264_")]
        except Exception:
            found = []
        return found[:1] + ["libx264"]

    def cancel(self, job_id=None):
        """Отменить работу: идущую (job_id не указан или совпадает) или ждущую в очереди."""
        with self._lock:
            job = next((j for j in self._queue if j["id"] == job_id), None)
            if job is not None:
                self._queue.remove(job)
            current = self._current
        if job is not None:
            self._emit(job, "job_done", {"ok": False, "text": "Отменено.", "path": ""})
            return True
        if current is None or (job_id is not None and current["id"] != job_id):
            return False
        self._cancel = True
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        return True

    # ---------------------------------------------------------------- работа

    def _stage(self, text, percent=None):
        self._emit(self._job, "job", {"stage": text, "percent": percent})

    def _run(self, path, action, start, end, tracks=None, segments=None, gate=None, url="", channel=""):
        temps = []
        send_base = None
        try:
            if not self.ffmpeg:
                raise Fail("Не найден ffmpeg.")
            if segments and len(segments) > 1:
                # Несколько кусков одного клипа: сначала склеиваем (с выбранными дорожками), дальше — как с целым клипом.
                pieces = [{"path": path, "start": float(a), "end": float(b), "tracks": tracks, "gate": gate} for a, b in segments]
                base = os.path.splitext(path)[0]
                if action == "trim":
                    out = unique(base + " (склейка).mp4")
                    self._join(pieces, out, "Склеиваю куски…")
                    self._done(action, "Склейка сохранена: %d %s" % (len(pieces), plural(len(pieces), "кусок", "куска", "кусков")), out)
                    return
                joined = os.path.join(self.tmp_dir, "joined.mp4")
                temps.append(joined)
                self._join(pieces, joined, "Склеиваю куски…")
                if action == "compress":   # сжатая склейка ложится рядом с исходным клипом
                    limit = max(1, int(self.cfg["discord_limit_mb"]))
                    out = unique("%s (склейка, %d МБ).mp4" % (base, limit))
                    temps.append(out)
                    duration, fps, _h = probe(self.ffmpeg, joined)
                    self._audio, self._audio_copy = ["-map", "0:v", "-map", "0:a?"], True
                    self._compress(joined, out, limit, 0.0, duration, fps)
                    temps.remove(out)
                    self._done(action, "Сжатая склейка сохранена: %.1f МБ" % (os.path.getsize(out) / 1048576), out)
                    return
                path, start, end, tracks = joined, None, None, None
                send_base = base + " (склейка)"   # сжатая копия и имя в Discord — от исходного клипа
            duration, fps, _height = probe(self.ffmpeg, path)
            s = max(0.0, float(start or 0))
            e = min(duration, float(end) if end else duration)
            if e - s < 0.3:
                raise Fail("Отрезок слишком короткий.")
            ranged = s > 0.05 or e < duration - 0.05
            names = audio_tracks(self.ffmpeg, path) if tracks is not None or gate else []
            total, extra = len(names), extra_tracks(names)
            self._audio, self._audio_copy = audio_choice(tracks, total, gate, s, extra)
            picked = (tracks is not None and total >= 2
                      and sorted({int(t) for t in tracks if 1 <= int(t) < total}) != [t for t in range(1, total) if t not in extra]) \
                or not self._audio_copy   # друзья из Discord приглушены — звук уже не тот, что в исходном клипе
            base = send_base or os.path.splitext(path)[0]
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
                url = (url or "").strip()
                if not WEBHOOK_RE.match(url):
                    raise Fail("Сначала добавьте канал: Настройки → Discord." if not url
                               else "Ссылка вебхука канала «%s» не похожа на настоящую — проверьте её в настройках." % channel)
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
                    self._emit(self._job, "job_done", {"ok": False, "path": kept,
                        "text": "%s Сжатый клип сохранён рядом с исходным (%s) — его можно отправить самому." % (
                            "Отправка отменена." if self._cancel else str(exc), os.path.basename(kept))})
                    return
                self._done(action, "Клип отправлен в «%s»" % channel + (". Сжатая копия сохранена рядом" if kept else ""), kept)
        except Fail as exc:
            self._emit(self._job, "job_done", {"ok": False,
                                                 "text": "Отменено." if self._cancel else str(exc), "path": ""})
        except Exception as exc:  # не оставлять интерфейс в «занято»
            self._emit(self._job, "job_done", {"ok": False, "text": "Ошибка: %s" % exc, "path": ""})
        finally:
            for temp in temps:
                try:
                    os.remove(temp)
                except OSError:
                    pass
            self._proc = None

    def _done(self, action, text, path):
        self._emit(self._job, "job_done", {"ok": True, "text": text, "path": path})

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
