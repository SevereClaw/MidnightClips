"""MidnightClips — запись клипов с экрана (буфер повтора) для Windows."""
import json
import os
import sys
import threading
import time

from midnightclips import APP_NAME, __version__, winutil
from midnightclips.config import CAPTURE_KEYS, Config, cache_dir, data_dir, resource_dir
from midnightclips.hotkeys import Hotkeys
from midnightclips.library import Library
from midnightclips.recorder import Recorder, find_ffmpeg
from midnightclips.media import MediaServer
from midnightclips.sounds import EVENTS as SOUND_EVENTS, SoundBank
from midnightclips.toast import Toast
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
        self.window = None
        self.tray = None
        self.quitting = False
        self.visible = True
        self.updater = Updater(data_dir(), lambda kind, data: self.push(kind, data))
        self.hotkeys = Hotkeys()
        self.toast = Toast()
        self.library = Library(self.cfg, self.ffmpeg, cache_dir())
        self.sounds = SoundBank(self.cfg, self.ffmpeg, os.path.join(self.res, "assets"), cache_dir())
        self.media = MediaServer(self.library.inside)
        self.tools = ClipTools(self.cfg, self.ffmpeg, os.path.join(cache_dir(), "tools"), self.on_tool_event)
        self.recorder = Recorder(
            self.cfg, self.ffmpeg, os.path.join(cache_dir(), "buffer"), self.log_path,
            self.on_event, winutil.monitors)

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
        self.hotkeys.bind("clip", self.cfg["hotkey_clip"], self.save_clip)
        self.hotkeys.bind("record", self.cfg["hotkey_record"], self.toggle_record)
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

    def save_clip(self):
        self.recorder.save_clip(winutil.foreground_app())

    def toggle_record(self):
        self.recorder.toggle_recording(winutil.foreground_app())

    def sound(self, name):
        self.sounds.play(name)

    def on_event(self, kind, payload):
        if kind == "saved":
            self.sound("saved")
            if self.cfg["toast"]:
                title = "Клип сохранён" if payload.get("kind") == "clip" else "Запись сохранена"
                self.toast.show(title, os.path.basename(payload.get("path", "")))
        elif kind == "recording":
            self.sound("rec_on" if payload.get("on") else "rec_off")
            if payload.get("on") and self.cfg["toast"]:
                self.toast.show("Запись идёт", "Остановить: " + (self.cfg["hotkey_record"] or "кнопкой в окне"))
        elif kind == "error":
            self.sound("error")
            if self.cfg["toast"]:
                self.toast.show("Не получилось", payload.get("text", ""))
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
        return data

    def set_settings(self, values):
        app = self._app
        changed = app.cfg.update(values or {})
        if changed & {"hotkey_clip", "hotkey_record"}:
            app.bind_hotkeys()
        if "autostart" in changed:
            winutil.set_autostart(app.cfg["autostart"])
        if changed & CAPTURE_KEYS and app.ffmpeg:
            if changed - {"buffer_seconds", "buffer_enabled"}:
                app.recorder.restart()   # длину буфера можно менять на лету
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
        self._app.recorder.toggle_recording("")
        return True

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

    def clip_action(self, path, action, start=None, end=None):
        """action: trim — сохранить обрезку, compress — сжать под лимит, send — отправить в Discord."""
        if not (self._app.library.inside(path) and os.path.isfile(path)):
            return False
        return self._app.tools.start(path, action, start, end)

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
            folder = os.path.dirname(path)
            try:  # убрать опустевшую папку игры
                if ok and self._app.library.inside(folder) and not os.listdir(folder):
                    os.rmdir(folder)
            except OSError:
                pass
            return ok
        return False

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
        import webview
        try:
            result = self._app.window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception:
            result = None
        if result:
            folder = result[0] if isinstance(result, (list, tuple)) else result
            self._app.cfg.update({"clips_dir": folder})
        return self.get_settings()

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
    if not winutil.single_instance():
        winutil.message_box("MidnightClips уже запущен — значок в трее, возле часов.")
        return
    import webview

    app = App()
    if app.cfg["autostart"]:
        winutil.set_autostart(True)  # обновить путь, если программу перенесли
    minimized = "--minimized" in sys.argv
    app.visible = not minimized
    app.window = webview.create_window(
        "MidnightClips", os.path.join(app.res, "ui", "index.html"), js_api=Api(app),
        width=1180, height=760, min_size=(940, 620), background_color="#FFE100",
        hidden=minimized)
    app.window.events.closing += app.on_closing
    webview.start(app.on_start)
    app.shutdown()
    os._exit(0)  # фоновые потоки (трей, уведомления) не должны держать процесс


if __name__ == "__main__":
    main()
