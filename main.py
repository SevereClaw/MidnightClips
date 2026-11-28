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
from midnightclips import syv
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
        self._overlay_jobs = set()   # отправки, начатые из меню поверх игры: их итог — уведомлением
        self.updater = Updater(data_dir(), lambda kind, data: self.push(kind, data))
        self.hotkeys = Hotkeys()
        self.toast = Toast()
        self.library = Library(self.cfg, self.ffmpeg, cache_dir())
        self.sounds = SoundBank(self.cfg, self.ffmpeg, os.path.join(self.res, "assets"), cache_dir())
        # видео не из папки клипов, которые пользователь сам перетащил или выбрал для монтажа
        self.external = set()
        self.montage_path = os.path.join(data_dir(), "montage.json")
        for piece in self.load_montage():
            if not self.library.inside(piece["path"]):
                self.external.add(os.path.normcase(os.path.abspath(piece["path"])))
        self.media = MediaServer(self.allowed)
        self.mover = storage.Mover(self.on_move_event)
        self.syv = syv.Link()   # Steal Your Voice: мьют, эффект голоса и пресеты — в меню поверх игры
        self.tools = ClipTools(self.cfg, self.ffmpeg, os.path.join(cache_dir(), "tools"), self.on_tool_event,
                               self.log_path)
        self.recorder_encoders = lambda: getattr(self.recorder, "_encoders", None) or []
        self.recorder = Recorder(
            self.cfg, self.ffmpeg, os.path.join(cache_dir(), "buffer"), self.log_path,
            self.on_event, winutil.monitors, self.overlay_diag, find_engine(self.res), winutil.find_window,
            winutil.detect_game)
        self.tools.encoders = self.recorder_encoders   # склейка кодирует той же видеокартой, что и запись

    # ---------------------------------------------------------------- запуск

    VIDEO_EXT = (".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".ts", ".flv", ".wmv", ".mpg", ".mpeg")

    def allowed(self, path):
        """Файл можно показывать и монтировать: он в папке клипов или его добавили в монтаж сами."""
        try:
            return self.library.inside(path) or os.path.normcase(os.path.abspath(path)) in self.external
        except (TypeError, ValueError):
            return False

    def add_external(self, paths):
        """Видео с компьютера — в монтаж (без копирования). Возвращает [{path, name}] того, что подошло."""
        out = []
        for path in paths or []:
            if not path or not os.path.isfile(path) or not path.lower().endswith(self.VIDEO_EXT):
                continue
            path = os.path.abspath(path)
            if not self.library.inside(path):
                self.external.add(os.path.normcase(path))
            out.append({"path": path, "name": os.path.splitext(os.path.basename(path))[0]})
        return out

    def load_montage(self):
        try:
            with open(self.montage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return []
        out = []
        for p in data if isinstance(data, list) else []:
            try:
                out.append({"path": str(p["path"]), "name": str(p.get("name") or ""), "a": float(p.get("a") or 0),
                            "b": float(p["b"]) if p.get("b") is not None else None,
                            "tracks": [int(t) for t in p["tracks"]] if p.get("tracks") is not None else None})
            except (KeyError, TypeError, ValueError):
                continue
        return out[:200]

    def bind_file_drop(self):
        """Видео, перетащенные из Проводника в окно, — в монтаж. Полный путь к файлу даёт pywebview."""
        try:
            from webview.dom import DOMEventHandler
        except ImportError:
            return

        def on_drop(event):
            try:
                files = (event.get("dataTransfer") or {}).get("files") or []
                paths = [f.get("pywebviewFullPath") for f in files if isinstance(f, dict)]
            except AttributeError:
                return
            paths = [p for p in paths if p]
            if not paths:
                return   # перетаскивание внутри окна (клип на дорожку) — его ведёт интерфейс
            items = self.add_external(paths)
            if items:
                self.push("files_dropped", {"items": items})
            else:
                self.push("warning", {"text": "Это не видео — в монтаж можно добавить mp4, mkv, mov, webm и похожие файлы."})

        try:
            self.window.events.loaded.wait(30)   # обработчик вешается на уже загруженную страницу
            self.window.dom.document.events.drop += DOMEventHandler(on_drop, True, True)
        except Exception as exc:
            self.recorder._log("перетаскивание файлов недоступно: %s" % exc)

    def hwnd(self):
        """Окно программы для Windows (своя полоса заголовка двигает и растягивает его через систему)."""
        if not getattr(self, "_hwnd", None):
            self._hwnd = winutil.app_window("MidnightClips")
            winutil.prepare_frameless(self._hwnd)
        return self._hwnd

    def on_start(self):
        """Вызывается, когда окно готово."""
        threading.Thread(target=self.hwnd, daemon=True).start()
        threading.Thread(target=self.bind_file_drop, daemon=True).start()
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
        self.hotkeys.bind("marker", self.cfg["hotkey_marker"], self.add_marker)
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

    def add_marker(self):
        """Отметка во время игры: клип не сохраняется, а в будущем клипе или записи это место будет видно на шкале."""
        if self.recorder.add_marker():
            self.sound("marker")
            if self.cfg["toast"]:
                self.toast.show("Отметка поставлена", "Она будет на шкале клипа или записи")
        else:
            self.sound("error")
            if self.cfg["toast"]:
                self.toast.show("Отметку не поставить", "Захват не идёт или клипы выключены")

    def attach_voices(self, path, span):
        """Фразы друзей из Discord за время клипа — в клип (под шкалой видно, кто где говорил)."""
        start, end = span
        reply = syv.call("voices", timeout=3.0, since=start, until=end)
        voices = (reply or {}).get("voices") or []
        out = []
        for v in voices:
            try:
                s, e = max(0.0, float(v["s"]) - start), min(end - start, float(v["e"]) - start)
            except (KeyError, TypeError, ValueError):
                continue
            if e - s >= 0.05:
                out.append({"name": v.get("name") or "Друг", "s": s, "e": e})
        if out:
            self.library.set_voices(path, out)
            self.push("clip_meta", {"path": path})

    def gate_for(self, path):
        """Моменты, когда звук Discord в клипе молчит (выключенные друзья): {номер дорожки Discord: [(с, по)]}."""
        mark = self.library.marks_of(path)
        off = set(mark.get("voices_off") or [])
        spans = [(v["s"], v["e"]) for v in mark.get("voices") or [] if v.get("name") in off]
        if not spans or not self.ffmpeg:
            return None
        names = [n.lower() for n in clip_tools.audio_tracks(self.ffmpeg, path)]
        if "discord" not in names[1:]:
            return None
        return {names.index("discord", 1): spans}

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
            self.overlay_page, self.overlay_gpage, self.overlay_sel = "main", 0, None
            threading.Thread(target=self.refresh_heard_apps, daemon=True, name="heard-apps").start()
            self.syv.refresh()
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
            facts["clips"] = clips
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
            {"id": "settings", "title": "Настройки", "active": True, "sub": "Быстрые настройки прямо здесь", "icon": "gear"},
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
        rows = []
        sounds = self.overlay_sounds()
        if sounds:
            rows.append({"label": "ЗВУК В ЗАПИСИ", "items": sounds})
        voice = self.overlay_voice()
        if voice:
            rows.append({"label": "STEAL YOUR VOICE", "items": voice})
            hint += "   ·   ПКМ по пресету — спрятать"
        model = {"status": status, "recording": recording, "tiles": tiles, "chips": chips, "last": last_text, "hint": hint,
                 "rows": rows, "page": "main"}
        page = getattr(self, "overlay_page", "main")
        if page == "settings":
            model.update(self.overlay_settings_page(), page="settings", back=True)
        elif page == "gallery":
            model.update(self.overlay_gallery_page(facts.get("clips") or []), page="gallery", back=True)
        return model

    # ---- свои разделы меню поверх игры: настройки и галерея прямо поверх игры, без окна программы

    def syv_hidden(self):
        try:
            data = json.loads(self.cfg["syv_hidden_presets"] or "[]")
            return [str(x) for x in data] if isinstance(data, list) else []
        except (ValueError, TypeError):
            return []

    def overlay_settings_page(self):
        cfg = self.cfg

        def span(v):
            return "%d мин" % (v // 60) if v >= 60 and v % 60 == 0 else "%d с" % v

        def switch(key, title, on_sub, off_sub, extra=None):
            on = bool(cfg[key])
            tile = {"id": "set:" + key, "title": title, "active": on, "switch": on, "sub": on_sub if on else off_sub}
            tile.update(extra or {})
            return tile
        tiles = [
            {"id": "set:buffer_seconds", "title": "Длина клипа", "active": True, "value": span(int(cfg["buffer_seconds"])),
             "sub": "Сколько последних секунд сохраняет клип. Нажмите — дальше"},
            {"id": "set:fps", "title": "Кадров в секунду", "active": True, "value": str(int(cfg["fps"])),
             "sub": "30 / 60 / 120. Перезапустит захват — буфер начнётся заново"},
            switch("audio", "Звук компьютера", "Игра и программы попадают в запись", "Запись без звука компьютера"),
            switch("mic_on", "Микрофон в записи", "Голос попадает в клипы", "Голос не записывается")
            if cfg["mic"] else {"id": "set:none", "title": "Микрофон в записи", "active": False, "sub": "Устройство не выбрано в настройках программы"},
            switch("cursor", "Курсор в записи", "Курсор мыши виден в клипах", "Курсор не записывается"),
            switch("indicator", "Точка записи", "Красная точка в углу, пока идёт запись", "Без точки"),
            switch("toast", "Уведомления", "Сообщение в углу при сохранении", "Без сообщений"),
            switch("sound", "Звуковые сигналы", "Сигнал при сохранении и записи", "Без звуков"),
        ]
        state = self.syv.state
        presets = [p for p in ((state or {}).get("presets") or [])]
        if presets:
            current = cfg["syv_preset_record"]
            tiles.append({"id": "set:syv_preset_record", "title": "Голос на запись", "active": bool(current),
                          "value": current or "не менять",
                          "sub": "Пресет Steal Your Voice на время записи. Нажмите — следующий"})
        else:
            tiles.append(switch("separate_tracks", "Отдельные дорожки", "Микрофон, Discord, игра — отдельно в клипе",
                                "Всё одним звуком"))
        rows = []
        hidden = self.syv_hidden()
        if hidden:
            rows.append({"label": "СПРЯТАННЫЕ ПРЕСЕТЫ", "items": [{"id": "unhide:" + n, "name": "↺ " + n} for n in hidden[:8]]})
        rows.append({"label": "ЕЩЁ", "items": [{"id": "open:settings", "name": "Все настройки — в программе"},
                                                {"id": "page:main", "name": "← Назад   Esc"}]})
        return {"tiles": tiles, "rows": rows,
                "hint": "Нажатие — переключить или следующее значение.   Esc — назад, %s — закрыть" % (self.cfg["hotkey_overlay"] or "Esc")}

    def overlay_gallery_page(self, clips):
        per = 6
        pages = max(1, (len(clips) + per - 1) // per)
        self.overlay_gpage = max(0, min(getattr(self, "overlay_gpage", 0), pages - 1))
        chunk = clips[self.overlay_gpage * per:(self.overlay_gpage + 1) * per]
        if not getattr(self, "overlay_sel", None) or not any(c["path"] == self.overlay_sel for c in clips):
            self.overlay_sel = chunk[0]["path"] if chunk else None
        tiles = []
        missing = []
        for clip in chunk:
            ago = max(0, int(time.time() - clip["mtime"]))
            when = "только что" if ago < 60 else "%d мин назад" % (ago // 60) if ago < 3600 else \
                "%d ч назад" % (ago // 3600) if ago < 86400 else "%d дн назад" % (ago // 86400)
            dur = clip.get("duration")
            length = "%d:%02d" % (int(dur) // 60, int(dur) % 60) if dur else ""
            jpg = self.library.thumb_file(clip["path"])
            if not jpg:
                missing.append(clip["path"])
            tiles.append({"id": "gal:" + clip["path"], "title": clip["name"][:60], "image": jpg or "",
                          "sub": "  ·  ".join(x for x in (when, length, "%.0f МБ" % (clip["size"] / 1048576)) if x),
                          "active": clip["path"] == self.overlay_sel, "key": "★" if clip.get("favorite") else ""})
        if missing and not getattr(self, "_thumbs_busy", False):   # превью делаются в фоне, меню не ждёт
            self._thumbs_busy = True

            def make(paths):
                try:
                    for path in paths:
                        self.library.thumb(path)
                finally:
                    self._thumbs_busy = False
            threading.Thread(target=make, args=(missing,), daemon=True).start()
        sel = next((c for c in clips if c["path"] == self.overlay_sel), None)
        rows = []
        if sel:
            items = [{"id": "gact:copy", "name": "Скопировать — Ctrl+V в Discord"}]
            chans = clip_tools.channels(self.cfg)
            if len(chans) == 1:
                items.append({"id": "gact:send:0", "name": "Отправить в Discord"})
            for i, ch in enumerate(chans if len(chans) > 1 else []):   # несколько каналов — по пункту на канал
                items.append({"id": "gact:send:%d" % i, "name": "В Discord → " + ch["name"][:24]})
            items.append({"id": "gact:fav", "name": "★ Убрать из избранного" if sel.get("favorite") else "★ В избранное"})
            items.append({"id": "gact:open", "name": "Открыть в программе"})
            rows.append({"label": "ВЫБРАН: " + sel["name"][:28].upper(), "items": items})
        rows.append({"label": "СТРАНИЦА %d ИЗ %d" % (self.overlay_gpage + 1, pages),
                     "items": [{"id": "gal:prev", "name": "◀  Новее"}, {"id": "gal:next", "name": "Старее  ▶"},
                               {"id": "page:main", "name": "← Назад   Esc"}]})
        return {"tiles": tiles, "rows": rows,
                "hint": "Нажмите на клип, чтобы выбрать.   ПКМ по клипу — в избранное.   Esc — назад" if clips
                else "Клипов пока нет.   Esc — назад"}

    def overlay_setting(self, key):
        """Переключить настройку из меню поверх игры (переключатель или следующее значение)."""
        cfg = self.cfg

        def after(values, current):
            values = list(values)
            return values[(values.index(current) + 1) % len(values)] if current in values else values[0]
        if key == "buffer_seconds":
            self.apply_settings({key: after([15, 30, 60, 120, 300, 600], int(cfg[key]))})
        elif key == "fps":
            self.apply_settings({key: after([30, 60, 120], int(cfg[key]))})
        elif key == "syv_preset_record":
            names = [""] + [p for p in ((self.syv.state or {}).get("presets") or [])]
            self.apply_settings({key: after(names, cfg[key])})
        elif key in ("audio", "mic_on", "cursor", "indicator", "toast", "sound", "separate_tracks"):
            if key == "mic_on" and not cfg["mic"]:
                return
            self.apply_settings({key: not cfg[key]})

    def overlay_gallery_action(self, what):
        path = getattr(self, "overlay_sel", None)
        if not path or not os.path.isfile(path):
            return False
        name = os.path.splitext(os.path.basename(path))[0]
        if what == "copy":
            if winutil.copy_file_to_clipboard(path):
                self.toast.show("Клип скопирован", "Вставьте в Discord: Ctrl+V")
                return True
        elif what == "send" or what.startswith("send:"):
            chans = clip_tools.channels(self.cfg)
            try:
                ch = chans[int(what.split(":", 1)[1])] if ":" in what else clip_tools.pick_channel(self.cfg)
            except (ValueError, IndexError):
                ch = None
            if not ch:
                self.toast.show("Нет канала", "Добавьте канал: Настройки → Discord")
                return False
            job = self.tools.start(path, "send", channel=ch["url"])
            if job:
                self._overlay_jobs.add(job)   # результат — уведомлением, меню можно закрыть
                self.toast.show("Отправляю в «%s»" % ch["name"], name)
                return True
        elif what == "fav":
            clip = next((c for c in (self.overlay_facts().get("clips") or []) if c["path"] == path), None)
            self.library.set_favorite(path, not (clip or {}).get("favorite"))
            self._overlay_facts = None   # перечитать — звёздочка сразу на месте
            self.push("clip_meta", {"path": path})
        elif what == "open":
            self.show_window()
            self.push("open_clip", {"path": path})
            return True
        return False

    def overlay_voice(self):
        """Строка Steal Your Voice в меню: мьют микрофона, эффект голоса и пресеты (если панель запущена)."""
        if time.time() - self.syv.checked > 2:
            self.syv.refresh()   # в фоне: меню берёт последнее известное состояние и не ждёт
        state = self.syv.state
        if not state or not state.get("connected"):
            return []
        voice = str(state.get("fxVoice") or "").strip()
        items = [{"id": "syv:mute", "name": "Микрофон везде", "on": not state.get("muted")},
                 {"id": "syv:fx", "name": "Голос: " + (voice.capitalize() if voice else "эффект"), "on": bool(state.get("fx"))}]
        active = str(state.get("active") or "")
        hidden = {h.lower() for h in self.syv_hidden()}
        for name in [p for p in (state.get("presets") or []) if p.lower() not in hidden][:8]:
            items.append({"id": "syv:preset:" + name, "name": name.capitalize() if name.isupper() else name,
                          "active": name.lower() == active.lower()})
        return items

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
        if ident == "syv:mute":
            self.syv.send("mute")
            return False
        if ident == "syv:fx":
            self.syv.send("fx")
            return False
        if ident.startswith("syv:preset:"):
            self.syv.send("preset", name=ident[len("syv:preset:"):])
            return False
        if ident.startswith("ctx:"):   # правая кнопка
            target = ident[4:]
            if target.startswith("syv:preset:"):   # спрятать пресет из меню (вернуть — в «Настройках» меню)
                name = target[len("syv:preset:"):]
                hidden = self.syv_hidden()
                if name not in hidden:
                    self.apply_settings({"syv_hidden_presets": json.dumps(hidden + [name], ensure_ascii=False)})
            elif target.startswith("gal:") and len(target) > 4 and target[4:] not in ("prev", "next"):
                self.overlay_sel = target[4:]
                self.overlay_gallery_action("fav")
            return False
        if ident.startswith("page:"):
            self.overlay_page = ident[5:]
            if self.overlay_page == "gallery":
                self._overlay_facts = None   # свежий список клипов
            return False
        if ident.startswith("set:"):
            self.overlay_setting(ident[4:])
            return False
        if ident.startswith("unhide:"):
            self.apply_settings({"syv_hidden_presets": json.dumps([h for h in self.syv_hidden() if h != ident[7:]],
                                                                  ensure_ascii=False)})
            return False
        if ident == "open:settings":
            self.show_window()
            self.push("open", {"view": "settings"})
            return True
        if ident in ("gal:prev", "gal:next"):
            self.overlay_gpage = max(0, getattr(self, "overlay_gpage", 0) + (1 if ident == "gal:next" else -1))
            self.overlay_sel = None
            return False
        if ident.startswith("gal:"):
            self.overlay_sel = ident[4:]
            return False
        if ident.startswith("gact:"):
            return self.overlay_gallery_action(ident[5:])
        if ident == "copy":
            last = self.overlay_facts()["last"]
            if last and os.path.isfile(last["path"]) and winutil.copy_file_to_clipboard(last["path"]):
                self.toast.show("Клип скопирован", "Вставьте в Discord: Ctrl+V")
                return True
            return False
        if ident in ("clips", "settings"):   # свои разделы меню — прямо поверх игры
            self.overlay_page = "gallery" if ident == "clips" else "settings"
            if ident == "clips":
                self._overlay_facts = None
            return False
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
            if payload.get("markers"):
                self.library.set_markers(payload["path"], payload["markers"])
            if payload.get("span"):   # кто говорил в Discord за время клипа — у Steal Your Voice
                threading.Thread(target=self.attach_voices, args=(payload["path"], payload["span"]), daemon=True).start()
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
            if self.cfg["syv_preset_record"]:   # пресет Steal Your Voice на время записи и обратно
                if payload.get("on"):
                    threading.Thread(target=self.syv.send, args=("preset",), kwargs={"name": self.cfg["syv_preset_record"], "temp": True},
                                     daemon=True).start()
                else:
                    threading.Thread(target=self.syv.send, args=("restore",), daemon=True).start()
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
        # Итог отправки — уведомлением, если её начали из меню поверх игры или окно программы спрятано в трей.
        if kind == "job_done" and (payload.get("id") in self._overlay_jobs
                                   or (payload.get("action") == "send" and not self.visible)):
            self._overlay_jobs.discard(payload.get("id"))
            self.toast.show(("Discord" if payload.get("ok") else "Не отправилось") + ": " + payload.get("name", "")[:40],
                            payload.get("text", ""))
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
        if changed & {"hotkey_clip", "hotkey_record", "hotkey_overlay", "hotkey_clip_a", "hotkey_clip_b", "hotkey_marker", "buffer_enabled"}:
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
        return self._app.library.thumb(path, force=self._app.allowed(path))

    def media_url(self, path):
        """Адрес клипа для встроенного плеера."""
        if self._app.allowed(path) and os.path.isfile(path):
            return self._app.media.url(path)
        return ""

    def clip_info(self, path):
        """Длительность и размер кадра — для дорожки монтажа."""
        if not (self._app.allowed(path) and os.path.isfile(path) and self._app.ffmpeg):
            return None
        try:
            info = clip_tools.media_info(self._app.ffmpeg, path)
        except clip_tools.Fail:
            return None
        info["name"] = os.path.splitext(os.path.basename(path))[0]
        return info

    def get_montage(self):
        return self._app.load_montage()

    def save_montage(self, pieces):
        """Монтаж сохраняется между запусками программы."""
        clean = []
        for p in list(pieces or [])[:200]:
            try:
                path = str(p["path"])
                if not self._app.allowed(path):
                    continue
                clean.append({"path": path, "name": str(p.get("name") or ""), "a": round(float(p.get("a") or 0), 3),
                              "b": round(float(p["b"]), 3) if p.get("b") is not None else None,
                              "tracks": [int(t) for t in p["tracks"]] if p.get("tracks") is not None else None})
            except (KeyError, TypeError, ValueError):
                continue
        try:
            tmp = self._app.montage_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(clean, f, ensure_ascii=False)
            os.replace(tmp, self._app.montage_path)
        except OSError:
            return False
        return True

    def add_videos(self):
        """«Добавить видео…»: выбрать файлы на компьютере (их можно и просто перетащить в окно)."""
        import webview
        types = ("Видео (*.mp4;*.mkv;*.mov;*.webm;*.avi;*.m4v;*.ts;*.flv;*.wmv)", "Все файлы (*.*)")
        try:
            result = self._app.window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True, file_types=types)
        except Exception:
            try:
                result = self._app.window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True)
            except Exception:
                result = None
        return self._app.add_external(list(result or []))

    def clip_tracks(self, path):
        """Звуковые дорожки клипа — для галочек в окне клипа."""
        if not (self._app.library.inside(path) and os.path.isfile(path) and self._app.ffmpeg):
            return []
        return clip_tools.audio_tracks(self._app.ffmpeg, path)

    # ---- своя полоса заголовка
    def win_state(self):
        return {"maximized": winutil.is_maximized(self._app.hwnd())}

    def win_drag(self, edge="move"):
        return winutil.begin_drag(self._app.hwnd(), edge, min_size=(940, 620))

    def win_minimize(self):
        winutil.minimize(self._app.hwnd())
        return True

    def win_maximize(self):
        return {"maximized": winutil.toggle_maximize(self._app.hwnd())}

    def win_close(self):
        """Крестик: как у обычного окна — в трей (если он есть), иначе выход."""
        app = self._app
        if app.tray is not None and not app.quitting:
            try:
                app.window.hide()
                app.visible = False
                return True
            except Exception:
                pass
        threading.Thread(target=app.quit, daemon=True).start()
        return True

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

    def clip_action(self, path, action, start=None, end=None, tracks=None, segments=None, channel=None):
        """action: trim — сохранить обрезку, compress — сжать под лимит, send — отправить в Discord.
        segments — оставленные куски [[начало, конец], ...]; если их несколько, они склеиваются.
        channel — ссылка вебхука канала (из настроек). Возвращает номер работы в очереди или 0."""
        if not (self._app.library.inside(path) and os.path.isfile(path)):
            return 0
        if action == "send" and channel and channel != self._app.cfg["discord_channel"]:
            self._app.cfg.update({"discord_channel": str(channel)})   # в следующий раз — тот же канал
        try:
            segments = [[float(a), float(b)] for a, b in (segments or [])][:80]
        except (TypeError, ValueError):
            segments = []
        return self._app.tools.start(path, action, start, end, tracks, segments, self._app.gate_for(path), channel)

    def send_clips(self, paths, channel=None):
        """Отправить несколько клипов целиком в один канал — каждый встаёт в очередь. [{id, path}]."""
        jobs = []
        for path in list(paths or [])[:50]:
            job = self.clip_action(str(path), "send", channel=channel)
            if job:
                jobs.append({"id": job, "path": str(path)})
        return jobs

    def clip_jobs(self):
        """Идущие и ждущие работы (для панели отправок после перезагрузки окна)."""
        return self._app.tools.jobs()

    def webhook_name(self, url):
        """Имя вебхука — подсказка для названия канала в настройках."""
        return clip_tools.webhook_name(str(url or ""))

    def clip_strip(self, path):
        """Полоса кадров для шкалы в окне клипа и для кусков на дорожке монтажа."""
        if not (self._app.allowed(path) and os.path.isfile(path) and self._app.ffmpeg):
            return None
        return clip_tools.film_strip(self._app.ffmpeg, path, os.path.join(cache_dir(), "strips"))

    def montage(self, pieces):
        """Склеить куски разных клипов в один ролик. pieces = [{path, start, end, tracks}]. Ролик ложится
        в папку первого клипа под именем «Монтаж <дата>»."""
        clean = []
        for p in list(pieces or [])[:80]:
            try:
                path = str(p["path"])
                if not (self._app.allowed(path) and os.path.isfile(path)):
                    return False
                tracks = p.get("tracks")
                clean.append({"path": path, "start": float(p["start"]), "end": float(p["end"]),
                              "tracks": [int(t) for t in tracks] if tracks is not None else None,
                              "gate": self._app.gate_for(path) if self._app.library.inside(path) else None})
            except (KeyError, TypeError, ValueError):
                return False
        if not clean:
            return False
        # ролик — рядом с первым клипом; если первый кусок — видео с компьютера, то в папку клипов
        first = clean[0]["path"]
        folder = os.path.dirname(first) if self._app.library.inside(first) else self._app.cfg.clips_dir()
        out = os.path.join(folder, "Монтаж %s.mp4" % time.strftime("%Y-%m-%d %H-%M-%S"))
        return self._app.tools.montage(clean, clip_tools.unique(out))

    def cancel_job(self, job_id=None):
        return self._app.tools.cancel(int(job_id) if job_id else None)

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

    def syv_state(self):
        """Состояние Steal Your Voice (для настроек): запущена ли панель и какие в ней пресеты."""
        self._app.syv.refresh(wait=True)
        return self._app.syv.state

    def set_voices_off(self, path, names):
        """Выключить в клипе друзей из Discord (их фразы приглушаются)."""
        return self._app.library.set_voices_off(path, names)

    def set_markers(self, path, seconds):
        """Отметки в клипе — из окна клипа (поставить, убрать)."""
        return self._app.library.set_markers(path, seconds)

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
        hidden=minimized, frameless=True, easy_drag=False)   # полоса заголовка — своя, в стиле программы
    app.window.events.closing += app.on_closing
    webview.start(app.on_start)
    app.shutdown()
    os._exit(0)  # фоновые потоки (трей, уведомления) не должны держать процесс


if __name__ == "__main__":
    main()
