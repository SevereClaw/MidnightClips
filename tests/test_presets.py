"""Свой набор для записи: сам включается при старте записи и сам возвращает настройки клипов.

Запуск:  python tests/test_presets.py   (тестовая картинка, работает и на Linux)
"""
import json
import os
import subprocess
import sys
import tempfile
import time

os.environ["MC_TEST_SOURCE"] = "1"
tmp = tempfile.mkdtemp(prefix="mc_presets_")
os.environ["APPDATA"] = os.path.join(tmp, "appdata")
os.environ["LOCALAPPDATA"] = os.path.join(tmp, "local")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as app_main  # noqa: E402


def fps_of(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
                          "stream=r_frame_rate:format=duration", "-of", "json", path],
                         capture_output=True, text=True).stdout
    info = json.loads(out)
    num, den = info["streams"][0]["r_frame_rate"].split("/")
    return round(int(num) / int(den)), float(info["format"]["duration"])


def main():
    app = app_main.App()
    app.cfg.update({"clips_dir": os.path.join(tmp, "clips"), "fps": 60, "height": 0, "bitrate_mbps": 12,
                    "buffer_seconds": 10, "toast": False, "sound": False})
    events = []
    original = app.recorder.on_event
    app.recorder.on_event = lambda k, p: (events.append((k, dict(p))), original(k, p))
    api = app_main.Api(app)

    assert api.save_preset("Мой для записи") == ""
    assert [p["name"] for p in api.get_presets()][-1] == "Мой для записи"
    app.presets.custom[-1].update(fps=30, bitrate_mbps=6)   # набор для записи отличается от настроек клипов
    app.cfg.update({"preset_record": "Мой для записи"})

    app.recorder.start()
    t0 = time.time()
    while app.recorder.state != "on" and time.time() - t0 < 20:
        time.sleep(0.2)
    assert app.recorder.state == "on"
    time.sleep(5)
    before = len(app.recorder._segments())

    t0 = time.time()
    app.toggle_record("Проверка")        # как по горячей клавише
    print("запись началась через %.1f c; настройки на время записи: %d к/с, %d Мбит/с; буфер начат заново (кусочков было %d, стало %d)" % (
        time.time() - t0, app.cfg["fps"], app.cfg["bitrate_mbps"], before, len(app.recorder._segments())))
    assert app.recorder.recording_since is not None
    assert (app.cfg["fps"], app.cfg["bitrate_mbps"]) == (30, 6) and app.cfg["clip_restore"]
    time.sleep(6)
    app.toggle_record("Проверка")
    t0 = time.time()
    while not [1 for k, _ in events if k == "saved"] and time.time() - t0 < 30:
        time.sleep(0.2)
    saved = [p for k, p in events if k == "saved"]
    assert saved, events
    fps, dur = fps_of(saved[0]["path"])
    print("запись: %d к/с, %.1f c" % (fps, dur))
    assert fps == 30 and dur >= 4
    t0 = time.time()
    while (app.cfg["clip_restore"] or app.recorder.state != "on") and time.time() - t0 < 30:
        time.sleep(0.2)
    print("после записи: %d к/с, %d Мбит/с; захват снова в эфире: %s" % (
        app.cfg["fps"], app.cfg["bitrate_mbps"], app.recorder.state == "on"))
    assert (app.cfg["fps"], app.cfg["bitrate_mbps"], app.cfg["clip_restore"]) == (60, 12, "")

    # клип после возврата — снова с настройками клипов
    time.sleep(7)
    events.clear()
    app.save_clip()
    t0 = time.time()
    while not [1 for k, _ in events if k in ("saved", "error")] and time.time() - t0 < 30:
        time.sleep(0.2)
    saved = [p for k, p in events if k == "saved"]
    assert saved, events
    print("клип после записи: %d к/с" % fps_of(saved[0]["path"])[0])
    assert fps_of(saved[0]["path"])[0] == 60

    # набор совпадает с настройками клипов — переключения нет
    app.cfg.update({"preset_record": "Высокое качество", "bitrate_mbps": 20})
    time.sleep(4)
    while app.recorder.state != "on":
        time.sleep(0.2)
    session = app.recorder._session
    app.toggle_record("Без переключения")
    assert app.recorder.recording_since is not None and app.recorder._session == session and not app.cfg["clip_restore"]
    app.toggle_record("Без переключения")
    time.sleep(5)

    # программу закрыли посреди записи со своим набором — при следующем запуске настройки клипов возвращаются
    app.cfg.update({"fps": 30, "clip_restore": json.dumps({"fps": 60, "height": 0, "bitrate_mbps": 20})})
    app.recorder.stop()
    app2 = app_main.App()
    print("после перезапуска программы: %d к/с, отложенные настройки очищены: %s" % (app2.cfg["fps"], app2.cfg["clip_restore"] == ""))
    assert app2.cfg["fps"] == 60 and app2.cfg["clip_restore"] == ""
    assert api.delete_preset("Мой для записи") and app.cfg["preset_record"] == "Высокое качество"
    print("ВСЁ ПРОШЛО")
    os._exit(0)


if __name__ == "__main__":
    main()
