"""Склейка кусков (проверка без Windows): куски одного и разных клипов, разное разрешение,
клип без звука, выбор дорожек, точная длина и полоса кадров для шкалы."""
import os
import shutil
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from midnightclips import tools  # noqa: E402
from midnightclips.recorder import find_ffmpeg  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FF = find_ffmpeg(ROOT)


def make(path, size, seconds, tracks):
    cmd = [FF, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=%s:rate=60" % size]
    for k in range(tracks):
        cmd += ["-f", "lavfi", "-i", "sine=frequency=%d:sample_rate=48000" % (300 + 200 * k)]
    cmd += ["-t", str(seconds), "-map", "0:v"]
    for k in range(tracks):
        cmd += ["-map", "%d:a" % (k + 1)]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-g", "60", "-c:a", "aac", "-shortest", path]
    subprocess.run(cmd, check=True)


def run(fn, *args):
    done = threading.Event()
    result = {}

    def event(kind, payload):
        if kind == "job_done":
            result.update(payload)
            done.set()
    tools_obj.on_event = event
    fn(*args)
    assert done.wait(300), "склейка не закончилась"
    return result


if __name__ == "__main__":
    assert FF, "нет ffmpeg"
    work = tempfile.mkdtemp(prefix="mc_montage_")
    try:
        a, b = os.path.join(work, "a.mp4"), os.path.join(work, "Клип b.mp4")
        make(a, "1280x720", 10, 3)     # «всё вместе» + две отдельные дорожки
        make(b, "640x480", 6, 0)       # другой размер и без звука
        cfg = {"bitrate_mbps": 8, "discord_limit_mb": 10, "discord_webhook": ""}
        tools_obj = tools.ClipTools(cfg, FF, os.path.join(work, "tmp"), lambda *_: None)

        out = os.path.join(work, "Монтаж.mp4")
        res = run(tools_obj.montage, [{"path": a, "start": 1.0, "end": 3.5, "tracks": None},
                                      {"path": b, "start": 2.0, "end": 0, "tracks": None},
                                      {"path": a, "start": 7.0, "end": 9.0, "tracks": [2]}], out)
        assert res["ok"], res
        info = tools.media_info(FF, out)
        print("монтаж:", res["text"], info, flush=True)
        assert abs(info["duration"] - 8.5) < 0.15, info
        assert (info["w"], info["h"]) == (1280, 720) and info["audio"] == 1, info

        # несколько кусков одного клипа через «Сохранить» — склейка рядом с клипом
        res = run(tools_obj.start, a, "trim", None, None, None, [[0.5, 2.0], [5.0, 6.0], [8.0, 9.5]])
        assert res["ok"] and res["path"].endswith("a (склейка).mp4"), res
        info = tools.media_info(FF, res["path"])
        print("склейка кусков:", info, flush=True)
        assert abs(info["duration"] - 4.0) < 0.15, info

        # друг из Discord выключен: его фразы (2–4 с) молчат и в обрезке, и в склейке, остальное звучит
        g = os.path.join(work, "g.mp4")
        subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
                        "-f", "lavfi", "-i", "sine=frequency=500:sample_rate=48000", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                        "-t", "8", "-filter_complex", "[1:a]asplit=2[all][dc]", "-map", "0:v", "-map", "[all]", "-map", "2:a",
                        "-map", "[dc]", "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-c:a", "aac", "-shortest", g], check=True)

        def level(path, a, b):
            out = subprocess.run([FF, "-hide_banner", "-ss", str(a), "-t", str(b - a), "-i", path, "-map", "0:a:0",
                                  "-af", "volumedetect", "-f", "null", "-"], capture_output=True).stderr.decode("utf-8", "replace")
            import re
            return float(re.search(r"max_volume: (-?[\d.]+|-inf) dB", out).group(1).replace("-inf", "-200"))
        gate = {2: [(2.0, 4.0)]}   # дорожка 2 — Discord
        res = run(tools_obj.start, g, "trim", 1.0, 7.0, None, None, gate)
        assert res["ok"], res
        quiet, loud = level(res["path"], 1.4, 2.6), level(res["path"], 3.6, 5.5)   # в обрезке время сдвинуто на 1 с
        print("обрезка с выключенным другом: тишина %.1f дБ, звук %.1f дБ" % (quiet, loud), flush=True)
        assert quiet < -60 and loud > -30, (quiet, loud)
        out2 = os.path.join(work, "Монтаж друзья.mp4")
        res = run(tools_obj.montage, [{"path": g, "start": 1.0, "end": 7.0, "tracks": None, "gate": gate}], out2)
        assert res["ok"], res
        quiet, loud = level(out2, 1.4, 2.6), level(out2, 3.6, 5.5)
        print("склейка с выключенным другом: тишина %.1f дБ, звук %.1f дБ" % (quiet, loud), flush=True)
        assert quiet < -60 and loud > -30, (quiet, loud)

        strip = tools.film_strip(FF, a, os.path.join(work, "strips"))
        assert strip and strip["image"].startswith("data:image/jpeg;base64,") and len(strip["image"]) > 2000, strip
        print("полоса кадров: ok, %d байт" % len(strip["image"]), flush=True)
        print("СКЛЕЙКА: ВСЁ ПРОШЛО")
    finally:
        shutil.rmtree(work, ignore_errors=True)
