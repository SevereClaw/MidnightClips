"""Проверка движка захвата mccap на настоящей Windows (запускается при сборке).

Тестовая картинка -> именованный канал -> ffmpeg. Смотрим, что кадры идут ровно
с заданной частотой и что ffmpeg получает ровно столько кадров, сколько положено.
"""
import os
import re
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MCCAP = os.path.join(ROOT, "mccap.exe")
FFMPEG = os.path.join(ROOT, "ffmpeg.exe")


DONE = re.compile(r"done frames=(\d+) elapsed=([\d.]+) late_max=([\d.]+)ms late_over_2ms=(\d+) write_max=([\d.]+)ms "
                  r"highres_timer=(\d) trimmed=(\d+) overflow=(\d+)")


def start_helper(size, fps, seconds):
    pipe = r"\\.\pipe\mccap_test_%d_%d" % (os.getpid(), int(time.time() * 1000) % 100000)
    helper = subprocess.Popen(
        [MCCAP, "--test", "--size", size, "--fps", str(fps), "--seconds", str(seconds),
         "--pipe", pipe, "--parent", str(os.getpid()), "--stat", "1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    ready = helper.stdout.readline().decode().strip()
    assert ready.startswith("READY"), ready
    _, w, h, f = ready.split()
    return helper, pipe, int(w), int(h), int(f)


def finish_helper(helper):
    err = helper.stderr.read().decode(errors="replace")
    helper.wait(timeout=20)
    done = DONE.search(err)
    assert done, err
    return [float(x) for x in done.groups()], err


def run_case(name, size, fps, seconds, encode):
    """Движок -> ffmpeg: ffmpeg должен получить ровно столько кадров, сколько отправлено."""
    helper, pipe, w, h, f = start_helper(size, fps, seconds)
    out = os.path.join(tempfile.gettempdir(), "mccap_test.mp4")
    cmd = [FFMPEG, "-hide_banner", "-y", "-f", "rawvideo", "-pixel_format", "bgra",
           "-video_size", "%dx%d" % (w, h), "-framerate", str(f), "-i", pipe]
    cmd += (["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", out] if encode
            else ["-f", "null", "-"])
    ff = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    (frames, elapsed, late, over, wmax, hi, trimmed, overflow), _ = finish_helper(helper)
    got = re.findall(r"frame=\s*(\d+)", ff.stderr)
    received = int(got[-1]) if got else -1
    print("%-26s отправлено %d за %.2f c (%.1f к/с), ffmpeg получил %d; опоздание тика макс %.2f мс; "
          "выброшено из очереди %d+%d" % (name, frames, elapsed, frames / elapsed, received, late, trimmed, overflow), flush=True)
    assert ff.returncode == 0, ff.stderr[-800:]
    assert received == frames, "ffmpeg получил %d кадров из %d" % (received, frames)
    assert abs(frames - fps * seconds) <= max(3, fps // 10) and trimmed + overflow <= 3, (frames, trimmed, overflow)
    assert over <= frames * 0.02, "тики опаздывают: %d из %d" % (over, frames)
    return out


def read_case(name, size, fps, seconds, startup_stall=0.0, burst=0, pause=0.0):
    """Читатель-имитация: ffmpeg читает видео порциями и с задержкой на старте.
    В каждом тестовом кадре записаны его номер и время съёмки — проверяем, что кадры
    идут подряд, без повторов, и что картинка не отстаёт."""
    import struct
    helper, pipe, w, h, f = start_helper(size, fps, seconds)
    frame_size = w * h * 4
    buf = bytearray(frame_size)
    view = memoryview(buf)
    rows = []   # (номер кадра, отставание в мс, время чтения)
    with open(pipe, "rb", buffering=0) as src:
        if startup_stall:
            time.sleep(startup_stall)
        count = 0
        while True:
            got = 0
            while got < frame_size:
                n = src.readinto(view[got:])
                if not n:
                    break
                got += n
            if got < frame_size:
                break
            index, micros = struct.unpack_from("<qq", buf, 0)
            rows.append((index, (time.time() * 1e6 - micros) / 1000.0, time.time()))
            count += 1
            if burst and count % burst == 0:
                time.sleep(pause)
    (frames, elapsed, late, over, wmax, hi, trimmed, overflow), err = finish_helper(helper)
    t_start = rows[0][2]
    steady = [r for r in rows if r[2] - t_start > 2.5]          # после разгона
    gaps = sum(1 for a, b in zip(steady, steady[1:]) if b[0] != a[0] + 1)
    lag = sorted(r[1] for r in steady)
    print("%-26s прочитано %d; после разгона: пропусков/повторов %d из %d, отставание картинки медиана %.0f мс, "
          "макс %.0f мс; выброшено из очереди %d+%d" % (
              name, len(rows), gaps, len(steady), lag[len(lag) // 2], lag[-1], trimmed, overflow), flush=True)
    return gaps, lag[len(lag) // 2], lag[-1], len(steady)


if __name__ == "__main__":
    if sys.platform != "win32":
        print("только для Windows — пропуск")
        sys.exit(0)
    assert os.path.isfile(MCCAP), "нет mccap.exe"
    run_case("720p 60 -> ffmpeg", "1280x720", 60, 6, False)
    run_case("1080p 60 -> ffmpeg", "1920x1080", 60, 5, False)
    run_case("1080p 144 -> ffmpeg", "1920x1080", 144, 5, False)
    out = run_case("720p 60 -> x264", "1280x720", 60, 6, True)
    info = subprocess.run([FFMPEG, "-hide_banner", "-i", out], capture_output=True, text=True, errors="replace").stderr
    print("файл:", re.search(r"Duration: [\d:.]+", info).group(0), re.search(r"\d+(?:\.\d+)? fps", info).group(0))

    # ffmpeg читает видео не ровно, а порциями (в такт звуку) и не сразу после запуска.
    gaps, med, mx, n = read_case("ровное чтение, 120 к/с", "1280x720", 120, 7)
    assert gaps <= 2 and med < 40, (gaps, med)
    gaps, med, mx, n = read_case("порциями по 16 кадров", "1280x720", 120, 7, burst=16, pause=0.125)
    assert gaps <= n * 0.02, "кадры теряются при чтении порциями: %d из %d" % (gaps, n)
    assert med < 150, "картинка отстаёт: %.0f мс" % med
    gaps, med, mx, n = read_case("задержка 1,2 с на старте", "1280x720", 120, 7, startup_stall=1.2)
    assert med < 40, "отставание после задержки на старте не убрано: %.0f мс" % med
    gaps, med, mx, n = read_case("задержка + порции, 60 к/с", "1280x720", 60, 8, startup_stall=1.0, burst=8, pause=0.125)
    assert med < 150 and gaps <= n * 0.02, (gaps, med)

    # Настоящий захват экрана: на сборочном сервере нет видеокарты, так что это только справка.
    probe = subprocess.run([MCCAP, "--probe", "--fps", "60", "--seconds", "3"], capture_output=True, text=True, errors="replace")
    print("захват экрана здесь: код %s | %s | %s" % (probe.returncode, probe.stdout.strip(), probe.stderr.strip()[-300:]))
    print("ДВИЖОК ЗАХВАТА: OK")
