"""MidnightClips — запись клипов с экрана (буфер повтора) для Windows."""
import json
import os
import shutil
import sys
import threading
import time

from midnightclips import APP_NAME, __version__, winutil
from midnightclips.config import CAPTURE_KEYS, Config, cache_dir, data_dir, default_clips_dir, resource_dir
from midnightclips.hotkeys import Hotkeys
from midnightclips.library import Library
from midnightclips.recorder import Recorder, find_engine, find_ffmpeg
from midnightclips.media import MediaServer
from midnightclips.presets import KEYS as PRESET_KEYS, VIDEO_KEYS, Presets
from midnightclips.sounds import EVENTS as SOUND_EVENTS, SoundBank
from midnightclips.toast import Toast
from midnightclips import storage
from midnightclips import tools as clip_tools
from midnightclips.tools import ClipTools
from midnightclips.updater import Updater


class App:
    def __init__(self):
        self.cfg = Config()
        self.res = resource_dir()
        self.ffmpeg = find_ffmpeg(self.res)
        self.log_path = os.path.join(data_dir(), "ffmpeg.log")
        try:  # журнал не должен расти бесконечно
            if os.path.getsize(self.log_path) > 2 * 1024 * 1024:
                os.remove(self.log_path)
        except OSError:
            pass
        self.presets = Presets(data_dir())
        self._record_switch = threading.Lock()
        if self.cfg["clip_restore"]:   # прошлый раз программу закрыли посреди записи — вернуть настройки клипов
            try:
                self.cfg.update(dict(json.loads(self.cfg["clip_restore"]), clip_restore=""))
            except (ValueError, TypeError):
                self.cfg.update({"clip_restore": ""})
        self.window = None
        self.tray = None
        self.quitting = False
        self.api = None
        self.visible = True
        self.updater = Updater(data_dir(), lambda kind, data: self.push(kind, data))
        self.hotkeys = Hotkeys()
        self.toast = Toast()
        self.library = Library(self.cfg, self.ffmpeg, cache_dir())
        self.sounds = SoundBank(self.cfg, self.ffmpeg, os.path.join(self.res, "assets"), cache_dir())
        self.media = MediaServer(self.library.inside)
        self.mover = storage.Mover(self.on_move_event)
        self.tools = ClipTools(self.cfg, self.ffmpeg, os.path.join(cache_dir(), "tools"), self.on_tool_event,
                               self.log_path)
        self.recorder = Recorder(
            self.cfg, self.ffmpeg, os.path.join(cache_dir(), "buffer"), self.log_path,
            self.on_event, winutil.monitors, self.overlay_diag, find_engine(self.res), winutil.find_window,
            winutil.detect_game)

    # ---------------------------------------------------------------- запуск

    def on_start(self):
        """Вызывается, когда окно готово."""
        self.hotkeys.start()
        self.bind_hotkeys()
        self.toast.start()
        self.start_tray()
        if not self.ffmpeg:
            self.recorder.state = "error"
            self.recorder.message = "Не найден ffmpeg.exe — положите его рядом с программой."
        else:
            self.recorder.start()
        threading.Thread(target=self.update_loop, daemon=True, name="updater").start()
        threading.Thread(target=self.cleanup_loop, daemon=True, name="cleanup").start()

    def cleanup_loop(self):
        """Автоочистка: вскоре после запуска и дальше раз в полчаса."""
        time.sleep(20)
        while not self.quitting:
            self.run_cleanup()
            time.sleep(1800)

    def run_cleanup(self):
        try:
            count, freed = self.library.cleanup(winutil.recycle)
        except Exception:
            return
        if count:
            self.push("cleaned", {"count": count, "bytes": freed})

    def update_loop(self):
        """При запуске — проверить и сразу обновиться; дальше проверять раз в 6 часов."""
        first = True
        while not self.quitting:
            if self.cfg["auto_update"]:
                ready = self.updater.check()
                if ready and first and not self.updater.already_tried() \
                        and self.recorder.recording_since is None:
                    self.apply_update()
                    return
            first = False
            time.sleep(6 * 3600)

    def apply_update(self):
        if self.recorder.recording_since is not None:
            self.push("error", {"text": "Сначала остановите запись — потом обновлюсь."})
            return False
        if not self.updater.apply("" if self.visible else "--minimized"):
            return False
        if self.cfg["toast"]:
            self.toast.show("Обновляюсь", "MidnightClips " + self.updater.latest)
            time.sleep(1.2)
        self.quit()
        return True

    def bind_hotkeys(self):
        # Клипы выключены (буфер повтора) — их клавиши освобождаются: не срабатывают и достаются другим программам.
        clips = bool(self.cfg["buffer_enabled"])
        self.hotkeys.bind("clip", self.cfg["hotkey_clip"] if clips else "", self.save_clip)
        self.hotkeys.bind("record", self.cfg["hotkey_record"], self.toggle_record)
        self.hotkeys.bind("overlay", self.cfg["hotkey_overlay"], self.toggle_overlay)
        self.hotkeys.bind("clip_a", self.cfg["hotkey_clip_a"] if clips else "",
                          lambda: self.save_clip(int(self.cfg["clip_a_seconds"])))
        self.hotkeys.bind("clip_b", self.cfg["hotkey_clip_b"] if clips else "",
                          lambda: self.save_clip(int(self.cfg["clip_b_seconds"])))
        time.sleep(0.15)  # дать потоку клавиш применить изменения
        self.push("hotkeys", dict(self.hotkeys.status))

    def start_tray(self):
        try:
            import pystray
            from PIL import Image
            image = Image.open(os.path.join(self.res, "assets", "icon.png"))
            menu = pystray.Menu(
                pystray.MenuItem("Открыть MidnightClips", lambda: self.show_window(), default=True),
                pystray.MenuItem("Сохранить клип", lambda: self.save_clip()),
                pystray.MenuItem("Начать / остановить запись", lambda: self.toggle_record()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Выход", lambda: self.quit()),
            )
            self.tray = pystray.Icon(APP_NAME, image, APP_NAME, menu)
            self.tray.run_detached()
        except Exception:
            self.tray = None  # без трея окно просто закрывает программу

    # ---------------------------------------------------------------- окно

    def show_window(self):
        try:
            self.window.show()
            self.window.restore()
            self.visible = True
        except Exception:
            pass

    def on_closing(self):
        if self.quitting or self.tray is None:
            self.shutdown()
            return True
        try:
            self.window.hide()
            self.visible = False
        except Exception:
            return True
        return False  # не закрывать: программа остаётся в трее

    def quit(self):
        self.quitting = True
        self.shutdown()
        try:
            self.window.destroy()
        except Exception:
            os._exit(0)

    def shutdown(self):
        self.quitting = True
        try:
            self.hotkeys.shutdown()
            self.recorder.stop()
            if self.tray:
                self.tray.stop()
        except Exception:
            pass

    # ------------------------------------------------------------- действия

    def save_clip(self, seconds=None):
        """Сохранить клип: весь буфер или только последние seconds секунд."""
        self.recorder.save_clip(winutil.foreground_app(), seconds)

    def toggle_record(self, title=None):
        """Начать или остановить запись. Если для записи выбран свой набор настроек,
        он включается на время записи, а после неё возвращаются настройки клипов."""
        if title is None:
            title = winutil.foreground_app()
        rec = self.recorder
        if rec.recording_since is not None:
            rec.stop_recording()
            return
        preset = self.presets.get(self.cfg["preset_record"]) if self.cfg["preset_record"] else None
        wanted = {k: preset[k] for k in VIDEO_KEYS} if preset else {}
        if not wanted or all(self.cfg[k] == v for k, v in wanted.items()) or not self.ffmpeg:
            rec.start_recording(title)
            return
        if not self._record_switch.acquire(False):
            return   # переключение уже идёт
        try:
            current = {k: self.cfg[k] for k in VIDEO_KEYS}
            self.cfg.update(dict(wanted, clip_restore=json.dumps(current)))
            self.push("settings", {})
            if self.cfg["toast"]:
                self.toast.show("Включаю набор «%s»" % preset["name"], "Запись начнётся через пару секунд")
            session = rec._session
            rec.restart()
            deadline = time.time() + 25
            while time.time() < deadline and not (rec.state == "on" and rec._session != session):
                time.sleep(0.1)
            if rec.state != "on" or not rec.start_recording(title):
                self.restore_clip_settings()   # не вышло — вернуть как было
        finally:
            self._record_switch.release()

    def restore_clip_settings(self):
        """После записи со своим набором вернуть настройки клипов."""
        saved = self.cfg["clip_restore"]
        if not saved:
            return
        try:
            values = json.loads(saved)
        except (ValueError, TypeError):
            values = {}
        changed = self.cfg.update(dict(values, clip_restore=""))
        self.push("settings", {})
        if changed & set(VIDEO_KEYS) and self.ffmpeg:
            self.recorder.restart()

    def sound(self, name):
        self.sounds.play(name)

    def overlay_diag(self):
        """Строка для журнала: работал ли «метроном» точного захвата."""
        toast = self.toast
        return "Метроном: %s, тиков %d, поток окон %s, ошибки %s" % (
            "включён" if toast.tickle_on else "выключен", toast.ticks,
            "жив" if toast.is_alive() else "не работает", toast.errors[-3:] or "нет")

    def monitor_rect(self):
        mons = winutil.monitors()
        idx = self.cfg["monitor"]
        return mons[idx] if 0 <= idx < len(mons) else (mons[0] if mons else None)

    # ------------------------------------------------ меню поверх игры (Alt+Z)

    def toggle_overlay(self):
        if not self.toast.menu_open:   # какие программы сейчас звучат — для переключателей звука в меню
            threading.Thread(target=self.refresh_heard_apps, daemon=True, name="heard-apps").start()
        self.toast.toggle_menu(self.monitor_rect(), self.overlay_model, self.overlay_action)

    def refresh_heard_apps(self):
        try:
            self._heard_apps = self.recorder.list_audio_apps()
        except Exception:
            pass

    def overlay_sounds(self):
        """Выключатели звука программ в меню: игра, программы из списка в настройках и те, что звучат сейчас."""
        cfg, rec = self.cfg, self.recorder
        if not cfg["audio"]:
            return []
        rows = rec.audio_rows()
        specific = cfg["audio_mode"] == "apps"
        own = os.path.basename(sys.executable).lower()
        out = []
        if cfg["capture_target"] in ("game", "window"):
            out.append({"id": "snd:@game", "name": "Игра", "on": rows.get("@game", (True, 100))[0]})
        listed = sorted(k for k in rows if not k.startswith("@"))
        heard = [exe for exe in sorted(getattr(self, "_heard_apps", None) or []) if exe not in rows and exe != own]
        for exe in (listed + heard)[:8]:
            on = rows[exe][0] if exe in rows else not specific   # в режиме «только отмеченные» неотмеченные не пишутся
            name = exe[:-4] if exe.endswith(".exe") else exe
            out.append({"id": "snd:" + exe, "name": name[:1].upper() + name[1:], "on": on})
        return out

    def toggle_app_sound(self, key):
        """Включить или выключить звук программы в записи (без перезапуска захвата, буфер не теряется)."""
        try:
            data = json.loads(self.cfg["app_volumes"] or "{}")
            if not isinstance(data, dict):
                data = {}
        except (ValueError, TypeError):
            data = {}
        rows = self.recorder.audio_rows()
        if key in rows:
            on, vol = rows[key]
            entry = data.get(key) if isinstance(data.get(key), dict) else {}
            entry = dict(entry, on=not on, vol=vol)
        elif key == "@game":   # игра по умолчанию пишется — выключаем
            entry = {"on": False, "vol": 100}
        else:   # программы ещё нет в списке: выключаем (а в режиме «только отмеченные» она не пишется — включаем)
            entry = {"on": self.cfg["audio_mode"] == "apps", "vol": 100}
            if self.cfg["separate_tracks"]:
                entry["menu"] = True   # без своей дорожки — набор дорожек не меняется, буфер сохраняется
        data[key] = entry
        self.apply_settings({"app_volumes": json.dumps(data, ensure_ascii=False)})

    def overlay_facts(self):
        """Последний клип, клипы за сегодня и свободное место — для меню. Папку перечитываем не чаще раза в 4 секунды."""
        now = time.time()
        cached = getattr(self, "_overlay_facts", None)
        if cached and now - cached[0] < 4:
            return cached[1]
        facts = {"last": None, "today": 0, "free_gb": None}
        try:
            clips = self.library.list()
            midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
            facts["today"] = sum(1 for c in clips if c["mtime"] >= midnight)
            facts["last"] = clips[0] if clips else None
        except Exception:
            pass
        try:
            facts["free_gb"] = shutil.disk_usage(self.cfg.clips_dir()).free / 1024 ** 3
        except OSError:
            pass
        self._overlay_facts = (now, facts)
        return facts

    def overlay_model(self):
        cfg, rec = self.cfg, self.recorder
        recording = rec.recording_since is not None
        clips_on = bool(cfg["buffer_enabled"])
        ready = rec.state == "on"
        seconds = int(cfg["buffer_seconds"])

        def span(value):
            return "%d мин" % (value // 60) if value >= 60 and value % 60 == 0 else "%d с" % value

        length = span(seconds)
        if recording:
            passed = int(time.time() - rec.recording_since)
            status = "Идёт запись  %02d:%02d" % (passed // 60, passed % 60)
        elif ready:
            status = ("Клипы включены — в буфере последние %s" % length) if clips_on else "Клипы выключены — работает только запись"
        else:
            status = rec.message or "Захват запускается…"
        mic_on = bool(cfg["mic"]) and bool(cfg["mic_on"])
        facts = self.overlay_facts()
        last = facts["last"]

        def clip_tile(ident, want, key):
            if not clips_on:
                sub = "Клипы выключены"
            elif want > seconds:
                sub = "Буфер короче — сохранится %s" % length
            else:
                sub = "Последние %s" % span(want)
            return {"id": ident, "title": "Сохранить клип" if ident == "clip" else "Клип: %s" % span(want),
                    "active": clips_on and ready, "sub": sub, "key": (key or "без клавиши") if clips_on else "",
                    "icon": "clip" if ident == "clip" else "short"}

        tiles = [
            clip_tile("clip", seconds, cfg["hotkey_clip"]),
            clip_tile("clip_a", int(cfg["clip_a_seconds"]), cfg["hotkey_clip_a"]),
            clip_tile("clip_b", int(cfg["clip_b_seconds"]), cfg["hotkey_clip_b"]),
            {"id": "record", "title": "Остановить запись" if recording else "Начать запись", "active": True, "alert": recording,
             "sub": "Идёт %02d:%02d" % divmod(int(time.time() - rec.recording_since), 60) if recording else "Пишет, пока не остановите",
             "key": cfg["hotkey_record"] or "без клавиши", "icon": "stop" if recording else "rec"},
            {"id": "buffer", "title": "Клипы", "active": clips_on, "switch": clips_on,
             "sub": "Буфер повтора работает, клавиши клипов заняты" if clips_on else "Буфер повтора остановлен, клавиши клипов свободны"},
            {"id": "mic", "title": "Микрофон", "active": mic_on, "switch": mic_on,
             "sub": "Попадает в клипы и записи" if mic_on else ("Не записывается" if cfg["mic"] else "Устройство не выбрано в настройках")},
            {"id": "copy", "title": "Скопировать", "active": bool(last), "icon": "copy",
             "sub": "Последний клип — и Ctrl+V в Discord" if last else "Клипов пока нет"},
            {"id": "clips", "title": "Галерея", "active": True, "icon": "grid",
             "sub": "Сегодня клипов: %d" % facts["today"] if facts["today"] else "Все клипы и записи"},
            {"id": "settings", "title": "Настройки", "active": True, "sub": "Открыть настройки программы", "icon": "gear"},
        ]

        snap = rec.snapshot()
        chips = []
        if snap.get("window_name") and cfg["capture_target"] in ("game", "window"):
            chips.append({"text": snap["window_name"], "accent": True})
        if ready:
            if snap.get("encoder"):
                chips.append({"text": str(snap["encoder"])})
            fps = snap.get("fps_real") or snap.get("fps_target") or int(cfg["fps"])
            height = int(cfg["height"])
            chips.append({"text": "%s%d кадров/с" % ("%dp · " % height if height else "", round(fps))})
            if clips_on:
                chips.append({"text": "Буфер ~%d МБ %s" % (snap.get("buffer_mb") or 0, "в памяти" if snap.get("buffer_in_memory") else "на диске")})
        if facts["free_gb"] is not None:
            chips.append({"text": "Свободно %d ГБ" % facts["free_gb"], "accent": facts["free_gb"] < 5})

        last_text = ""
        if last:
            ago = max(0, int(time.time() - last["mtime"]))
            when = "только что" if ago < 60 else "%d мин назад" % (ago // 60) if ago < 3600 else \
                "%d ч назад" % (ago // 3600) if ago < 86400 else "%d дн назад" % (ago // 86400)
            last_text = "%s  ·  %s  ·  %.0f МБ" % (last["name"][:48], when, last["size"] / 1048576)
        hint = "Цифры 1–9, стрелки с Enter или мышь — выбрать.   %s или Esc — закрыть" % (cfg["hotkey_overlay"] or "Esc")
        return {"status": status, "recording": recording, "tiles": tiles, "chips": chips, "last": last_text, "hint": hint,
                "sounds": self.overlay_sounds()}

    def overlay_action(self, ident):
        """Нажатие плитки. True — меню после этого закрыть."""
        if ident == "clip":
            if self.cfg["buffer_enabled"]:
                self.save_clip()
                return True
            return False
        if ident in ("clip_a", "clip_b"):
            if self.cfg["buffer_enabled"]:
                self.save_clip(int(self.cfg["%s_seconds" % ident]))
                return True
            return False
        if ident == "record":
            self.toggle_record()
            return False
        if ident == "buffer":
            self.apply_settings({"buffer_enabled": not self.cfg["buffer_enabled"]})
            return False
        if ident == "mic":
            if self.cfg["mic"]:
                self.apply_settings({"mic_on": not self.cfg["mic_on"]})
            return False
        if ident.startswith("snd:"):
            self.toggle_app_sound(ident[4:])
            return False
        if ident == "copy":
            last = self.overlay_facts()["last"]
            if last and os.path.isfile(last["path"]) and winutil.copy_file_to_clipboard(last["path"]):
                self.toast.show("Клип скопирован", "Вставьте в Discord: Ctrl+V")
                return True
            return False
        if ident in ("clips", "settings"):
            self.show_window()
            self.push("open", {"view": ident})
            return True
        return False

    def apply_settings(self, values):
        """Поменять настройки не из окна программы (меню поверх игры) и обновить окно."""
        if self.api is not None:
            self.api.set_settings(values)
            self.push("settings", {})

    def sync_overlays(self):
        """Точка записи и «метроном» точного захвата — по текущему состоянию."""
        rec = self.recorder
        rect = self.monitor_rect()
        self.toast.set_indicator(rec.recording_since is not None and self.cfg["indicator"],
                                 rect, self.cfg["indicator_corner"])
        wants = rec.state in ("starting", "on") and self.cfg["capture"] in ("auto", "dda") \
            and not rec._avoid_precise and not (rec.state == "on" and rec.capture != "dda")
        self.toast.set_tickle(wants, rect, 1000 / max(30, min(120, int(self.cfg["fps"]))))

    def on_event(self, kind, payload):
        if kind in ("state", "recording"):
            self.sync_overlays()
        if kind == "saved":
            threading.Thread(target=self.run_cleanup, daemon=True).start()
            self.sound("saved")
            if self.cfg["toast"]:
                title = "Клип сохранён" if payload.get("kind") == "clip" else "Запись сохранена"
                self.toast.show(title, os.path.basename(payload.get("path", "")))
            if self.cfg.clips_dir_missing:   # выбранный диск отключён — клип ушёл в запасную папку
                self.push("warning", {"text": "Папка %s недоступна (диск отключён?) — клип сохранён в %s" % (
                    self.cfg.clips_dir_missing, os.path.dirname(payload.get("path", "")))})
        elif kind == "recording":
            if not payload.get("on") and self.cfg["clip_restore"]:
                threading.Thread(target=self.restore_clip_settings, daemon=True).start()
            self.sound("rec_on" if payload.get("on") else "rec_off")
            if payload.get("on") and self.cfg["toast"]:
                self.toast.show("Запись идёт", "Остановить: " + (self.cfg["hotkey_record"] or "кнопкой в окне"))
        elif kind == "error":
            self.sound("error")
            if self.cfg["toast"]:
                self.toast.show("Не получилось", payload.get("text", ""))
        self.push(kind, payload)

    def on_move_event(self, kind, payload):
        if kind == "move_done":
            with self.library._lock:
                self.library._marks_dir = None   # избранное и теги переехали — перечитать
            self.sound("saved" if payload.get("ok") else "error")
        self.push(kind, payload)

    def on_tool_event(self, kind, payload):
        if kind == "job_done":
            self.sound("saved" if payload.get("ok") else "error")
        self.push(kind, payload)

    def push(self, kind, payload):
        """Отправить событие в интерфейс."""
        if self.window is None or self.quitting:
            return
        try:
            self.window.evaluate_js("window.onBackendEvent && window.onBackendEvent(%s, %s)" % (
                json.dumps(kind), json.dumps(payload, ensure_ascii=False)))
        except Exception:
            pass


class Api:
    """Методы, которые вызывает интерфейс (window.pywebview.api.*)."""

    def __init__(self, app):
        self._app = app

    def get_state(self):
        app = self._app
        state = app.recorder.snapshot()
        state["hotkeys"] = dict(app.hotkeys.status)
        state["version"] = __version__
        state["update"] = app.updater.snapshot()
        return state

    def get_settings(self):
        data = dict(self._app.cfg.data)
        data["clips_dir_resolved"] = self._app.cfg.clips_dir()
        data["clips_dir_missing"] = self._app.cfg.clips_dir_missing
        data["clips_dir_default"] = default_clips_dir()
        data["clips_free"] = storage.free_space(data["clips_dir_resolved"])
        data["moving"] = self._app.mover.busy
        return data

    def set_settings(self, values):
        app = self._app
        changed = app.cfg.update(values or {})
        if changed & {"hotkey_clip", "hotkey_record", "hotkey_overlay", "hotkey_clip_a", "hotkey_clip_b", "buffer_enabled"}:
            app.bind_hotkeys()
        if changed & {"cleanup_days", "cleanup_gb", "clips_dir"}:
            threading.Thread(target=app.run_cleanup, daemon=True).start()
        if changed & {"indicator", "indicator_corner", "monitor", "capture", "fps"}:
            app.sync_overlays()
        if "autostart" in changed:
            winutil.set_autostart(app.cfg["autostart"])
        if changed & CAPTURE_KEYS and app.ffmpeg:
            capture_changed = (changed & CAPTURE_KEYS) - {"buffer_seconds", "buffer_enabled"}   # длина буфера меняется на лету
            if app.recorder.is_running() and app.recorder._use_memory() != app.recorder.in_memory \
                    and app.recorder.recording_since is None:
                capture_changed.add("buffer_storage")   # буфер переезжает из памяти на диск или обратно
            if capture_changed:
                # громкость в записи применяется без потери буфера и идущей записи
                volume_only = capture_changed <= {"rec_volume_system", "rec_volume_mic", "app_volumes", "audio_mode", "mic_on"}
                # громкость меняется на ходу; если так нельзя (меняется набор дорожек) — мягкий перезапуск без потери буфера
                if not (volume_only and app.recorder.update_audio()):
                    app.recorder.restart(soft=volume_only)
        return self.get_settings()

    def get_options(self):
        mons = winutil.monitors()
        return {
            "monitors": ["Монитор %d — %d×%d%s" % (i + 1, m["w"], m["h"], " (основной)" if m["primary"] else "")
                         for i, m in enumerate(mons)] or ["Монитор 1"],
            "mics": self._app.library.list_mics() if self._app.ffmpeg else [],
        }

    def save_clip(self):
        self._app.recorder.save_clip("")
        return True

    def toggle_record(self):
        threading.Thread(target=self._app.toggle_record, args=("",), daemon=True).start()
        return True

    def list_windows(self):
        """Открытые окна программ — для выбора, чьё окно записывать."""
        seen, out = set(), []
        for w in sorted(winutil.list_windows(), key=lambda w: -w["area"]):
            if w["exe"] not in seen:
                seen.add(w["exe"])
                out.append({"exe": w["exe"], "title": w["title"][:60]})
        return sorted(out, key=lambda w: w["exe"])

    def list_audio_apps(self):
        return self._app.recorder.list_audio_apps()

    def get_presets(self):
        return self._app.presets.all()

    def save_preset(self, name):
        """Сохранить текущие настройки как свой набор. Возвращает текст ошибки или пустую строку."""
        values = {k: self._app.cfg[k] for k in PRESET_KEYS}
        if self._app.cfg["clip_restore"]:   # идёт запись со своим набором — берём настройки клипов
            try:
                values.update(json.loads(self._app.cfg["clip_restore"]))
            except (ValueError, TypeError):
                pass
        return self._app.presets.save(name or "", values)

    def delete_preset(self, name):
        ok = self._app.presets.delete(name)
        if ok and self._app.cfg["preset_record"] == name:
            self._app.cfg.update({"preset_record": ""})
        return ok

    def check_update(self):
        threading.Thread(target=self._app.updater.check, daemon=True).start()
        return True

    def apply_update(self):
        return self._app.apply_update()

    def restart_capture(self):
        if self._app.ffmpeg:
            self._app.recorder.restart()
        return True

    def list_clips(self):
        return self._app.library.list()

    def get_thumb(self, path):
        return self._app.library.thumb(path)

    def media_url(self, path):
        """Адрес клипа для встроенного плеера."""
        if self._app.library.inside(path) and os.path.isfile(path):
            return self._app.media.url(path)
        return ""

    def clip_tracks(self, path):
        """Звуковые дорожки клипа — для галочек в окне клипа."""
        if not (self._app.library.inside(path) and os.path.isfile(path) and self._app.ffmpeg):
            return []
        return clip_tools.audio_tracks(self._app.ffmpeg, path)

    def set_fullscreen(self, on):
        """Плеер в окне клипа развернули на весь экран (или свернули) — окно программы следует за ним."""
        on = bool(on)
        if on != getattr(self, "_fullscreen", False) and self._app.window is not None:
            self._fullscreen = on
            self._app.window.toggle_fullscreen()
        return on

    def clip_track_urls(self, path):
        """Адреса отдельных звуковых дорожек (без общей) — чтобы в окне клипа было слышно только включённые."""
        if not (self._app.library.inside(path) and os.path.isfile(path) and self._app.ffmpeg):
            return []
        files = clip_tools.split_tracks(self._app.ffmpeg, path, os.path.join(cache_dir(), "tracks"))
        return [self._app.media.allow(f) for f in files]

    def clip_action(self, path, action, start=None, end=None, tracks=None):
        """action: trim — сохранить обрезку, compress — сжать под лимит, send — отправить в Discord."""
        if not (self._app.library.inside(path) and os.path.isfile(path)):
            return False
        return self._app.tools.start(path, action, start, end, tracks)

    def cancel_job(self):
        self._app.tools.cancel()
        return True

    def open_clip(self, path):
        if self._app.library.inside(path) and os.path.isfile(path):
            winutil.open_path(path)
            return True
        return False

    def reveal_clip(self, path):
        if self._app.library.inside(path) and os.path.isfile(path):
            winutil.reveal(path)
            return True
        return False

    def copy_clip(self, path):
        if self._app.library.inside(path) and os.path.isfile(path):
            return winutil.copy_file_to_clipboard(path)
        return False

    def delete_clip(self, path):
        if self._app.library.inside(path) and os.path.isfile(path):
            ok = winutil.recycle(path)
            if ok:
                self._app.library.forget(path)
            folder = os.path.dirname(path)
            try:  # убрать опустевшую папку игры
                if ok and self._app.library.inside(folder) and not os.listdir(folder):
                    os.rmdir(folder)
            except OSError:
                pass
            return ok
        return False

    def set_favorite(self, path, value):
        return self._app.library.set_favorite(path, value)

    def set_tags(self, path, tags):
        return self._app.library.set_tags(path, tags)

    def cleanup_preview(self, days, gigabytes):
        """Сколько клипов уйдёт в корзину при таких настройках автоочистки."""
        try:
            plan = self._app.library.cleanup_plan(int(days), int(gigabytes))
        except Exception:
            plan = []
        return {"count": len(plan), "bytes": sum(c["size"] for c in plan)}

    def delete_clips(self, paths):
        """Удалить сразу несколько клипов. Возвращает, сколько удалено."""
        return sum(1 for path in list(paths or [])[:5000] if self.delete_clip(path))

    def set_favorites(self, paths, value):
        return sum(1 for path in list(paths or [])[:5000] if self._app.library.set_favorite(path, value))

    def rename_clip(self, path, new_name):
        return self._app.library.rename(path, new_name or "")

    def pick_sound(self, event):
        """Выбрать свой файл для сигнала."""
        import webview
        if event not in SOUND_EVENTS:
            return self.get_settings()
        types = ("Звук (*.wav;*.mp3;*.ogg;*.flac;*.m4a;*.opus)", "Все файлы (*.*)")
        try:
            result = self._app.window.create_file_dialog(webview.OPEN_DIALOG, file_types=types)
        except Exception:
            try:
                result = self._app.window.create_file_dialog(webview.OPEN_DIALOG)
            except Exception:
                result = None
        if result:
            path = result[0] if isinstance(result, (list, tuple)) else result
            self._app.cfg.update({"sound_" + event: path})
            self._app.sounds.play(event, force=True)
        return self.get_settings()

    def test_sound(self, event):
        self._app.sounds.play(event, force=True)
        return True

    def pick_folder(self):
        """Выбрать папку для клипов (на любом диске). Возвращает то же, что set_clips_dir, или None — если отменили."""
        import webview
        try:
            result = self._app.window.create_file_dialog(webview.FOLDER_DIALOG, directory=self._app.cfg.clips_dir())
        except Exception:
            try:
                result = self._app.window.create_file_dialog(webview.FOLDER_DIALOG)
            except Exception:
                result = None
        if not result:
            return None
        folder = result[0] if isinstance(result, (list, tuple)) else result
        return self.set_clips_dir(folder)

    def list_drives(self):
        """Диски компьютера — чтобы одним нажатием сохранять клипы на другой диск."""
        current = os.path.splitdrive(os.path.realpath(self._app.cfg.clips_dir()))[0].upper()
        out = []
        for d in winutil.drives():
            d["current"] = d["root"][:2].upper() == current
            d["target"] = storage.resolve_target(d["root"])
            out.append(d)
        return out

    def use_drive(self, root):
        """Сохранять клипы на этот диск — в папку MidnightClips в его корне."""
        return self.set_clips_dir(root)

    def use_default_folder(self):
        return self.set_clips_dir(default_clips_dir())

    def set_clips_dir(self, folder):
        """Сменить папку клипов. Ответ: {ok, error, settings, old, old_count, old_bytes} — по old_count
        интерфейс предлагает перенести уже сохранённые клипы."""
        app = self._app
        if app.mover.busy:
            return {"ok": False, "error": "Сначала дождитесь, пока закончится перенос клипов.", "settings": self.get_settings()}
        target = storage.resolve_target(folder)
        error = storage.check_writable(target)
        if error:
            return {"ok": False, "error": error, "settings": self.get_settings()}
        old = app.cfg.clips_dir()
        if storage.same(old, target):
            if app.cfg.clips_dir_missing:   # выбрали ту же папку, а она снова доступна
                app.cfg.update({"clips_dir": "" if storage.same(target, default_clips_dir()) else target})
            return {"ok": True, "error": "", "settings": self.get_settings(), "old": "", "old_count": 0, "old_bytes": 0}
        if storage.inside(old, target):
            return {"ok": False, "error": "Новая папка не может содержать старую — выберите другую.", "settings": self.get_settings()}
        app.cfg.update({"clips_dir": "" if storage.same(target, default_clips_dir()) else target})
        threading.Thread(target=app.run_cleanup, daemon=True).start()
        info = storage.summary(old, skip=target if storage.inside(target, old) else None)
        return {"ok": True, "error": "", "settings": self.get_settings(), "old": old,
                "old_count": info["count"], "old_bytes": info["bytes"]}

    def move_clips(self, old):
        """Перенести клипы из прежней папки в текущую (в фоне, ход — событиями move / move_done)."""
        app = self._app
        new = app.cfg.clips_dir()
        if not old or not os.path.isdir(old) or storage.same(old, new) or storage.inside(old, new):
            return False
        return app.mover.start(old, new)

    def cancel_move(self):
        self._app.mover.cancel()
        return True

    def open_clips_folder(self):
        winutil.open_path(self._app.cfg.clips_dir())
        return True

    def open_log(self):
        path = self._app.log_path
        if not os.path.isfile(path):
            open(path, "a").close()
        winutil.open_path(path)
        return True


def main():
    winutil.set_dpi_aware()
    winutil.precise_timers()
    if not winutil.single_instance():
        winutil.message_box("MidnightClips уже запущен — значок в трее, возле часов.")
        return
    import webview

    app = App()
    if app.cfg["autostart"]:
        winutil.set_autostart(True)  # обновить путь, если программу перенесли
    minimized = "--minimized" in sys.argv
    app.visible = not minimized
    app.api = Api(app)
    app.window = webview.create_window(
        "MidnightClips", os.path.join(app.res, "ui", "index.html"), js_api=app.api,
        width=1180, height=760, min_size=(940, 620), background_color="#FFE100",
        hidden=minimized)
    app.window.events.closing += app.on_closing
    webview.start(app.on_start)
    app.shutdown()
    os._exit(0)  # фоновые потоки (трей, уведомления) не должны держать процесс


if __name__ == "__main__":
    main()
