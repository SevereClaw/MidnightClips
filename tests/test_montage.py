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

        strip = tools.film_strip(FF, a, os.path.join(work, "strips"))
        assert strip and strip["image"].startswith("data:image/jpeg;base64,") and len(strip["image"]) > 2000, strip
        print("полоса кадров: ok, %d байт" % len(strip["image"]), flush=True)
        print("СКЛЕЙКА: ВСЁ ПРОШЛО")
    finally:
        shutil.rmtree(work, ignore_errors=True)
