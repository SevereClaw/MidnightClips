"""Буфер повтора и запись на базе ffmpeg.

ffmpeg непрерывно пишет экран маленькими кусочками (.ts по 2 секунды) во
временную папку; старые кусочки удаляются. «Сохранить клип» склеивает
последние N секунд в mp4 без перекодирования — поэтому это мгновенно.
Обычная запись (старт/стоп) — те же кусочки, только без удаления.
"""
import glob
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime

from . import audio as audio_mod

SEGMENT = 2  # секунд в одном кусочке
CONCAT_LIMIT = 24000  # длиннее — склеиваем через общий файл
TEST = bool(os.environ.get("MC_TEST_SOURCE"))  # проверка без Windows
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

HW_ENCODERS = ["h264_nvenc", "h264_amf", "h264_qsv"]
ENCODER_TITLES = {
    "h264_nvenc": "NVIDIA NVENC",
    "h264_amf": "AMD AMF",
    "h264_qsv": "Intel Quick Sync",
    "libx264": "процессор (x264)",
}
CAPTURE_TITLES = {"dda": "Desktop Duplication", "gdi": "GDI", "test": "тест"}


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


def safe_name(text, fallback="Клип"):
    cleaned = "".join(c for c in text if c not in '<>:"/\\|?*' and ord(c) >= 32).strip(" .")
    return cleaned[:60] or fallback


class Recorder:
    def __init__(self, cfg, ffmpeg, work_dir, log_path, on_event, monitors=None):
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
        self._good = None                 # последний сработавший вариант захвата

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

    def restart(self):
        """Перезапустить захват с новыми настройками."""
        if self._thread and self._thread.is_alive():
            self._good = None
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
        while not self._stop.is_set():
            self._restart.clear()
            self._set_state("starting", "Включаю захват…")
            if self._encoders is None:
                self._encoders = self._detect_encoders()
            session = self._launch_any()
            if session is None:
                self._set_state(
                    "error",
                    "Не удалось запустить захват экрана. Подробности — в журнале (Настройки → Журнал).",
                )
                self._wait(15)
                continue
            proc, source, pump, started = session
            self._set_state("on", "")
            reason = ""
            while not self._stop.is_set() and not self._restart.is_set():
                if proc.poll() is not None:
                    reason = "ffmpeg остановился, перезапускаю захват"
                    break
                if source is not None and not source.alive():
                    reason = "устройство звука сменилось, перезапускаю захват"
                    break
                if time.time() - started > 20 * 3600 and self.recording_since is None:
                    break  # плановый перезапуск: метки времени в .ts не бесконечны
                self._prune()
                self._wait(1)
            if self.recording_since is not None:
                self._finish_recording(wait_next=False)
            self._shutdown(proc, source, pump)
            with self._save_lock:
                for path in glob.glob(os.path.join(self.work_dir, "*.ts")):
                    self._remove(path)
            if reason:
                self.on_event("warning", {"text": reason.capitalize() + "."})
        self._set_state("off", "")

    def _wait(self, seconds):
        end = time.time() + seconds
        while time.time() < end and not self._stop.is_set() and not self._restart.is_set():
            time.sleep(0.1)

    # ---------------------------------------------------- запуск ffmpeg

    def _detect_encoders(self):
        if TEST:
            return ["libx264"]
        found = []
        for enc in HW_ENCODERS:
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
            if ok:
                found.append(enc)
        return found + ["libx264"]

    def _candidates(self):
        """Варианты «способ захвата + кодировщик» от лучшего к самому надёжному."""
        if TEST:
            return [("test", "libx264", False)]
        wanted = self.cfg["encoder"]
        if wanted != "auto" and wanted in self._encoders:
            encoders = [wanted] + (["libx264"] if wanted != "libx264" else [])
        else:
            encoders = list(self._encoders)
        capture = self.cfg["capture"]
        captures = [capture] if capture in ("dda", "gdi") else ["dda", "gdi"]
        result = []
        for enc in encoders:
            for cap in captures:
                if cap == "dda" and enc in ("h264_nvenc", "h264_amf") and not self.cfg["height"]:
                    result.append((cap, enc, True))   # кадры не покидают видеокарту
                result.append((cap, enc, False))
        if self._good in result:
            result.remove(self._good)
            result.insert(0, self._good)
        return result

    def _launch_any(self):
        for cand in self._candidates():
            if self._stop.is_set() or self._restart.is_set():
                return None
            session = self._launch(cand)
            if session:
                self._good = cand
                self.capture, self.encoder = cand[0], cand[1]
                return session
        return None

    def _launch(self, cand):
        capture, encoder, zero_copy = cand
        self._session += 1
        prefix = "s%04d_" % self._session
        source = None
        self.audio_note = ""
        if self.cfg["audio"]:
            source = audio_mod.FakeSource() if TEST else audio_mod.LoopbackSource()
            try:
                source.open()
            except Exception as exc:  # нет звуковой карты, нет библиотеки и т.п.
                self.audio_note = "Звук системы не записывается: %s" % exc
                source.close()
                source = None
        cmd = self._build_cmd(capture, encoder, zero_copy, source, prefix)
        try:
            log = open(self.log_path, "a", encoding="utf-8", errors="replace")
            log.write("\n=== %s | %s + %s%s ===\n%s\n" % (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"), capture, encoder,
                " (zero-copy)" if zero_copy else "", " ".join(cmd)))
            log.flush()
            proc = subprocess.Popen(
                cmd, cwd=self.work_dir, bufsize=0,
                stdin=subprocess.PIPE if source else subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=log, creationflags=NO_WINDOW,
            )
            log.close()
        except OSError as exc:
            self.message = str(exc)
            if source:
                source.close()
            return None
        pump = None
        if source:
            pump = audio_mod.AudioPump(source, proc.stdin)
            pump.start()
        # Успех = появился первый кусочек с данными.
        deadline = time.time() + 12
        pattern = os.path.join(self.work_dir, prefix + "*.ts")
        while time.time() < deadline and proc.poll() is None and not self._stop.is_set():
            files = glob.glob(pattern)
            if files and max(os.path.getsize(f) for f in files) > 0:
                self._proc = proc
                return proc, source, pump, time.time()
            time.sleep(0.2)
        self._shutdown(proc, source, pump)
        for path in glob.glob(pattern):
            self._remove(path)
        return None

    def _build_cmd(self, capture, encoder, zero_copy, source, prefix):
        cfg = self.cfg
        fps = max(15, min(240, int(cfg["fps"])))
        height = int(cfg["height"])
        mbps = max(2, min(100, int(cfg["bitrate_mbps"])))
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostats", "-y"]

        # --- вход 0: экран
        if capture == "test":
            cmd += ["-re", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=%d" % fps]
        elif capture == "dda":
            graph = "ddagrab=output_idx=%d:framerate=%d:draw_mouse=1" % (int(cfg["monitor"]), fps)
            if not zero_copy:
                graph += ",hwdownload,format=bgra"
            cmd += ["-f", "lavfi", "-i", graph]
        else:
            cmd += ["-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", "1"]
            mons = self.monitors()
            idx = int(cfg["monitor"])
            if 0 <= idx < len(mons):
                m = mons[idx]
                cmd += ["-offset_x", str(m["x"]), "-offset_y", str(m["y"]),
                        "-video_size", "%dx%d" % (m["w"], m["h"])]
            cmd += ["-i", "desktop"]

        # --- входы звука
        audio_inputs = []
        if source:
            cmd += ["-f", "s16le", "-ar", str(source.rate), "-ac", str(source.channels),
                    "-thread_queue_size", "4096"]
            offset = int(cfg["audio_offset_ms"])
            if offset:
                cmd += ["-itsoffset", "%.3f" % (offset / 1000.0)]
            cmd += ["-i", "pipe:0"]
            audio_inputs.append(1)
        if cfg["mic"] and not TEST:
            cmd += ["-f", "dshow", "-thread_queue_size", "4096", "-i", "audio=" + cfg["mic"]]
            audio_inputs.append(len(audio_inputs) + 1)

        # --- видео
        if not zero_copy:
            if height:
                scale = "scale=-2:'trunc(min(ih,%d)/2)*2'" % height
            else:
                scale = "scale='trunc(iw/2)*2':'trunc(ih/2)*2'"
            pix = "yuv420p" if encoder == "libx264" else "nv12"
            cmd += ["-vf", "%s,format=%s" % (scale, pix)]
        rate = ["-b:v", "%dM" % mbps, "-maxrate", "%dM" % int(mbps * 1.5),
                "-bufsize", "%dM" % (mbps * 2), "-g", str(fps)]
        if encoder == "h264_nvenc":
            cmd += ["-c:v", encoder, "-preset", "p4", "-rc", "vbr"] + rate
        elif encoder == "h264_amf":
            cmd += ["-c:v", encoder, "-usage", "transcoding", "-quality", "balanced",
                    "-rc", "vbr_peak"] + rate
        elif encoder == "h264_qsv":
            cmd += ["-c:v", encoder, "-preset", "veryfast"] + rate
        else:
            cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency"] + rate
            cmd += ["-keyint_min", str(fps), "-sc_threshold", "0"]

        # --- звук
        if len(audio_inputs) == 2:
            cmd += ["-filter_complex",
                    "[1:a]aresample=48000[sys];[2:a]aresample=48000:async=1[mic];"
                    "[sys][mic]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]",
                    "-map", "0:v", "-map", "[a]"]
        elif audio_inputs:
            cmd += ["-map", "0:v", "-map", "1:a"]
        else:
            cmd += ["-map", "0:v", "-an"]
        if audio_inputs:
            cmd += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]

        cmd += ["-f", "segment", "-segment_time", str(SEGMENT), "-segment_format", "mpegts",
                prefix + "%06d.ts"]
        return cmd

    def _shutdown(self, proc, source, pump):
        if pump:
            pump.stop()
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

    def _segments(self):
        return sorted(glob.glob(os.path.join(self.work_dir, "s%04d_*.ts" % self._session)))

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
                self._remove(path)
        finally:
            self._save_lock.release()

    @staticmethod
    def _remove(path):
        try:
            os.remove(path)
        except OSError:
            pass

    def _closed_segments(self, wait_next=True):
        """Дождаться, пока текущий кусочек допишется, и вернуть все готовые."""
        segments = self._segments()
        if not segments:
            return []
        if not wait_next:
            return segments
        current = segments[-1]
        deadline = time.time() + SEGMENT + 3
        while time.time() < deadline:
            segments = self._segments()
            if segments and segments[-1] != current:
                return [p for p in segments if p <= current]
            if self._proc is None or self._proc.poll() is not None:
                break
            time.sleep(0.1)
        return self._segments()

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
        """Склеить кусочки в mp4 без перекодирования."""
        names = [os.path.basename(p) for p in segments]
        joined = None
        if sum(len(n) + 1 for n in names) < CONCAT_LIMIT:
            source = "concat:" + "|".join(names)
        else:  # длинная запись: командная строка Windows ограничена
            joined = os.path.join(self.work_dir, "joined_%d.tmp" % int(time.time() * 1000))
            with open(joined, "wb") as dst:
                for path in segments:
                    with open(path, "rb") as src:
                        shutil.copyfileobj(src, dst, 4 * 1024 * 1024)
            source = os.path.basename(joined)
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "mpegts", "-i", source]
        if skip > 0.05:
            cmd += ["-ss", "%.3f" % skip]
        cmd += ["-c", "copy", "-movflags", "+faststart", out_path]
        try:
            result = subprocess.run(
                cmd, cwd=self.work_dir, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, creationflags=NO_WINDOW, timeout=1800,
            )
            ok = result.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0
            if not ok:
                with open(self.log_path, "a", encoding="utf-8", errors="replace") as log:
                    log.write("\n=== сохранение не удалось ===\n%s\n%s\n" % (
                        " ".join(cmd), result.stderr.decode("utf-8", "replace")))
            return ok
        except (OSError, subprocess.SubprocessError):
            return False
        finally:
            if joined:
                self._remove(joined)

    def _save_clip(self, title, seconds):
        if self.state != "on" or not self.cfg["buffer_enabled"]:
            self.on_event("error", {"text": "Буфер повтора выключен — сохранять нечего."})
            return
        seconds = int(seconds or self.cfg["buffer_seconds"])
        self.on_event("saving", {})
        with self._save_lock:
            closed = self._closed_segments()
            chosen = closed[-(math.ceil(seconds / SEGMENT) + 1):]
            if not chosen:
                self.on_event("error", {"text": "В буфере пока пусто, подождите пару секунд."})
                return
            skip = max(0.0, len(chosen) * SEGMENT - seconds)
            out_path = self._output_path(title, "Клип")
            ok = self._mux(chosen, out_path, skip)
        if ok:
            self.on_event("saved", {"path": out_path, "kind": "clip",
                                    "seconds": min(seconds, len(chosen) * SEGMENT)})
        else:
            self._remove(out_path)
            self.on_event("error", {"text": "Не получилось сохранить клип. Подробности — в журнале."})

    def _finish_recording(self, wait_next=True):
        first, title = self._rec_first, self._rec_title
        if first is None:
            return
        with self._save_lock:
            if self._rec_first is None:
                return
            closed = self._closed_segments(wait_next)
            chosen = [p for p in closed if os.path.basename(p) >= first]
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
