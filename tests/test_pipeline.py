"""Проверка конвейера «кусочки -> клип» на тестовой картинке (работает и на Linux).

Запуск:  MC_TEST_SOURCE=1 python tests/test_pipeline.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time

os.environ["MC_TEST_SOURCE"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from midnightclips import recorder as rec  # noqa: E402
from midnightclips.config import Config  # noqa: E402


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,start_time",
         "-of", "json", path], capture_output=True, text=True).stdout
    return json.loads(out)


def main(storage="disk"):
    print("=== буфер: %s" % storage)
    tmp = tempfile.mkdtemp(prefix="mc_test_")
    cfg = Config(os.path.join(tmp, "settings.json"))
    cfg.update({"clips_dir": os.path.join(tmp, "clips"), "buffer_seconds": 6, "fps": 30, "buffer_storage": storage})
    events = []
    r = rec.Recorder(cfg, rec.find_ffmpeg("/nonexistent"), os.path.join(tmp, "buf"),
                     os.path.join(tmp, "ffmpeg.log"), lambda k, p: events.append((k, dict(p))))
    r.start()
    t0 = time.time()
    while r.state != "on" and time.time() - t0 < 20:
        time.sleep(0.2)
    assert r.state == "on", (r.state, open(os.path.join(tmp, "ffmpeg.log")).read())
    print("захват запущен за %.1f c" % (time.time() - t0))

    time.sleep(13)
    n_seg = len(r._segments())
    assert n_seg <= 6 / rec.SEGMENT + 6, "старые кусочки не удаляются: %d" % n_seg
    on_disk = [f for f in os.listdir(os.path.join(tmp, "buf")) if f.endswith(".ts")]
    print("кусочков в буфере: %d, из них файлами на диске: %d, в памяти: %d (%.1f МБ)" % (
        n_seg, len(on_disk), len(r._mem), sum(len(v) for v in r._mem.values()) / 1048576))
    assert r.in_memory == (storage == "memory")
    assert (not on_disk) if storage == "memory" else (len(on_disk) == n_seg and not r._mem)

    # 1) клип из буфера
    r.save_clip("Тест: Игра?")
    # 2) обычная запись ~5 секунд
    time.sleep(4)
    assert r.start_recording("Запись теста")
    time.sleep(5)
    spilled = [f for f in os.listdir(os.path.join(tmp, "buf")) if f.endswith(".ts")]
    if storage == "memory":
        print("во время записи сброшено на диск кусочков:", len(spilled))
        assert spilled, "идущая запись должна уходить на диск, а не копиться в памяти"
    r.stop_recording()
    time.sleep(6)

    saved = [p for k, p in events if k == "saved"]
    errors = [p for k, p in events if k == "error"]
    assert not errors, errors
    assert len(saved) == 2, events
    for item in saved:
        info = probe(item["path"])
        kinds = sorted(s["codec_type"] for s in info["streams"])
        dur = float(info["format"]["duration"])
        starts = [float(s["start_time"]) for s in info["streams"]]
        print("%-9s %5.2f c  потоки=%s  старты=%s  %s" % (
            item["kind"], dur, kinds, starts, os.path.relpath(item["path"], tmp)))
        assert kinds == ["audio", "video"], kinds
        assert abs(starts[0] - starts[1]) < 0.15, "рассинхрон старта"
        if item["kind"] == "clip":
            assert 5.0 <= dur <= 7.2, dur
        else:
            assert 4.0 <= dur <= 9.0, dur

    # 3) длинный путь склейки (через общий файл)
    out = os.path.join(tmp, "joined.mp4")
    rec.CONCAT_LIMIT = 0
    ok = r._mux(r._segments()[:-1], out)
    rec.CONCAT_LIMIT = 24000
    assert ok and os.path.getsize(out) > 0
    assert not [f for f in os.listdir(os.path.join(tmp, "buf")) if f.endswith(".tmp")]
    print("склейка через файл: ok,", os.path.getsize(out) // 1024, "КБ")

    pump_stats = [t for t in __import__("threading").enumerate() if t.name == "audio-pump"]
    for t in pump_stats:
        print("звук: записано %.1f c, из них тишины %.2f c" % (
            t.frames_written / 48000, t.frames_padded / 48000))
        assert t.frames_padded > 0.5 * 48000, "пауза в звуке не заполнена тишиной"

    r.restart()
    time.sleep(1)
    t0 = time.time()
    while r.state != "on" and time.time() - t0 < 20:
        time.sleep(0.2)
    assert r.state == "on"
    print("перезапуск: ok")
    r.stop()
    assert r.state == "off"
    left = os.listdir(os.path.join(tmp, "buf"))
    assert not left and not r._mem, (left, list(r._mem))
    print("остановка: ok, временные файлы убраны")
    print("ВСЁ ПРОШЛО")


if __name__ == "__main__":
    main("memory")
    main("disk")
