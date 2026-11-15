"""Проверка обрезки, сжатия под лимит, отправки и сервера для плеера (работает и на Linux)."""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from midnightclips import tools  # noqa: E402
from midnightclips.config import Config  # noqa: E402
from midnightclips.media import MediaServer  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FFMPEG = os.path.join(ROOT, "ffmpeg.exe") if os.path.isfile(os.path.join(ROOT, "ffmpeg.exe")) else shutil.which("ffmpeg")


def main():
    tmp = tempfile.mkdtemp(prefix="mc_tools_")
    clip = os.path.join(tmp, "Клип тест.mp4")
    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=60",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-vf", "noise=alls=25:allf=t", "-t", "40",
                    "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "9M", "-g", "60", "-c:a", "aac", clip],
                   check=True)
    print("исходник: %.1f МБ" % (os.path.getsize(clip) / 1048576))
    cfg = Config(os.path.join(tmp, "s.json"))
    cfg.update({"clips_dir": tmp, "discord_limit_mb": 10})
    events = []
    t = tools.ClipTools(cfg, FFMPEG, os.path.join(tmp, "work"), lambda k, p: events.append((k, dict(p))))

    def run(action, start=None, end=None):
        events.clear()
        assert t.start(clip, action, start, end)
        assert not t.start(clip, action)          # второй раз, пока занято, нельзя
        while t.busy:
            time.sleep(0.2)
        done = [p for k, p in events if k == "job_done"][-1]
        if not done["ok"]:
            try:
                print("   ffmpeg:", open(os.path.join(tmp, "work", "tools_err.log"), encoding="utf-8", errors="replace").read()[-1500:], flush=True)
            except OSError:
                pass
        return done, [p["percent"] for k, p in events if k == "job" and p["percent"] is not None]

    done, _ = run("trim", 10.3, 25.0)
    assert done["ok"], done
    d = tools.probe(FFMPEG, done["path"])[0]
    print("обрезка 10.3–25.0 → %.2f c  (%s)" % (d, os.path.basename(done["path"])))
    assert 14.5 <= d <= 15.8, d

    done, _ = run("trim")
    assert not done["ok"] and "ползунками" in done["text"]

    # ---- клип с отдельными дорожками: общая (оба тона), «Микрофон» (440 Гц), «Игра» (1000 Гц)
    os.makedirs(os.path.join(tmp, "tracks"), exist_ok=True)
    multi = os.path.join(tmp, "tracks", "Дорожки.mp4")
    subprocess.run([FFMPEG, "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-f", "lavfi", "-i", "sine=frequency=1000",
                    "-filter_complex", "[1:a][2:a]amix=inputs=2:normalize=0[m]", "-map", "0:v", "-map", "[m]", "-map", "1:a",
                    "-map", "2:a", "-t", "8", "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-c:a", "aac",
                    "-metadata:s:a:0", "handler_name=Всё вместе", "-metadata:s:a:1", "handler_name=Микрофон",
                    "-metadata:s:a:2", "handler_name=Игра", multi], check=True)
    names = tools.audio_tracks(FFMPEG, multi)
    print("дорожки клипа:", names)
    assert names == ["Всё вместе", "Микрофон", "Игра"], names

    def tones(path):
        import math
        raw = subprocess.run([FFMPEG, "-v", "error", "-i", path, "-map", "0:a:0", "-ss", "2", "-t", "1", "-f", "s16le",
                              "-ac", "1", "-ar", "48000", "-"], capture_output=True).stdout
        data = [int.from_bytes(raw[i:i + 2], "little", signed=True) for i in range(0, len(raw) - 1, 2)]
        out = []
        for freq in (440, 1000):
            re_, im_ = 0.0, 0.0
            for n, v in enumerate(data):
                angle = 2 * math.pi * freq * n / 48000
                re_ += v * math.cos(angle)
                im_ += v * math.sin(angle)
            out.append(2 * math.hypot(re_, im_) / max(1, len(data)))
        return out

    def run_multi(action, picked, start=None, end=None):
        events.clear()
        assert t.start(multi, action, start, end, picked)
        while t.busy:
            time.sleep(0.2)
        done = [p for k, p in events if k == "job_done"][-1]
        assert done["ok"], done
        return done["path"], len(tools.audio_tracks(FFMPEG, done["path"])), tones(done["path"])

    for label, action, picked, want in (("только «Игра»", "trim", [2], (False, True)),
                                        ("только «Микрофон», с обрезкой", "trim", [1], (True, False)),
                                        ("обе дорожки (это общая)", "compress", [1, 2], (True, True)),
                                        ("сжатие только «Игры»", "compress", [2], (False, True)),
                                        ("без звука", "trim", [], (False, False))):
        path, count, (low, high) = run_multi(action, picked, 1.0 if "обрезк" in label else None, 6.0 if "обрезк" in label else None)
        print("%-32s дорожек в файле %d, 440 Гц %5.0f, 1000 Гц %5.0f (%s)" % (label, count, low, high, os.path.basename(path)))
        assert count == (0 if picked == [] else 1), count
        assert (low > 800) == want[0] and (high > 800) == want[1], (label, low, high)
    path, count, _ = run_multi("trim", None, 1.0, 6.0)
    assert count == 3, "без выбора дорожек обрезка должна сохранить все: %d" % count
    events.clear()
    assert t.start(multi, "trim", None, None, [1, 2])   # всё выбрано и отрезка нет — сохранять нечего
    while t.busy:
        time.sleep(0.2)
    assert not [p for k, p in events if k == "job_done"][-1]["ok"]

    done, percents = run("compress")
    assert done["ok"], done
    size = os.path.getsize(done["path"]) / 1048576
    d = tools.probe(FFMPEG, done["path"])
    print("сжатие целиком → %.2f МБ, %.1f c, %d fps, высота %d; прогресс %d…%d" % (
        size, d[0], d[1], d[2], percents[0], percents[-1]))
    assert 7.5 <= size <= 10.0, size
    assert percents == sorted(percents) and percents[-1] >= 95

    cfg.update({"discord_limit_mb": 1})
    done, _ = run("compress", 5, 15)
    assert done["ok"], done
    size = os.path.getsize(done["path"]) / 1048576
    print("сжатие отрезка 10 c до 1 МБ → %.2f МБ, %.1f c" % (size, tools.probe(FFMPEG, done["path"])[0]))
    assert size <= 1.0
    done, _ = run("compress")           # 40 c в 1 МБ — должно получиться, но мелко
    assert done["ok"] and os.path.getsize(done["path"]) <= 1048576, done
    small = done["path"]
    try:
        tools.plan(1, 600)
        raise AssertionError("ожидался отказ")
    except tools.Fail as exc:
        print("слишком длинный клип →", exc)

    # отправка: без вебхука — понятная ошибка
    done, _ = run("send")
    assert not done["ok"] and "вебхук" in done["text"].lower(), done

    # отправка: поддельный «Discord» принимает файл
    got = {}

    class Hook(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            got.update(type=self.headers["Content-Type"], size=len(body), ua=self.headers["User-Agent"],
                       has_name="Клип тест.mp4".encode("utf-8") in body, has_ftyp=b"ftyp" in body)
            self.send_response(got.get("code", 204))
            self.end_headers()

    srv = HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    hook = "http://127.0.0.1:%d/api/webhooks/1/x" % srv.server_address[1]
    tools.post_file(hook, small, "Клип тест.mp4")
    print("отправка →", got)
    assert got["has_name"] and got["has_ftyp"] and "multipart/form-data" in got["type"]
    got["code"] = 413
    try:
        tools.post_file(hook, small, "x.mp4")
        raise AssertionError("ожидалась ошибка")
    except tools.Fail as exc:
        print("отказ сервера →", exc)

    # полный путь «отправить»: отрезок + сжатие + загрузка
    got["code"] = 204
    tools.WEBHOOK_RE = __import__("re").compile(r"^http://127\.0\.0\.1")
    cfg.update({"discord_webhook": hook, "discord_limit_mb": 2})
    done, _ = run("send", 2, 22)
    assert done["ok"], done
    print("отправить отрезок 20 c при лимите 2 МБ → ушло %.2f МБ" % (got["size"] / 1048576))
    assert got["size"] <= 2 * 1048576 + 2000
    assert not [f for f in os.listdir(os.path.join(tmp, "work")) if f.endswith(".mp4")], "временные файлы остались"

    # отмена
    cfg.update({"discord_limit_mb": 10})
    t.start(clip, "compress")
    time.sleep(1.0)
    t.cancel()
    while t.busy:
        time.sleep(0.1)
    left = [f for f in os.listdir(tmp) if "(10 МБ) 2" in f]
    print("отмена →", [p["text"] for k, p in events if k == "job_done"], "недописанный файл:", left)
    assert not left

    # сервер для плеера
    from midnightclips.library import Library
    lib = Library(cfg, FFMPEG, os.path.join(tmp, "cache"))
    ms = MediaServer(lib.inside)
    url = ms.url(clip)
    req = urllib.request.Request(url, headers={"Range": "bytes=100-199"})
    with urllib.request.urlopen(req) as r:
        assert r.status == 206 and len(r.read()) == 100 and r.headers["Content-Range"].startswith("bytes 100-199/")
    with urllib.request.urlopen(url) as r:
        assert len(r.read()) == os.path.getsize(clip)
    for bad in (url.replace(ms.token, "x"), ms.url("/etc/passwd")):
        try:
            urllib.request.urlopen(bad)
            raise AssertionError("должно быть 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    print("сервер плеера: диапазоны ok, чужие файлы и неверный ключ → 404")
    print("ВСЁ ПРОШЛО", url)
    return tmp, ms, clip


if __name__ == "__main__":
    main()
