"""Медленный диск не должен останавливать чтение потока движка (работает и на Linux).

Раньше кусочек буфера писался на диск прямо в потоке, который читает движок: задумался диск —
встало чтение, переполнился канал, и захват замирал целиком («запись зависла»)."""
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from midnightclips.config import Config  # noqa: E402
from midnightclips.recorder import Recorder  # noqa: E402


def main():
    tmp = tempfile.mkdtemp(prefix="mc_spill_")
    try:
        cfg = Config(os.path.join(tmp, "s.json"))
        work = os.path.join(tmp, "buffer")
        os.makedirs(work)
        log = os.path.join(tmp, "log.txt")
        rec = Recorder(cfg, None, work, log, lambda *_: None)
        gate = threading.Event()
        real = rec._spill_write

        def slow(path, data):          # «диск задумался»: запись ждёт, пока её не отпустят
            gate.wait(10)
            time.sleep(3.2)
            real(path, data)
        rec._spill_write = slow

        names = ["s0001_%06d.ts" % i for i in range(5)]
        began = time.monotonic()
        for i, name in enumerate(names):
            data = bytes([i]) * 188 * 1000
            with rec._mem_lock:
                rec._mem[name] = data
            rec._spill(name, data)      # так делает читатель потока после каждого кусочка
        spent = time.monotonic() - began
        print("читатель отдал 5 кусочков за %.3f с" % spent)
        assert spent < 0.5, "читатель не должен ждать диск"
        # пока диск занят, кусочки в памяти — клип из них собирается как обычно
        assert all(rec._seg_size(os.path.join(work, n)) == 188000 for n in names)
        assert len(rec._segments()) == 5
        rec._drop(os.path.join(work, names[1]))    # буфер сдвинулся, пока диск думал
        gate.set()
        deadline = time.time() + 30
        while time.time() < deadline:
            with rec._mem_lock:
                left = [n for n in names if n in rec._mem]
            if not left:
                break
            time.sleep(0.1)
        assert not left, left
        on_disk = sorted(os.listdir(work))
        print("на диске:", on_disk)
        assert on_disk == [n for n in names if n != names[1]], on_disk
        with open(os.path.join(work, names[3]), "rb") as f:
            assert f.read() == bytes([3]) * 188000
        text = open(log, encoding="utf-8").read()
        print("журнал:", text.strip())
        assert "диск не успевает" in text
        print("СБРОС НА ДИСК: ВСЁ ПРОШЛО")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
