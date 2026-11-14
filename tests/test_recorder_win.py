"""Запись целиком на настоящей Windows: свой движок захвата -> ffmpeg -> буфер -> клип.

Захватывается настоящий экран сборочного сервера, по которому бегает квадрат.
Проверяем: клип собирается, в нём живое движение, запись переживает гибель
движка и гибель ffmpeg.
"""
import os
import re
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.pop("MC_TEST_SOURCE", None)

ANIMATION = r"""
import tkinter as tk, time
root = tk.Tk(); root.geometry("700x500+40+40"); root.attributes("-topmost", True)
c = tk.Canvas(root, width=700, height=500, bg="#202020"); c.pack()
r = c.create_rectangle(0, 0, 90, 90, fill="#FFE100")
t0 = time.perf_counter()
def step():
    x = int((time.perf_counter() - t0) * 300) % 600
    c.coords(r, x, 200, x + 90, 290)
    if time.perf_counter() - t0 < 120: root.after(4, step)
    else: root.destroy()
root.after(4, step); root.mainloop()
"""


def frames(ffmpeg, path, vf=None):
    cmd = [ffmpeg, "-hide_banner", "-i", path] + (["-vf", vf] if vf else []) + ["-an", "-f", "null", "-"]
    err = subprocess.run(cmd, capture_output=True, text=True, errors="replace").stderr
    found = re.findall(r"frame=\s*(\d+)", err)
    info = re.search(r"Video: .*", err)
    return (int(found[-1]) if found else 0), (info.group(0)[:110] if info else "")


def main(kind="own"):
    from midnightclips import recorder as rec, winutil
    from midnightclips.config import Config

    winutil.set_dpi_aware()
    winutil.precise_timers()
    ffmpeg = os.path.join(ROOT, "ffmpeg.exe")
    engine = rec.find_engine(ROOT)
    assert engine, "нет mccap.exe"
    anim = subprocess.Popen([sys.executable, "-c", ANIMATION])
    tmp = tempfile.mkdtemp(prefix="mc_win_")
    cfg = Config(os.path.join(tmp, "settings.json"))
    cfg.update({"clips_dir": os.path.join(tmp, "clips"), "buffer_seconds": 15, "fps": 60, "capture": kind})
    events = []
    log = os.path.join(tmp, "ffmpeg.log")
    r = rec.Recorder(cfg, ffmpeg, os.path.join(tmp, "buf"), log, lambda k, p: events.append((k, dict(p))),
                     winutil.monitors, None, engine)

    def wait_on(seconds=90):
        t0 = time.time()
        while r.state != "on" and time.time() - t0 < seconds:
            time.sleep(0.2)
        assert r.state == "on", (r.state, open(log, encoding="utf-8", errors="replace").read()[-3000:])
        return time.time() - t0

    try:
        r.start()
        print("запуск за %.1f c: захват «%s», кодировщик «%s», звук: %s" % (
            wait_on(), r.capture, r.encoder, r.audio_note or "есть"), flush=True)
        assert r.capture == kind, r.capture
        time.sleep(12)
        print("экран обновляется %s раз/с, пишется %s из %s кадров/с" % (r.screen_fps, r.fps_real, r.fps_target), flush=True)
        assert r.fps_real and r.fps_real > 54, r.fps_real

        t0 = time.time()
        r.save_clip("Проверка")
        while not [1 for k, _ in events if k in ("saved", "error")] and time.time() - t0 < 30:
            time.sleep(0.02)
        took = time.time() - t0
        saved = [p for k, p in events if k == "saved"]
        assert saved, events
        # клип берёт и кусочек, который ещё пишется: ждать его закрытия не нужно, битых кадров в конце нет
        broken = subprocess.run([ffmpeg, "-v", "error", "-i", saved[0]["path"], "-f", "null", "-"],
                                capture_output=True, text=True, errors="replace").stderr.strip()
        print("клип сохранён за %.2f c после нажатия; ошибок при чтении: %s" % (took, broken[-300:] or "нет"), flush=True)
        assert took < 1.5, "сохранение клипа ждёт: %.2f c" % took
        assert not broken, broken[-600:]
        total, info = frames(ffmpeg, saved[0]["path"])
        unique, _ = frames(ffmpeg, saved[0]["path"], "mpdecimate=hi=256:lo=64:frac=0.005")
        print("клип: %d кадров (%.1f c), из них разных %d (%.0f в секунду) | %s" % (
            total, total / 60.0, unique, unique / (total / 60.0), info), flush=True)
        assert total >= 60 * 9, total
        assert unique >= total * 0.2, "в клипе почти нет движения: %d из %d" % (unique, total)

        # гибель движка посреди записи
        assert r.start_recording("Сбой движка")
        time.sleep(3)
        r._proc.mc_helper.kill()
        time.sleep(1.5)
        assert r.recording_since is not None, "запись оборвалась"
        print("после гибели движка восстановление за %.1f c" % wait_on(40), flush=True)
        assert r.capture == kind, r.capture
        time.sleep(4)
        # гибель ffmpeg
        r._proc.kill()
        time.sleep(1.5)
        print("после гибели ffmpeg восстановление за %.1f c" % wait_on(40), flush=True)
        time.sleep(4)
        events.clear()
        r.stop_recording()
        t0 = time.time()
        while not [1 for k, _ in events if k in ("saved", "error")] and time.time() - t0 < 30:
            time.sleep(0.3)
        saved = [p for k, p in events if k == "saved"]
        assert saved, events
        total, _ = frames(ffmpeg, saved[0]["path"])
        print("запись через два сбоя: %d кадров (%.1f c)" % (total, total / 60.0), flush=True)
        assert total >= 60 * 8, total
        stars = [l.strip()[:200] for l in open(log, encoding="utf-8", errors="replace") if l.startswith("***")]
        print("журнал:\n  " + "\n  ".join(stars), flush=True)
        r.stop()
        assert r.state == "off"
        print("ЗАПИСЬ ЦЕЛИКОМ (%s): OK" % kind, flush=True)
    finally:
        anim.kill()
        if r.is_running():
            r.stop()


GAME = r"""
import tkinter as tk
root = tk.Tk(); root.title("mc-game-test"); root.geometry("640x480+120+120"); root.configure(bg="#E4002B")
root.after(120000, root.destroy); root.mainloop()
"""


def game_mode():
    """Автозахват игры: игра появилась — пишем её окно, закрылась — снова весь монитор; буфер при этом цел."""
    import ctypes
    from midnightclips import recorder as rec, winutil
    from midnightclips.config import Config

    # правила «игра или нет» на настоящем активном окне
    print("активное окно сейчас: %s" % (winutil.detect_game({"python.exe"}, set()),), flush=True)
    assert winutil.detect_game(set(), {"python.exe"}) is None or winutil.detect_game(set(), {"python.exe"})[0] != "python.exe"

    ffmpeg = os.path.join(ROOT, "ffmpeg.exe")
    engine = rec.find_engine(ROOT)
    tmp = tempfile.mkdtemp(prefix="mc_game_")
    cfg = Config(os.path.join(tmp, "settings.json"))
    cfg.update({"clips_dir": os.path.join(tmp, "clips"), "buffer_seconds": 30, "fps": 60, "capture": "own",
                "capture_target": "game"})
    events, game = [], {"proc": None}

    def detect(include, exclude):   # «игра» — наше тестовое окно, пока оно открыто
        hwnd = ctypes.windll.user32.FindWindowW(None, "mc-game-test")
        return ("python.exe", int(hwnd)) if hwnd and game["proc"] is not None else None

    def find(exe):
        hwnd = ctypes.windll.user32.FindWindowW(None, "mc-game-test")
        return int(hwnd) if hwnd else None

    r = rec.Recorder(cfg, ffmpeg, os.path.join(tmp, "buf"), os.path.join(tmp, "ffmpeg.log"),
                     lambda k, p: events.append((k, dict(p))), winutil.monitors, None, engine, find, detect)

    def wait(cond, seconds, what):
        t0 = time.time()
        while not cond() and time.time() - t0 < seconds:
            time.sleep(0.2)
        assert cond(), "%s\n%s" % (what, open(os.path.join(tmp, "ffmpeg.log"), encoding="utf-8", errors="replace").read()[-2500:])
        return time.time() - t0

    try:
        r.start()
        wait(lambda: r.state == "on", 60, "захват не запустился")
        assert not r.window_active
        time.sleep(5)
        game["proc"] = subprocess.Popen([sys.executable, "-c", GAME])
        took = wait(lambda: r.window_active and r.state == "on", 30, "на окно игры не переключились")
        print("игра появилась: переключение на её окно за %.1f c (%s)" % (took, r.snapshot().get("window_name")), flush=True)
        time.sleep(6)
        game["proc"].kill()
        game["proc"] = None
        took = wait(lambda: not r.window_active and r.state == "on", 30, "к монитору не вернулись")
        print("игра закрыта: возврат к монитору за %.1f c" % took, flush=True)
        time.sleep(5)
        r.save_clip("Игра")
        wait(lambda: [1 for k, _ in events if k in ("saved", "error")], 30, "клип не сохранился")
        saved = [p for k, p in events if k == "saved"]
        assert saved, events
        total, info = frames(ffmpeg, saved[0]["path"])
        print("клип через оба переключения: %d кадров (%.1f c) | %s" % (total, total / 60.0, info), flush=True)
        assert total > 60 * 14, "буфер не пережил переключения: %d кадров" % total
        print("АВТОЗАХВАТ ИГРЫ: OK", flush=True)
    finally:
        r.stop()
        if game["proc"]:
            game["proc"].kill()


if __name__ == "__main__":
    if sys.platform != "win32":
        print("только для Windows — пропуск")
        sys.exit(0)
    main("own")
    game_mode()
    main("enc")   # движок кодирует сам (на сервере — процессором: кодировщика видеокарты здесь нет)
