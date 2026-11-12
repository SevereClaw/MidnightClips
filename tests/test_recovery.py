"""Сбой захвата посреди буфера и посреди записи: всё должно пережить перезапуск.

Запуск:  python tests/test_recovery.py   (тестовая картинка, работает и на Linux)
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
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,nb_frames",
         "-of", "json", path], capture_output=True, text=True).stdout
    info = json.loads(out)
    return float(info["format"]["duration"]), sorted(s["codec_type"] for s in info["streams"])


def wait_on(r, seconds=25):
    t0 = time.time()
    while r.state != "on" and time.time() - t0 < seconds:
        time.sleep(0.2)
    assert r.state == "on", r.state
    return time.time() - t0


def main():
    tmp = tempfile.mkdtemp(prefix="mc_rec_")
    cfg = Config(os.path.join(tmp, "settings.json"))
    cfg.update({"clips_dir": os.path.join(tmp, "clips"), "buffer_seconds": 30, "fps": 30})
    events = []
    log = os.path.join(tmp, "ffmpeg.log")
    r = rec.Recorder(cfg, rec.find_ffmpeg("/nonexistent"), os.path.join(tmp, "buf"), log,
                     lambda k, p: events.append((k, dict(p))))
    r.start()
    wait_on(r)
    time.sleep(9)

    # --- сбой №1: ffmpeg «умер» (как при смене режима экрана игрой)
    before = len(r._segments())
    r._proc.kill()
    time.sleep(1.5)
    print("после сбоя: состояние=%s, кусочков сохранено=%d (было %d)" % (r.state, len(r._segments()), before))
    assert len(r._segments()) >= before - 1, "буфер пропал при сбое"
    print("восстановление за %.1f c" % wait_on(r))
    time.sleep(7)
    sessions = sorted({os.path.basename(p)[:6] for p in r._segments()})
    assert len(sessions) == 2, sessions

    r.save_clip("Сбой")
    time.sleep(6)
    saved = [p for k, p in events if k == "saved"]
    assert len(saved) == 1, events
    dur, kinds = probe(saved[0]["path"])
    print("клип через сбой: %.1f c, потоки %s" % (dur, kinds))
    assert kinds == ["audio", "video"] and dur >= 14, dur

    # --- сбой №2 посреди записи: запись должна продолжиться
    assert r.start_recording("Долгая запись")
    time.sleep(5)
    r._proc.kill()
    time.sleep(1.0)
    assert r.recording_since is not None, "запись оборвалась при сбое"
    wait_on(r)
    assert r.recording_since is not None
    time.sleep(6)
    r.stop_recording()
    time.sleep(7)
    saved = [p for k, p in events if k == "saved" and p["kind"] == "recording"]
    assert len(saved) == 1, events
    dur, kinds = probe(saved[0]["path"])
    print("запись через сбой: %.1f c, потоки %s" % (dur, kinds))
    assert kinds == ["audio", "video"] and dur >= 9, dur

    # --- зависание: ffmpeg жив, но данных нет
    import signal
    r._proc.send_signal(signal.SIGSTOP)
    t0 = time.time()
    while "зависла" not in open(log, encoding="utf-8").read() and time.time() - t0 < 30:
        time.sleep(0.5)
    print("зависание замечено за %.0f c" % (time.time() - t0))
    assert time.time() - t0 < 30
    wait_on(r)
    print("журнал:", [l.strip() for l in open(log, encoding="utf-8") if l.startswith("***")])

    errors = [p for k, p in events if k == "error"]
    assert not errors, errors
    r.stop()
    assert not os.listdir(os.path.join(tmp, "buf")), os.listdir(os.path.join(tmp, "buf"))
    print("ВСЁ ПРОШЛО")


if __name__ == "__main__":
    main()
