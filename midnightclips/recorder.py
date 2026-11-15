"""Буфер повтора и запись на базе ffmpeg.

ffmpeg непрерывно пишет экран маленькими кусочками (.ts по 2 секунды) во
временную папку; старые кусочки удаляются. «Сохранить клип» склеивает
последние N секунд в mp4 без перекодирования — поэтому это мгновенно.
Обычная запись (старт/стоп) — те же кусочки, только без удаления.
"""
import glob
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime

from . import audio as audio_mod

SEGMENT = 2  # секунд в одном кусочке
MEMORY_LIMIT_MB = 768   # буфер больше этого в режиме «авто» держим на диске
CONCAT_LIMIT = 24000  # длиннее — склеиваем через общий файл
TEST = bool(os.environ.get("MC_TEST_SOURCE"))  # проверка без Windows
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
ABOVE_NORMAL = 0x00008000 if sys.platform == "win32" else 0   # приоритет процесса «выше среднего»

HW_ENCODERS = ["h264_nvenc", "h264_amf", "h264_qsv"]
# Тот же кодировщик в формате HEVC (H.265): файл заметно меньше при том же качестве.
HEVC_TWIN = {"h264_nvenc": "hevc_nvenc", "h264_amf": "hevc_amf", "h264_qsv": "hevc_qsv", "libx264": "libx265"}
ENCODER_TITLES = {
    "h264_nvenc": "NVIDIA NVENC",
    "h264_amf": "AMD AMF",
    "h264_qsv": "Intel Quick Sync",
    "libx264": "процессор (x264)",
    "hevc_nvenc": "NVIDIA NVENC, HEVC",
    "hevc_amf": "AMD AMF, HEVC",
    "hevc_qsv": "Intel Quick Sync, HEVC",
    "libx265": "процессор (x265), HEVC",
}
SOFTWARE = ("libx264", "libx265")


def vendor(encoder):
    """nvenc / amf / qsv / cpu — чей это кодировщик, независимо от формата."""
    return encoder.rsplit("_", 1)[-1] if "_" in encoder else "cpu"
ENGINE_KINDS = ("enc", "own", "owncpu")   # способы захвата через свой движок
CAPTURE_TITLES = {"enc": "движок MidnightClips, кодирование на видеокарте", "own": "движок MidnightClips", "owncpu": "движок MidnightClips (без обработки на видеокарте)", "dda": "Desktop Duplication", "ddal": "Desktop Duplication (старый режим)",
                  "gdi": "GDI", "test": "тест"}


def whole_frames(data):
    """Кусочек потока MPEG-TS, который ещё пишется: отрезать хвост по началу последнего видеокадра —
    тот может быть записан не целиком. Всё, что до него, — целые кадры."""
    i = len(data) - len(data) % 188 - 188
    while i >= 0:
        if data[i] == 0x47 and data[i + 1] & 0x40 and ((data[i + 1] & 0x1F) << 8 | data[i + 2]) == 0x100:
            return data[:i]
        i -= 188
    return data[:0]


def find_ffmpeg(resource_dir):
    name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    places = [resource_dir, os.path.join(resource_dir, "bin")]
    if getattr(sys, "frozen", False):
        places.insert(0, os.path.dirname(sys.executable))
    for place in places:
        path = os.path.join(place, name)
        if os.path.isfile(path):
            return path
    return shutil.which("ffmpeg")


def find_engine(resource_dir):
    """Свой движок захвата (mccap.exe). Его может не быть — тогда записываем через ffmpeg."""
    if sys.platform != "win32":
        return None
    places = [resource_dir]
    if getattr(sys, "frozen", False):
        places.insert(0, os.path.dirname(sys.executable))
    for place in places:
        path = os.path.join(place, "mccap.exe")
        if os.path.isfile(path):
            return path
    return None


def safe_name(text, fallback="Клип"):
    cleaned = "".join(c for c in text if c not in '<>:"/\\|?*' and ord(c) >= 32).strip(" .")
    return cleaned[:60] or fallback


class Recorder:
    def __init__(self, cfg, ffmpeg, work_dir, log_path, on_event, monitors=None, diag=None, engine=None,
                 find_window=None, detect_game=None):
        self.cfg = cfg
        self.ffmpeg = ffmpeg
        self.work_dir = work_dir
        self.log_path = log_path
        self.on_event = on_event          # on_event(kind, payload_dict)
        self.monitors = monitors or (lambda: [])

        self.state = "off"                # off / starting / on / error
        self.message = ""
        self.encoder = ""
        self.capture = ""
        self.audio_note = ""
        self.recording_since = None       # время начала обычной записи (epoch)

        self._thread = None
        self._stop = threading.Event()
        self._restart = threading.Event()
        self._save_lock = threading.Lock()
        self._proc = None
        self._session = 0
        self._rec_first = None            # имя первого кусочка обычной записи
        self._rec_title = ""
        self._encoders = None
        self._hevc = set()
        self._good = None                 # последний сработавший вариант захвата
        self._degrade = 0                 # 0 — всё, 1 — без микрофона, 2 — без звука
        self._mem = {}                    # буфер в памяти: имя кусочка -> байты
        self._mem_dur = {}                # сколько секунд в каждом кусочке из памяти
        self._mem_started = {}            # когда начат кусочек, который сейчас пишется в память
        self._partial = None              # кусочек, который ещё пишется: в клип идёт то, что уже готово
        self._partial_dur = 0.0
        self._mem_lock = threading.Lock()
        self._mem_epoch = 0               # растёт при полной очистке буфера
        self.in_memory = False            # где сейчас лежит буфер повтора
        self._soft = False                # мягкий перезапуск: буфер не стирать
        self.find_window = find_window or (lambda exe: None)   # окно программы по имени exe
        self.detect_game = detect_game or (lambda include, exclude: None)   # игра в активном окне: (exe, окно)
        self._track_names = ["Всё вместе"]  # звуковые дорожки текущей записи
        self._game_exe = None             # игра, найденная автоматически, пока её окно открыто
        self._game_skip = set()           # программы, чьё окно записать не получилось
        self.window_active = False        # сейчас записывается окно, а не весь монитор
        self._window_check = 0.0
        self.engine = engine              # путь к mccap.exe или None
        self.engine_test = bool(os.environ.get("MC_ENGINE_TEST"))   # тестовая картинка вместо экрана
        self.engine_trouble = False       # кодировщик не успевает за движком
        self.screen_fps = None            # сколько раз в секунду реально обновляется экран (от движка)
        self._format = None               # (кодировщик, размер, частота) прошлого запуска
        self._window_failed = False       # захват окна здесь не работает — пишем монитор
        self._diag = diag or (lambda: "")  # доп. сведения для журнала (состояние «метронома»)
        self._upgrades = 0                # сколько раз пробовали вернуться с запасного режима
        self._avoid_precise = False       # точный режим захвата здесь не работает
        self._stalls = []                 # когда запись «зависала» в точном режиме
        self._avoid_enc = False           # кодирование в движке здесь не работает
        self._enc_crashes = []            # когда оно падало
        self.fps_real = None              # сколько кадров в секунду реально успевает записываться
        self.fps_target = 0
        self.slow = False                 # захват не успевает за выбранной частотой

        os.makedirs(work_dir, exist_ok=True)
        for old in glob.glob(os.path.join(work_dir, "*.ts")):
            self._remove(old)

    # ------------------------------------------------------------ управление

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._restart.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="recorder")
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=15)
        self._set_state("off", "")

    def restart(self, soft=False):
        """Перезапустить захват с новыми настройками.

        soft=True — поменялось только то, что не влияет на формат видео (громкость в записи):
        буфер и идущая запись сохраняются, как при восстановлении после сбоя.
        """
        if self._thread and self._thread.is_alive():
            if soft and not self._restart.is_set():
                self._soft = True
            else:
                self._soft = False
                self._good = None
                self._upgrades = 0
                self._window_failed = False
            self._restart.set()
        else:
            self.start()

    def is_running(self):
        return bool(self._thread and self._thread.is_alive())

    def snapshot(self):
        return {
            "state": self.state,
            "message": self.message,
            "encoder": ENCODER_TITLES.get(self.encoder, self.encoder),
            "capture": CAPTURE_TITLES.get(self.capture, self.capture),
            "audio_note": self.audio_note,
            "buffer_in_memory": self.in_memory,
            "window_active": self.window_active,
            "window_name": (self._game_exe if self.cfg["capture_target"] == "game" else self.cfg["window_exe"]) or "",
            "buffer_mb": self.buffer_megabytes(),
            "fps_real": self.fps_real,
            "screen_fps": self.screen_fps,
            "fps_target": self.fps_target,
            "slow": self.slow or (self.capture in ENGINE_KINDS and self.engine_trouble),
            "fallback": self.state == "on" and (
                self.capture in ("gdi", "ddal") or self.encoder in SOFTWARE
                or (self.capture not in ENGINE_KINDS and self.engine is not None and self.cfg["capture"] == "auto")),
            "capture_kind": self.capture,
            "recording": self.recording_since is not None,
            "recording_since": self.recording_since,
        }

    # ------------------------------------------------------- клип и запись

    def save_clip(self, title="", seconds=None):
        """Сохранить последние N секунд. Работает в отдельном потоке."""
        threading.Thread(
            target=self._save_clip, args=(title, seconds), daemon=True, name="save-clip"
        ).start()

    def toggle_recording(self, title=""):
        if self.recording_since is None:
            return self.start_recording(title)
        self.stop_recording()
        return False

    def start_recording(self, title=""):
        if self.state != "on":
            self.on_event("error", {"text": "Захват ещё не запущен — запись не начата."})
            return False
        segments = self._segments()
        if not segments:
            return False
        self._rec_first = os.path.basename(segments[-1])
        self._rec_title = title
        self.recording_since = time.time()
        self.on_event("recording", {"on": True})
        self._emit_state()
        return True

    def stop_recording(self):
        if self.recording_since is None:
            return
        threading.Thread(target=self._finish_recording, daemon=True, name="save-rec").start()

    # --------------------------------------------------------------- поток

    def _run(self):
        crashed = False
        upgrading = False
        while not self._stop.is_set():
            self._restart.clear()
            self._set_state("starting", "Восстанавливаю захват…" if crashed else "Включаю захват…")
            if self._encoders is None:
                self._encoders = self._detect_encoders()
            session = self._launch_any(retry_last=crashed and not upgrading)
            upgrading = False
            if session is None:
                if self._stop.is_set() or self._restart.is_set():
                    continue
                self._set_state(
                    "error",
                    "Не удалось запустить захват экрана. Подробности — в журнале (Настройки → Журнал).",
                )
                self._wait(15)
                continue
            proc, source, pump, started = session
            self._set_state("on", "")
            reason = ""
            last_mark, last_change = None, time.time()
            while not self._stop.is_set() and not self._restart.is_set():
                helper = getattr(proc, "mc_helper", None)
                if helper is not None and helper.poll() is not None:
                    reason = "движок захвата остановился (код %s)" % helper.returncode
                    if helper.returncode == 3:
                        reason = "изменилось разрешение экрана"
                    elif helper.returncode == 6:
                        reason = "окно программы закрыто — записываю весь монитор"
                    elif helper.returncode == 9:
                        reason = "кодировщик в движке отказал"
                    break
                # Окно выбранной программы появилось (или режим сменили) — переключаемся на него.
                if (helper is not None and self.capture in ("enc", "own") and time.time() - self._window_check > 3
                        and self.recording_since is None):
                    self._window_check = time.time()
                    if bool(self._wanted_window()) and not self.window_active:
                        reason = "появилось окно программы — записываю его"
                        break
                if proc.poll() is not None:
                    reason = "ffmpeg остановился (код %s)" % proc.returncode
                    break
                if source is not None and not source.alive():
                    reason = "устройство звука сменилось"
                    break
                if time.time() - started > 20 * 3600 and self.recording_since is None:
                    reason = "плановый перезапуск"   # метки времени в .ts не бесконечны
                    break
                # Запасной режим мог включиться из-за разовой заминки при старте (экран был
                # заблокирован, шло окно UAC…). Через минуту и через десять пробуем вернуться.
                wait = 60 if self._upgrades == 0 else 600
                if (self._upgrades < 2 and self.recording_since is None and not TEST
                        and time.time() - started > wait and self._on_fallback()):
                    self._upgrades += 1
                    upgrading = True
                    self._good = None
                    reason = "пробую вернуться с запасного режима захвата на основной"
                    break
                mark = self._newest_mark()
                if mark != last_mark:
                    last_mark, last_change = mark, time.time()
                elif time.time() - last_change > 12:
                    reason = "запись зависла: 12 секунд нет новых данных. " + self._diag()
                    if self.capture == "dda":
                        # Точный режим ждёт обновлений экрана. Если их нет раз за разом —
                        # значит, здесь он не работает; возвращаемся к старому режиму.
                        now = time.time()
                        self._stalls = [t for t in self._stalls if now - t < 240] + [now]
                        if len(self._stalls) >= 3:
                            self._avoid_precise = True
                            self._good = None
                            reason += "; точный режим захвата отключён до перезапуска программы"
                    break
                self._prune()
                self._wait(1)
            soft = self._soft and self._restart.is_set() and not self._stop.is_set()
            self._soft = False
            deliberate = (self._stop.is_set() or self._restart.is_set()) and not soft
            if soft:
                reason = "изменена громкость записи"
            if deliberate and self.recording_since is not None:
                self._finish_recording(wait_next=False)
            self._shutdown(proc, source, pump)
            if deliberate:
                # Настройки могли измениться — старые кусочки с новыми не склеить.
                with self._save_lock:
                    with self._mem_lock:
                        self._mem_epoch += 1   # читатель старого потока больше ничего не добавит
                    for path in self._segments():
                        self._drop(path)
                crashed = False
            else:
                # Сбой (игра сменила режим экрана, UAC, сон монитора…): буфер и идущая
                # запись сохраняются, захват поднимается заново и продолжает с того же места.
                crashed = True
                self._log("%s — перезапускаю захват, буфер и запись сохранены" % reason)
                if self.capture == "enc" and reason.startswith(("кодировщик в движке", "ffmpeg остановился", "запись зависла")):
                    # Кодирование в движке падает раз за разом — значит, здесь оно не работает;
                    # возвращаемся к проверенному способу (кодирует ffmpeg).
                    now = time.time()
                    self._enc_crashes = [t for t in self._enc_crashes if now - t < 180] + [now]
                    if len(self._enc_crashes) >= 3:
                        self._avoid_enc = True
                        self._good = None
                        self._log("кодирование в движке отключено до перезапуска программы: три сбоя за три минуты")
        self._set_state("off", "")

    def _on_fallback(self):
        """Сейчас работает не лучший из доступных вариантов захвата?"""
        best = self._candidates(prefer_good=False)
        return bool(best) and self._good is not None and self._good != best[0]

    def _log(self, text):
        try:
            with open(self.log_path, "a", encoding="utf-8", errors="replace") as log:
                log.write("\n*** %s | %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text))
        except OSError:
            pass

    def _newest_mark(self):
        """(имя, размер) самого свежего кусочка — чтобы заметить, что запись встала."""
        current = self._current_segments()
        if not current:
            return None
        try:
            return current[-1], self._seg_size(current[-1])
        except OSError:
            return current[-1], -1

    def _wait(self, seconds):
        end = time.time() + seconds
        while time.time() < end and not self._stop.is_set() and not self._restart.is_set():
            time.sleep(0.1)

    # ---------------------------------------------------- запуск ffmpeg

    def _detect_encoders(self):
        if TEST:
            return ["libx264"]
        found = []
        self._hevc = {"libx265"}
        for enc in HW_ENCODERS + [HEVC_TWIN[e] for e in HW_ENCODERS]:
            if enc.startswith("hevc_") and enc.replace("hevc_", "h264_") not in found:
                continue   # этой видеокарты нет — её HEVC и пробовать незачем
            cmd = [
                self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                "-i", "color=c=black:s=640x360:r=30", "-frames:v", "10",
                "-c:v", enc, "-f", "null", "-",
            ]
            try:
                ok = subprocess.run(
                    cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=20, creationflags=NO_WINDOW,
                ).returncode == 0
            except (OSError, subprocess.SubprocessError):
                ok = False
            if ok and enc.startswith("hevc_"):
                self._hevc.add(enc)
            elif ok:
                found.append(enc)
        return found + ["libx264"]

    def _candidates(self, prefer_good=True):
        """Варианты «способ захвата + кодировщик» от лучшего к самому надёжному."""
        if TEST:
            return [("test", "libx264", False)]
        wanted = self.cfg["encoder"]
        if wanted != "auto" and wanted in self._encoders:
            encoders = [wanted] + (["libx264"] if wanted != "libx264" else [])
        else:
            encoders = list(self._encoders)
        if self.cfg["codec"] == "hevc":
            # HEVC — тем же кодировщиком, если он умеет; следом остаётся H.264 как запасной вариант.
            # На процессоре HEVC слишком тяжёл для записи на лету — только если процессор выбран явно.
            twins = []
            for enc in encoders:
                twin = HEVC_TWIN.get(enc)
                if twin in self._hevc and (enc != "libx264" or wanted == "libx264"):
                    twins.append(twin)
                twins.append(enc)
            encoders = twins
        capture = self.cfg["capture"]
        # dda  — точный режим: кадр берётся в момент обновления экрана, без неточного Sleep;
        # ddal — старый режим: ffmpeg сам отмеряет кадры (на Windows получается с рывками);
        # gdi  — самый медленный запасной.
        # own  — свой движок: сам отмеряет кадры точным таймером и сам переживает потерю экрана;
        #        кадр готовится на видеокарте (курсор, уменьшение, NV12); owncpu — то же без видеокарты;
        # enc  — тот же движок, но и кодирует сам: кадр вообще не покидает видеокарту;
        captures = {"enc": ["enc", "own", "owncpu"], "own": ["own", "owncpu"], "owncpu": ["owncpu", "dda"],
                    "dda": ["dda", "ddal"], "ddal": ["ddal"],
                    "gdi": ["gdi"]}.get(capture, ["enc", "own", "owncpu", "dda", "ddal", "gdi"])
        if not self.engine:
            captures = [c for c in captures if c not in ENGINE_KINDS] or ["dda", "ddal", "gdi"]
        if self._avoid_enc and "enc" in captures and len(captures) > 1:
            captures.remove("enc")
        if self._avoid_precise and "dda" in captures and len(captures) > 1:
            captures.remove("dda")
        result = []
        for enc in encoders:
            for cap in captures:
                if cap == "enc":
                    if vendor(enc) in ("nvenc", "amf"):
                        result.append((cap, enc, True))   # движок кодирует сам, кадр остаётся на видеокарте
                    elif capture == "enc":
                        result.append((cap, enc, False))  # выбрано вручную: движок кодирует из обычной памяти
                    continue
                if cap == "own":
                    result.append((cap, enc, True))       # готовый NV12 сразу в кодировщик
                    continue
                if cap == "owncpu":
                    if vendor(enc) == "nvenc" and not self._needs_scaling():
                        result.append((cap, enc, True))   # кадр BGRA сразу в кодировщик видеокарты
                    result.append((cap, enc, False))
                    continue
                if cap in ("dda", "ddal") and vendor(enc) in ("nvenc", "amf") and not self.cfg["height"]:
                    result.append((cap, enc, True))   # кадры не покидают видеокарту
                result.append((cap, enc, False))
        if prefer_good and self._good in result:
            result.remove(self._good)
            result.insert(0, self._good)
        return result

    def _needs_scaling(self):
        """Нужно ли уменьшать картинку (выбранная высота меньше высоты экрана)."""
        height = int(self.cfg["height"])
        if not height:
            return False
        mons = self.monitors()
        idx = int(self.cfg["monitor"])
        if 0 <= idx < len(mons):
            return height < mons[idx]["h"]
        return True

    def _launch_any(self, retry_last=False):
        if retry_last and self._good:
            # После сбоя захват экрана бывает недоступен секунду-другую — сначала
            # несколько раз пробуем тот же вариант, а не сползаем на запасной.
            for _ in range(4):
                if self._stop.is_set() or self._restart.is_set():
                    return None
                session = self._launch(self._good)
                if session:
                    return session
                self._wait(1.5)
        # Если не запускается со звуком — лучше писать без микрофона или без звука, чем никак.
        levels = [0]
        if self.cfg["mic"] and not TEST:
            levels.append(1)
        if self.cfg["audio"] or self.cfg["mic"]:
            levels.append(2)
        for level in levels:
            self._degrade = level
            for cand in self._candidates():
                if self._stop.is_set() or self._restart.is_set():
                    return None
                session = self._launch(cand)
                if session:
                    self._good = cand
                    self.capture, self.encoder = cand[0], cand[1]
                    if level == 1:
                        self.audio_note = "Микрофон не подключился — пишу без него. Подробности в журнале."
                    elif level == 2:
                        self.audio_note = "Звук не подключился — пишу без звука. Подробности в журнале."
                    return session
        return None

    def _launch(self, cand):
        capture, encoder, zero_copy = cand
        self._session += 1
        prefix = "s%04d_" % self._session
        source = None
        self.audio_note = ""
        # Свой движок снимает звук сам, на тех же часах, что и кадры.
        engine_audio = (capture in ENGINE_KINDS and self._degrade < 2
                        and bool(self.cfg["audio"] or self.cfg["mic"]))
        if self.cfg["audio"] and self._degrade < 2 and not engine_audio:
            source = audio_mod.FakeSource() if TEST else audio_mod.LoopbackSource()
            try:
                source.open()
            except Exception as exc:  # нет звуковой карты, нет библиотеки и т.п.
                self.audio_note = "Звук системы не записывается: %s" % exc
                source.close()
                source = None
        helper, own = None, None
        if capture in ENGINE_KINDS:
            helper, own = self._start_engine(gpu=capture != "owncpu", audio=engine_audio,
                                             encode=(encoder, zero_copy) if capture == "enc" else None)
            if helper is None:
                if source:
                    source.close()
                return None
        server, port = None, 0
        self.in_memory = self._use_memory()
        if self.in_memory:
            try:
                server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                server.bind(("127.0.0.1", 0))
                server.listen(1)
                port = server.getsockname()[1]
                threading.Thread(target=self._mem_reader, args=(server, prefix), daemon=True, name="buffer").start()
            except OSError as exc:
                self._log("буфер в памяти недоступен (%s) — пишу на диск" % exc)
                server, port, self.in_memory = None, 0, False
        cmd = self._build_cmd(capture, encoder, zero_copy, source, prefix, own, port)
        try:
            log = open(self.log_path, "a", encoding="utf-8", errors="replace")
            log.write("\n=== %s | %s + %s%s ===\n%s\n" % (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"), capture, encoder,
                " (zero-copy)" if zero_copy else "", " ".join(cmd)))
            log.flush()
            # Кодирование видеокартой почти не грузит процессор, поэтому ему можно дать
            # приоритет повыше — тогда игра не «выдавливает» запись и в клипе меньше рывков.
            priority = ABOVE_NORMAL if encoder not in SOFTWARE else 0
            proc = subprocess.Popen(
                cmd, cwd=self.work_dir, bufsize=0,
                stdin=subprocess.PIPE if source else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=log, creationflags=NO_WINDOW | priority,
            )
            proc.mc_helper = helper
            self.fps_real, self.slow = None, False
            self.fps_target = max(15, min(240, int(self.cfg["fps"])))
            threading.Thread(target=self._watch_progress, args=(proc, self.fps_target),
                             daemon=True, name="progress").start()
            log.close()
        except OSError as exc:
            self.message = str(exc)
            if source:
                source.close()
            self._kill(helper)
            return None
        pump = None
        if source:
            pump = audio_mod.AudioPump(source, proc.stdin)
            pump.start()
        # Успех = появился первый кусочек с данными.
        deadline = time.time() + 12
        while time.time() < deadline and proc.poll() is None and not self._stop.is_set():
            files = self._segments(prefix)
            if files and max(self._seg_size(f) for f in files) > 0:
                self._proc = proc
                self._track_names = list(own[5]) if own else ["Всё вместе"]
                self._format_changed((encoder, own[1:4] if own else None, self.fps_target,
                                      int(self.cfg["height"]), len(self._track_names)), prefix)
                return proc, source, pump, time.time()
            if helper is not None and helper.poll() is not None:
                break
            time.sleep(0.2)
        if helper is not None and helper.poll() is not None:
            why = "движок захвата вышел с кодом %s" % helper.returncode
        elif proc.poll() is not None:
            why = "ffmpeg вышел с кодом %s" % proc.returncode
        elif self._stop.is_set():
            why = "остановлено"
        else:
            why = "за 12 секунд не пришло ни одного кадра"
        self._log("вариант %s + %s не запустился: %s. %s" % (capture, encoder, why, self._diag()))
        self._shutdown(proc, source, pump)
        for path in self._segments(prefix):
            self._drop(path)
        return None

    def _wanted_window(self):
        if self._window_failed:
            return None
        target = self.cfg["capture_target"]
        try:
            if target == "window" and self.cfg["window_exe"]:
                return self.find_window(self.cfg["window_exe"])
            if target == "game":
                if self._game_exe:
                    hwnd = self.find_window(self._game_exe)
                    if hwnd:
                        return hwnd
                    if self.window_active:
                        return None            # игра свёрнута — остаёмся на её окне
                    self._game_exe = None      # игра закрыта
                names = lambda key: {n.strip().lower() for n in str(self.cfg[key] or "").replace(";", ",").split(",") if n.strip()}
                found = self.detect_game(names("game_include"), names("game_exclude"))
                if found and found[0] not in self._game_skip:
                    self._game_exe = found[0]
                    self._log("обнаружена игра: %s" % found[0])
                    return found[1]
        except Exception:
            pass
        return None

    def app_volumes(self):
        """{имя exe: громкость в процентах} из настроек."""
        try:
            data = json.loads(self.cfg["app_volumes"] or "{}")
            return {str(k).lower(): max(0, min(200, int(v))) for k, v in data.items() if str(k).strip()}
        except (ValueError, TypeError, AttributeError):
            return {}

    def audio_rows(self):
        """Строки списка «Что записывать из звука»: {ключ: (включено, громкость %)}.
        Ключ — имя exe либо «@game» (игра) и «@self» (сигналы самой программы)."""
        try:
            data = json.loads(self.cfg["app_volumes"] or "{}")
        except (ValueError, TypeError):
            return {}
        rows = {}
        for key, value in (data.items() if isinstance(data, dict) else []):
            key = str(key).strip().lower()
            try:
                if isinstance(value, dict):
                    rows[key] = (bool(value.get("on", True)), max(0, min(200, int(value.get("vol", 100)))))
                elif key:
                    rows[key] = (True, max(0, min(200, int(value))))
            except (ValueError, TypeError):
                pass
        return rows

    def audio_plan(self):
        """Что и с какой громкостью пишет движок и как звук раскладывается по дорожкам."""
        rows = self.audio_rows()
        specific = self.cfg["audio_mode"] == "apps"
        default = 0.0 if specific else max(0, min(200, int(self.cfg["rec_volume_system"]))) / 100.0
        target = self.cfg["capture_target"]
        game = (self._game_exe if target == "game" else self.cfg["window_exe"] if target == "window" else "") or ""
        game = game.lower()
        own = os.path.basename(sys.executable).lower()
        gains = {k: (vol / 100.0 if on else 0.0) for k, (on, vol) in rows.items() if not k.startswith("@")}
        if "@game" in rows and game and game not in gains:
            gains[game] = rows["@game"][1] / 100.0 if rows["@game"][0] else 0.0
        include_parent = False
        if "@self" in rows or specific:
            on, vol = rows.get("@self", (True, 100))
            gains[own] = vol / 100.0 if on else 0.0
            include_parent = on
        if specific and not gains:
            gains["@none"] = 0.0   # режим «только отмеченные» без отмеченных: движок всё равно снимает программы по одной
        mic = bool(self.cfg["mic"]) and bool(self.cfg["mic_on"]) and self._degrade < 1
        plan = {"default": default, "gains": gains, "include_parent": include_parent, "mic": mic,
                "names": ["Всё вместе"], "mic_track": 0, "other_track": 0, "app_track": {}}
        if self.cfg["separate_tracks"]:
            def add(name):
                plan["names"].append(name)
                return len(plan["names"]) - 1
            if mic:
                plan["mic_track"] = add("Микрофон")
            # Дорожка игры есть всегда, когда записывается игра или окно, — даже пока игра не запущена:
            # иначе набор дорожек менялся бы при входе в игру, и буфер приходилось бы начинать заново.
            if target in ("game", "window") and rows.get("@game", (True, 100))[0]:
                index = add("Игра")
                if game and gains.get(game, default) > 0:
                    plan["app_track"][game] = index
            for exe in sorted(gains):
                if exe not in (game, own) and gains[exe] > 0 and len(plan["names"]) < 7:
                    plan["app_track"][exe] = add(exe[:-4].capitalize() if exe.endswith(".exe") else exe)
            if default > 0 or include_parent:
                plan["other_track"] = add("Остальное")
                if include_parent and default <= 0:
                    plan["app_track"][own] = plan["other_track"]
        return plan

    def list_audio_apps(self):
        """Программы, у которых сейчас есть звук (спрашиваем у движка)."""
        if not self.engine:
            return []
        try:
            out = subprocess.run([self.engine, "--list-audio-apps", "--parent", str(os.getpid())],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 timeout=8, creationflags=NO_WINDOW).stdout.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            return []
        return sorted({line[4:].strip() for line in out.splitlines() if line.startswith("APP ") and line[4:].strip()})

    def _start_engine(self, gpu=True, audio=False, allow_window=True, encode=None):
        """Запустить mccap и дождаться строки «READY ширина высота fps»."""
        fps = max(15, min(240, int(self.cfg["fps"])))
        pipe = r"\\.\pipe\mccap_%d_%d" % (os.getpid(), self._session)
        args = [self.engine, "--pipe", pipe, "--fps", str(fps), "--parent", str(os.getpid())]
        audio_pipe = ""
        tracks = ["Всё вместе"]
        if audio:
            audio_pipe = pipe + "_a"
            plan = self.audio_plan()
            tracks = plan["names"]
            args += ["--audio-pipe", audio_pipe, "--audio-system", "1" if self.cfg["audio"] else "0",
                     "--vol-system", "%.2f" % plan["default"]]
            for exe, gain in sorted(plan["gains"].items()):   # своя громкость программ; 0 — в запись не идёт
                args += ["--app-vol", "%s=%.2f" % (exe, gain)]
            if plan["include_parent"]:
                args += ["--include-parent"]
            if plan["mic"]:
                args += ["--mic", self.cfg["mic"],
                         "--vol-mic", "%.2f" % (max(0, min(200, int(self.cfg["rec_volume_mic"]))) / 100.0)]
            if len(tracks) > 1:   # отдельные дорожки
                args += ["--tracks", str(len(tracks) - 1), "--track-mic", str(plan["mic_track"]),
                         "--track-other", str(plan["other_track"])]
                for exe, index in sorted(plan["app_track"].items()):
                    args += ["--track", "%s=%d" % (exe, index)]
        if not self.cfg["cursor"]:
            args.append("--no-cursor")
        if encode:   # движок кодирует сам; в канал идёт уже сжатое видео
            mbps = max(2, min(100, int(self.cfg["bitrate_mbps"])))
            args += ["--encode", encode[0], "--bitrate", str(mbps), "--frames", "gpu" if encode[1] else "cpu"]
        self.window_active = False
        hwnd = self._wanted_window() if gpu and allow_window and not self.engine_test else None
        if hwnd:   # записывать только окно выбранной программы
            args += ["--window", str(hwnd)]
            self.window_active = True
            if self.cfg["capture_target"] == "game":
                # Кадр — как при записи монитора: тогда вход в игру и выход из неё не сбрасывают буфер.
                mons = self.monitors()
                idx = int(self.cfg["monitor"])
                mon = mons[idx] if 0 <= idx < len(mons) else (mons[0] if mons else None)
                if mon:
                    want = int(self.cfg["height"])
                    out_h = want if 0 < want < mon["h"] else mon["h"]
                    out_w = int(mon["w"] * out_h / mon["h"] + 0.5)
                    args += ["--frame", "%dx%d" % (out_w & ~1, out_h & ~1)]
        if not gpu:
            args += ["--cpu"]
        elif int(self.cfg["height"]) > 0:
            args += ["--height", str(int(self.cfg["height"]))]   # уменьшение — на видеокарте
        if self.engine_test:
            args += ["--test", "--size", "1280x720"]
        elif not hwnd:
            mons = self.monitors()
            idx = int(self.cfg["monitor"])
            if 0 <= idx < len(mons):
                args += ["--x", str(mons[idx]["x"]), "--y", str(mons[idx]["y"])]
        try:
            helper = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        except OSError as exc:
            self._log("движок захвата не запустился: %s" % exc)
            return None, None
        threading.Thread(target=self._watch_engine, args=(helper, fps), daemon=True, name="engine-log").start()
        line = []
        reader = threading.Thread(target=lambda: line.append(helper.stdout.readline()), daemon=True)
        reader.start()
        reader.join(8)
        text = line[0].decode("ascii", "replace").strip() if line else ""
        parts = text.split()
        if len(parts) < 4 or parts[0] != "READY":
            code = helper.poll()
            self._log("движок захвата не готов (ответ: %r, код %s)" % (text, code))
            self._kill(helper)
            if encode and code == 8:   # кодировщик в движке не запустился — больше не пробуем, кодирует ffmpeg
                self._avoid_enc = True
                self._log("кодирование в движке здесь недоступно — кодирует ffmpeg")
            if hwnd and code in (6, 7):   # окно не снимается (закрылось или захват окон недоступен) — пишем монитор
                if self._game_exe:        # найденную игру записать не вышло — больше на неё не переключаемся
                    self._game_skip.add(self._game_exe)
                    self._game_exe = None
                if code == 7:
                    self._window_failed = True
                    self._log("захват окна здесь недоступен — записываю весь монитор")
                return self._start_engine(gpu, audio, allow_window=False, encode=encode)
            return None, None
        return helper, (pipe, int(parts[1]), int(parts[2]), parts[4] if len(parts) > 4 else "bgra", audio_pipe, tracks)

    def _watch_engine(self, helper, fps):
        """Сообщения движка — в журнал; из статистики берём реальную частоту обновления экрана."""
        count = 0
        self.screen_fps = None
        try:
            for raw in helper.stderr:
                text = raw.decode("utf-8", "replace").strip()
                if text.startswith("stat "):
                    values = dict(p.split("=", 1) for p in text[5:].split() if "=" in p)
                    try:
                        self.screen_fps = float(values.get("new", "0"))
                    except ValueError:
                        continue
                    count += 1
                    try:   # кодировщик не успевает: очередь переполняется или кадров уходит меньше нужного
                        trouble = (int(values.get("overflow", "0")) > 0
                                   or float(values.get("out", "0")) < fps * 0.9)
                    except ValueError:
                        trouble = False
                    self.engine_trouble = trouble and count > 2   # первые секунды — разгон, не считаются
                    if count % 12 != 1 and not (self.engine_trouble and count % 2 == 0):
                        continue   # в журнал — раз в минуту; при проблемах — каждые 10 секунд
                if text.startswith("info: audio "):   # состояние звука — показать в окне программы
                    note = self.audio_note
                    if "mic unavailable 0x80070005" in text:
                        note = "Windows не даёт доступ к микрофону: Параметры → Конфиденциальность → Микрофон."
                    elif "mic unavailable" in text:
                        note = "Микрофон не найден — запись идёт без него."
                    elif "per-app unavailable" in text:
                        note = "Отдельная громкость программ здесь не работает (нужна Windows 10 2004 или новее) — пишу общий звук."
                    elif "system unavailable" in text:
                        note = "Звук компьютера недоступен — нет устройства вывода."
                    elif text.endswith(" opened"):
                        note = ""
                    if note != self.audio_note:
                        self.audio_note = note
                        self._emit_state()
                if text:
                    self._log("движок: " + text)
        except (OSError, ValueError):
            pass

    @staticmethod
    def _kill(process):
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

    def _format_changed(self, key, keep_prefix):
        """Новый запуск пишет в другом формате (другой размер экрана, кодировщик) —
        старые кусочки с новыми в один клип не склеить, убираем их."""
        if self._format is not None and key != self._format:
            if self.recording_since is not None:
                self._finish_recording(wait_next=False, exclude_prefix=keep_prefix)
            with self._save_lock:
                for path in self._segments():
                    if not os.path.basename(path).startswith(keep_prefix):
                        self._drop(path)
            self._log("формат записи изменился — буфер начат заново")
        self._format = key

    def _build_cmd(self, capture, encoder, zero_copy, source, prefix, own=None, memory_port=0):
        cfg = self.cfg
        fps = max(15, min(240, int(cfg["fps"])))
        height = int(cfg["height"])
        mbps = max(2, min(100, int(cfg["bitrate_mbps"])))
        mouse = 1 if cfg["cursor"] else 0
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostats", "-y",
               "-progress", "pipe:1", "-stats_period", "2"]

        # --- вход 0: экран
        if capture == "test":
            cmd += ["-re", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=%d" % fps]
        elif capture == "enc":
            # Видео уже сжато движком, у каждого кадра своё точное время; ffmpeg его не трогает.
            cmd += ["-analyzeduration", "1000000", "-f", "nut", "-i", own[0]]
        elif capture in ("own", "owncpu"):
            # Кадры приходят ровно с нужной частотой, по порядку — время кадра считается по номеру;
            # ffmpeg только кодирует и пишет.
            cmd += ["-f", "rawvideo", "-pixel_format", own[3],
                    "-video_size", "%dx%d" % (own[1], own[2]), "-framerate", str(fps), "-i", own[0]]
        elif capture == "dda":
            # Частота с большим запасом и без повторов кадров: ddagrab тогда не «спит» между
            # кадрами (Sleep в Windows ошибается на 10–15 мс), а ждёт настоящего обновления
            # экрана. До нужной частоты поток выравнивает уже сам ffmpeg (-r ниже).
            graph = "ddagrab=output_idx=%d:framerate=1000:dup_frames=0:draw_mouse=%d" % (int(cfg["monitor"]), mouse)
            # Лишние кадры (монитор 144 Гц, запись 30) отбрасываем сразу, пока они ещё в памяти
            # видеокарты, — иначе каждый пришлось бы копировать и уменьшать процессором.
            graph += ",fps=%d" % fps
            if not zero_copy:
                graph += ",hwdownload,format=bgra"
            cmd += ["-f", "lavfi", "-i", graph]
        elif capture == "ddal":
            graph = "ddagrab=output_idx=%d:framerate=%d:draw_mouse=%d" % (int(cfg["monitor"]), fps, mouse)
            if not zero_copy:
                graph += ",hwdownload,format=bgra"
            cmd += ["-f", "lavfi", "-i", graph]
        else:
            cmd += ["-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", str(mouse)]
            mons = self.monitors()
            idx = int(cfg["monitor"])
            if 0 <= idx < len(mons):
                m = mons[idx]
                cmd += ["-offset_x", str(m["x"]), "-offset_y", str(m["y"]),
                        "-video_size", "%dx%d" % (m["w"], m["h"])]
            cmd += ["-i", "desktop"]

        # --- входы звука
        audio_inputs = []
        engine_audio = bool(own and len(own) > 4 and own[4])
        if engine_audio:   # звук уже сведён движком: 48 кГц, стерео, громкости применены
            cmd += ["-f", "s16le", "-ar", "48000", "-ac", "2"]
            offset = int(cfg["audio_offset_ms"])
            if offset:
                cmd += ["-itsoffset", "%.3f" % (offset / 1000.0)]
            cmd += ["-i", own[4]]
            audio_inputs.append(1)
            for k in range(1, len(own[5])):   # отдельные дорожки — такие же каналы, по одному на дорожку
                cmd += ["-f", "s16le", "-ar", "48000", "-ac", "2"]
                if offset:
                    cmd += ["-itsoffset", "%.3f" % (offset / 1000.0)]
                cmd += ["-i", "%s_%d" % (own[4], k)]
        elif source:
            cmd += ["-f", "s16le", "-ar", str(source.rate), "-ac", str(source.channels)]
            offset = int(cfg["audio_offset_ms"])
            if offset:
                cmd += ["-itsoffset", "%.3f" % (offset / 1000.0)]
            cmd += ["-i", "pipe:0"]
            audio_inputs.append(1)
        if cfg["mic"] and not TEST and self._degrade < 1 and not engine_audio:
            # Маленький буфер: иначе микрофон приходит крупными порциями, и ffmpeg в такт ему
            # читает порциями и видео.
            cmd += ["-f", "dshow", "-audio_buffer_size", "30", "-i", "audio=" + cfg["mic"]]
            audio_inputs.append(len(audio_inputs) + 1)

        # --- видео
        if capture == "enc":
            cmd += ["-c:v", "copy"]
        elif not zero_copy:
            if height:
                scale = "scale=-2:'trunc(min(ih,%d)/2)*2'" % height
            else:
                scale = "scale='trunc(iw/2)*2':'trunc(ih/2)*2'"
            pix = "yuv420p" if encoder in SOFTWARE else "nv12"
            cmd += ["-vf", "%s,format=%s" % (scale, pix)]
        if capture != "enc":
            cmd += ["-r", str(fps), "-fps_mode", "cfr"]   # ровная частота кадров на выходе
            if own and own[3] == "nv12":   # движок отдаёт цвет в стандарте BT.709 — подписываем, чтобы плееры не гадали
                cmd += ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
            rate = ["-b:v", "%dM" % mbps, "-maxrate", "%dM" % int(mbps * 1.5),
                    "-bufsize", "%dM" % (mbps * 2), "-g", str(fps)]
            if vendor(encoder) == "nvenc":
                # при высокой частоте кадров берём более быстрый пресет, чтобы кодер успевал
                cmd += ["-c:v", encoder, "-preset", "p4" if fps <= 60 else "p2", "-rc", "vbr"] + rate
            elif vendor(encoder) == "amf":
                cmd += ["-c:v", encoder, "-usage", "transcoding", "-quality", "balanced",
                        "-rc", "vbr_peak"] + rate
                if encoder == "hevc_amf":   # служебные заголовки — в каждом опорном кадре: по ним режется буфер
                    cmd += ["-header_insertion_mode", "idr"]
            elif vendor(encoder) == "qsv":
                cmd += ["-c:v", encoder, "-preset", "veryfast"] + rate
            elif encoder == "libx265":
                cmd += ["-c:v", "libx265", "-preset", "ultrafast", "-tune", "zerolatency", "-x265-params",
                        "keyint=%d:min-keyint=%d:scenecut=0:repeat-headers=1:log-level=error" % (fps, fps)] + rate
            else:
                cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency"] + rate
                cmd += ["-keyint_min", str(fps), "-sc_threshold", "0"]


        # --- звук
        # Громкость меняется только в записи: то, что слышит сам пользователь, не трогаем.
        vol_sys = max(0, min(200, int(cfg["rec_volume_system"]))) / 100.0
        vol_mic = max(0, min(200, int(cfg["rec_volume_mic"]))) / 100.0
        if len(audio_inputs) == 2:
            cmd += ["-filter_complex",
                    "[1:a]aresample=48000,volume=%.2f[sys];[2:a]aresample=48000:async=1,volume=%.2f[mic];"
                    "[sys][mic]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]" % (vol_sys, vol_mic),
                    "-map", "0:v", "-map", "[a]"]
        elif audio_inputs:
            cmd += ["-map", "0:v", "-map", "1:a"]
            if engine_audio:
                for k in range(1, len(own[5])):
                    cmd += ["-map", "%d:a" % (k + 1)]
            volume = 1.0 if engine_audio else (vol_sys if source else vol_mic)
            if abs(volume - 1.0) > 0.001:
                cmd += ["-af", "volume=%.2f" % volume]
        else:
            cmd += ["-map", "0:v", "-an"]
        if audio_inputs:
            cmd += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]

        if memory_port:
            # Буфер в оперативной памяти: поток уходит программе по локальному соединению,
            # на диск ничего не пишется, пока клип не сохраняют.
            cmd += ["-f", "mpegts", "-flush_packets", "1", "tcp://127.0.0.1:%d" % memory_port]
        else:
            cmd += ["-flush_packets", "1", "-f", "segment", "-segment_time", str(SEGMENT), "-segment_format", "mpegts",
                    prefix + "%06d.ts"]
        return cmd

    # --------------------------------------------------- буфер в памяти

    def buffer_megabytes(self):
        """Сколько примерно занимает буфер повтора при текущих настройках."""
        seconds = int(self.cfg["buffer_seconds"]) if self.cfg["buffer_enabled"] else 8
        mbps = max(2, min(100, int(self.cfg["bitrate_mbps"]))) + 0.2
        return int(mbps * (seconds + 8) / 8)

    def _use_memory(self):
        mode = self.cfg["buffer_storage"]
        if mode == "memory":
            return True
        if mode == "disk":
            return False
        return self.buffer_megabytes() <= MEMORY_LIMIT_MB

    def _mem_reader(self, server, prefix):
        """Принимает поток от ffmpeg и режет его на кусочки по SEGMENT секунд — в памяти.
        Пока идёт обычная запись, готовые кусочки сбрасываются на диск: запись может быть
        многочасовой, в памяти её держать нельзя."""
        try:
            server.settimeout(25)
            conn, _addr = server.accept()
        except OSError:
            server.close()
            return
        server.close()
        index, started, scan = 0, time.monotonic(), 0
        name = prefix + "%06d.ts" % index
        buf = bytearray()
        with self._mem_lock:
            epoch = self._mem_epoch
            self._mem[name] = buf
            self._mem_started = {name: started}
        try:
            conn.settimeout(5)
            while True:
                try:
                    data = conn.recv(262144)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
                with self._mem_lock:
                    if epoch != self._mem_epoch:
                        break   # буфер очистили (смена настроек) — этот поток больше не нужен
                    buf += data
                # Резать поток можно только перед опорным кадром: тогда любой кусочек начинается
                # с целой картинки. Опорный кадр виден по пометке в заголовке пакета (188 байт).
                elapsed = time.monotonic() - started
                limit = len(buf) - len(buf) % 188
                cut = None
                if elapsed >= SEGMENT - 0.5:
                    while scan < limit:
                        if (scan > 0 and buf[scan] == 0x47 and buf[scan + 3] & 0x20 and buf[scan + 4] > 0
                                and buf[scan + 5] & 0x40 and ((buf[scan + 1] & 0x1F) << 8 | buf[scan + 2]) == 0x100):
                            cut = scan
                            break
                        scan += 188
                    if cut is None and elapsed > SEGMENT * 4 and limit:
                        cut = limit   # опорных кадров не видно — режем как есть
                else:
                    scan = max(188, limit)
                if cut is None:
                    continue
                while cut >= 188:   # служебные таблицы перед опорным кадром уходят в новый кусочек вместе с ним
                    pid = (buf[cut - 187] & 0x1F) << 8 | buf[cut - 186]
                    if pid not in (0x0000, 0x0011, 0x1000):
                        break
                    cut -= 188
                if cut <= 0:
                    scan += 188
                    continue
                with self._mem_lock:
                    done, rest = bytes(buf[:cut]), bytearray(buf[cut:])
                    index += 1
                    new_name = prefix + "%06d.ts" % index
                    if name in self._mem:          # кусочек могли уже стереть (перезапуск)
                        self._mem[name] = done
                        self._mem_dur[name] = elapsed
                        spill = self._rec_first is not None
                    else:
                        spill = False
                    self._mem[new_name] = rest
                    self._mem_started = {new_name: time.monotonic()}
                if spill:
                    self._spill(name, done)
                name, buf, started, scan = new_name, rest, time.monotonic(), 0
        finally:
            try:
                conn.close()
            except OSError:
                pass
            with self._mem_lock:
                if name in self._mem and epoch == self._mem_epoch:
                    self._mem[name] = bytes(buf)
                    self._mem_dur[name] = time.monotonic() - started
                elif epoch != self._mem_epoch:
                    self._mem.pop(name, None)

    def _spill(self, name, data):
        try:
            with open(os.path.join(self.work_dir, name), "wb") as f:
                f.write(data)
        except OSError as exc:
            self._log("не удалось сбросить кусочек записи на диск: %s" % exc)
            return
        with self._mem_lock:
            self._mem.pop(name, None)

    def _seg_size(self, path):
        with self._mem_lock:
            data = self._mem.get(os.path.basename(path))
            if data is not None:
                return len(data)
        return os.path.getsize(path)

    def _seg_write(self, path, dst, partial=False):
        """Дописать содержимое кусочка (из памяти или с диска) в открытый файл.
        partial — кусочек ещё пишется: берём из него только целые кадры."""
        with self._mem_lock:
            data = self._mem.get(os.path.basename(path))
            if data is not None:
                data = bytes(data)
        if data is None and partial:
            with open(path, "rb") as src:
                data = src.read()
        if data is not None:
            dst.write(whole_frames(data) if partial else data)
        else:
            with open(path, "rb") as src:
                shutil.copyfileobj(src, dst, 4 * 1024 * 1024)

    def _drop(self, path):
        with self._mem_lock:
            self._mem.pop(os.path.basename(path), None)
            self._mem_dur.pop(os.path.basename(path), None)
        self._remove(path)

    def _tail(self, closed, seconds):
        """Последние кусочки общей длиной не меньше seconds и то, сколько срезать с начала."""
        chosen, total = [], 0.0
        for path in reversed(closed):
            chosen.append(path)
            if path == self._partial:
                total += self._partial_dur
            else:
                total += self._mem_dur.get(os.path.basename(path), SEGMENT)
            if total >= seconds + 0.5:
                break
        chosen.reverse()
        extra = total - seconds
        # срезаем целое число секунд (опорные кадры идут раз в секунду) и чуть меньше — чтобы
        # клип начался точно с опорного кадра, а звук не убежал вперёд картинки
        skip = max(0.0, round(extra) - 0.02) if extra >= 0.5 else 0.0
        return chosen, skip, min(seconds, total)

    def _watch_progress(self, proc, target):
        """Читает отчёт ffmpeg раз в 2 секунды и считает, сколько кадров реально пишется."""
        last_frame, last_time, low, started = None, None, 0, time.time()
        frame = None
        try:
            for raw in proc.stdout:
                line = raw.decode("ascii", "replace").strip()
                if line.startswith("frame="):
                    try:
                        frame = int(line[6:])
                    except ValueError:
                        frame = None
                elif line.startswith("progress=") and frame is not None:
                    now = time.time()
                    if last_frame is not None and now - last_time > 0.5:
                        fps = (frame - last_frame) / (now - last_time)
                        self.fps_real = round(fps, 1)
                        if now - started > 8:
                            low = low + 1 if fps < target * 0.8 else 0
                            slow = low >= 3
                            if slow != self.slow:
                                self.slow = slow
                                if slow:
                                    self._log("захват не успевает: %.0f из %d кадров в секунду (%s + %s)" % (
                                        fps, target, self.capture, self.encoder))
                                if proc is self._proc:
                                    self._emit_state()
                    last_frame, last_time = frame, now
        except (OSError, ValueError):
            pass

    def _shutdown(self, proc, source, pump):
        if pump:
            pump.stop()
        self._kill(getattr(proc, "mc_helper", None))
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None:
            try:
                proc.wait(timeout=2)   # без звука на входе ffmpeg сам не выйдет — это нормально
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        if source:
            source.close()
        if self._proc is proc:
            self._proc = None

    # ------------------------------------------------------ кусочки

    def _segments(self, prefix=None):
        """Все кусочки по порядку (и в памяти, и на диске), включая оставшиеся от прошлых запусков захвата."""
        pattern = (prefix + "*.ts") if prefix else "s[0-9][0-9][0-9][0-9]_*.ts"
        found = set(glob.glob(os.path.join(self.work_dir, pattern)))
        with self._mem_lock:
            for name in self._mem:
                if prefix is None or name.startswith(prefix):
                    found.add(os.path.join(self.work_dir, name))
        return sorted(found)

    def _current_segments(self):
        return self._segments("s%04d_" % self._session)

    def _prune(self):
        if not self._save_lock.acquire(False):
            return
        try:
            segments = self._segments()
            keep = math.ceil(int(self.cfg["buffer_seconds"]) / SEGMENT) + 4
            if not self.cfg["buffer_enabled"]:
                keep = 4
            old = segments[:-keep]
            if self._rec_first:
                old = [p for p in old if os.path.basename(p) < self._rec_first]
            for path in old:
                self._drop(path)
        finally:
            self._save_lock.release()

    @staticmethod
    def _remove(path):
        try:
            os.remove(path)
        except OSError:
            pass

    def _closed_segments(self, wait_next=True):
        """Все кусочки для клипа или записи — вместе с тем, что пишется прямо сейчас: из него берётся
        уже готовая часть, так что ждать его закрытия не нужно и запись кончается в момент нажатия."""
        segments = self._segments()
        self._partial, self._partial_dur = None, 0.0
        current_list = self._current_segments()
        alive = self._proc is not None and self._proc.poll() is None
        if not wait_next or not current_list or not alive:
            return segments   # захват перезапускается — берём всё, что есть
        current = current_list[-1]
        name = os.path.basename(current)
        with self._mem_lock:
            started = self._mem_started.get(name)
        if started is not None:
            elapsed = time.monotonic() - started
        else:   # кусочек — файл на диске: он начат, когда закрылся предыдущий
            try:
                before = current_list[-2] if len(current_list) > 1 else None
                elapsed = time.time() - (os.path.getmtime(before) if before else os.path.getctime(current))
            except OSError:
                elapsed = SEGMENT
        self._partial = current
        self._partial_dur = max(0.0, min(SEGMENT + 1.0, elapsed))
        return segments

    def _output_path(self, title, kind):
        folder = self.cfg.clips_dir()
        game = safe_name(title) if title else ""
        if game and self.cfg["folder_per_game"]:
            folder = os.path.join(folder, game)
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
        if game:
            name = "%s %s%s" % (game, "запись " if kind == "Запись" else "", stamp)
        else:
            name = "%s %s" % (kind, stamp)
        path = os.path.join(folder, name + ".mp4")
        n = 2
        while os.path.exists(path):
            path = os.path.join(folder, "%s (%d).mp4" % (name, n))
            n += 1
        return path

    def _mux(self, segments, out_path, skip=0.0):
        """Склеить кусочки в mp4 без перекодирования.

        Кусочки одного запуска захвата идут с непрерывными метками времени и склеиваются
        напрямую. Если захват перезапускался, каждая часть собирается отдельно, а потом
        части соединяются — так клип переживает сбой захвата.
        """
        groups = []
        for path in segments:
            prefix = os.path.basename(path)[:6]
            if groups and groups[-1][0] == prefix:
                groups[-1][1].append(path)
            else:
                groups.append((prefix, [path]))
        if len(groups) == 1:
            return self._mux_run(segments, out_path, skip)
        stamp = int(time.time() * 1000)
        parts = []
        list_path = os.path.join(self.work_dir, "parts_%d.txt" % stamp)
        try:
            for i, (_prefix, group) in enumerate(groups):
                part = os.path.join(self.work_dir, "part_%d_%d.mp4" % (stamp, i))
                parts.append(part)
                if not self._mux_run(group, part, 0.0, faststart=False):
                    parts.pop()   # битая часть (например, оборванный кусочек) — пропускаем
            if not parts:
                return False
            with open(list_path, "w", encoding="utf-8") as f:
                for part in parts:
                    f.write("file '%s'\n" % os.path.basename(part))
            cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "concat", "-safe", "0", "-i", os.path.basename(list_path)]
            if skip > 0.05:
                cmd += ["-ss", "%.3f" % skip]
            cmd += ["-map", "0:v", "-map", "0:a?", "-c", "copy"] + self._track_titles()
            cmd += ["-movflags", "+faststart", out_path]
            return self._run_mux(cmd, out_path)
        finally:
            for path in parts + [list_path]:
                self._remove(path)

    def _mux_run(self, segments, out_path, skip=0.0, faststart=True):
        names = [os.path.basename(p) for p in segments]
        joined = None
        with self._mem_lock:
            in_memory = any(n in self._mem for n in names)
        partial = self._partial if self._partial in segments else None
        if not in_memory and not partial and sum(len(n) + 1 for n in names) < CONCAT_LIMIT:
            source = "concat:" + "|".join(names)
        else:  # кусочки в памяти (или их очень много) — собираем во временный файл
            joined = os.path.join(self.work_dir, "joined_%d.tmp" % int(time.time() * 1000))
            with open(joined, "wb") as dst:
                for path in segments:
                    try:
                        self._seg_write(path, dst, partial=path == partial)
                    except OSError:
                        pass   # кусочек успели убрать — пропускаем
            source = os.path.basename(joined)
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "mpegts", "-i", source]
        if skip > 0.05:
            cmd += ["-ss", "%.3f" % skip]
        cmd += ["-map", "0:v", "-map", "0:a?", "-c", "copy"] + self._track_titles()
        if self.encoder in HEVC_TWIN.values():
            cmd += ["-tag:v", "hvc1"]   # так HEVC открывают и плееры Apple
        if faststart:
            cmd += ["-movflags", "+faststart"]
        cmd += [out_path]
        try:
            return self._run_mux(cmd, out_path)
        finally:
            if joined:
                self._remove(joined)

    def _track_titles(self):
        """Названия звуковых дорожек в готовом файле (видны в редакторах и плеерах)."""
        names = self._track_names
        if len(names) < 2:
            return []
        out = []
        for i, name in enumerate(names):
            out += ["-metadata:s:a:%d" % i, "title=" + name, "-metadata:s:a:%d" % i, "handler_name=" + name]
        return out

    def _run_mux(self, cmd, out_path):
        try:
            result = subprocess.run(
                cmd, cwd=self.work_dir, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, creationflags=NO_WINDOW, timeout=3600,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self._log("сохранение не удалось: %s" % exc)
            return False
        ok = os.path.isfile(out_path) and os.path.getsize(out_path) > 0
        if result.returncode != 0 or not ok:
            self._log("сохранение не удалось (код %s)\n%s\n%s" % (
                result.returncode, " ".join(cmd)[:2000], result.stderr.decode("utf-8", "replace")[-3000:]))
        return ok

    def _save_clip(self, title, seconds):
        if not self.cfg["buffer_enabled"]:
            self.on_event("error", {"text": "Буфер повтора выключен — сохранять нечего."})
            return
        if not self.is_running() or not self._segments():
            self.on_event("error", {"text": "Захват ещё не запущен — в буфере пусто."})
            return
        seconds = int(seconds or self.cfg["buffer_seconds"])
        self.on_event("saving", {})
        with self._save_lock:
            closed = self._closed_segments()
            chosen, skip, length = self._tail(closed, seconds)
            if not chosen:
                self.on_event("error", {"text": "В буфере пока пусто, подождите пару секунд."})
                return
            out_path = self._output_path(title, "Клип")
            ok = self._mux(chosen, out_path, skip)
        if ok:
            self.on_event("saved", {"path": out_path, "kind": "clip",
                                    "seconds": length})
        else:
            self._remove(out_path)
            self.on_event("error", {"text": "Не получилось сохранить клип. Подробности — в журнале."})

    def _finish_recording(self, wait_next=True, exclude_prefix=None):
        first, title = self._rec_first, self._rec_title
        if first is None:
            return
        with self._save_lock:
            if self._rec_first is None:
                return
            closed = self._closed_segments(wait_next)
            chosen = [p for p in closed if os.path.basename(p) >= first
                      and not (exclude_prefix and os.path.basename(p).startswith(exclude_prefix))]
            self._rec_first = None
            self.recording_since = None
            ok = False
            out_path = ""
            if chosen:
                self.on_event("saving", {})
                out_path = self._output_path(title, "Запись")
                ok = self._mux(chosen, out_path)
        self.on_event("recording", {"on": False})
        self._emit_state()
        if ok:
            self.on_event("saved", {"path": out_path, "kind": "recording",
                                    "seconds": len(chosen) * SEGMENT})
        else:
            if out_path:
                self._remove(out_path)
            self.on_event("error", {"text": "Не получилось сохранить запись. Подробности — в журнале."})

    # ------------------------------------------------------------- события

    def _set_state(self, state, message):
        self.state, self.message = state, message
        self._emit_state()

    def _emit_state(self):
        self.on_event("state", self.snapshot())
