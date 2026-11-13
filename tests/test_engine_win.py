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


def run_case(name, size, fps, seconds, encode, wallclock=False):
    pipe = r"\\.\pipe\mccap_test_%d_%d" % (os.getpid(), int(time.time() * 1000) % 100000)
    helper = subprocess.Popen(
        [MCCAP, "--test", "--size", size, "--fps", str(fps), "--seconds", str(seconds),
         "--pipe", pipe, "--parent", str(os.getpid())],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    ready = helper.stdout.readline().decode().strip()
    assert ready.startswith("READY"), ready
    _, w, h, f = ready.split()
    out = os.path.join(tempfile.gettempdir(), "mccap_test.mp4")
    cmd = [FFMPEG, "-hide_banner", "-y"] + (["-use_wallclock_as_timestamps", "1"] if wallclock else []) + [
        "-f", "rawvideo", "-pixel_format", "bgra", "-video_size", "%sx%s" % (w, h), "-framerate", f, "-i", pipe]
    if wallclock:
        cmd += ["-r", f, "-fps_mode", "cfr"]
    cmd += (["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", out] if encode
            else ["-f", "null", "-"])
    ff = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    err = helper.stderr.read().decode(errors="replace")
    helper.wait(timeout=20)
    done = re.search(r"done frames=(\d+) elapsed=([\d.]+) late_max=([\d.]+)ms late_over_2ms=(\d+) write_max=([\d.]+)ms highres_timer=(\d)", err)
    assert done, err
    frames, elapsed, late, over, wmax, hi = int(done[1]), float(done[2]), float(done[3]), int(done[4]), float(done[5]), done[6]
    got = re.findall(r"frame=\s*(\d+)", ff.stderr)
    received = int(got[-1]) if got else -1
    print("%-28s отправлено %d за %.2f c (%.2f к/с), ffmpeg получил %d; опоздание макс %.2f мс, >2 мс: %d раз; "
          "запись кадра макс %.1f мс; точный таймер: %s" % (
              name, frames, elapsed, frames / elapsed, received, late, over, wmax, "да" if hi == "1" else "нет"), flush=True)
    assert ff.returncode == 0, ff.stderr[-800:]
    if wallclock:   # время по часам: ffmpeg сам выравнивает, смотрим, сколько кадров он повторил/выбросил
        dup = re.findall(r"dup=\s*(\d+)", ff.stderr)
        drop = re.findall(r"drop=\s*(\d+)", ff.stderr)
        dup, drop = (int(dup[-1]) if dup else 0), (int(drop[-1]) if drop else 0)
        print("   время по часам: повторено %d, выброшено %d" % (dup, drop), flush=True)
        assert dup + drop <= max(3, frames // 100), "ffmpeg правил поток: dup=%d drop=%d" % (dup, drop)
        assert abs(received - frames) <= max(3, frames // 100), (received, frames)
        return late, over, frames, out
    assert received == frames, "ffmpeg получил %d кадров из %d" % (received, frames)
    assert abs(frames - fps * seconds) <= 2, "кадров %d, ожидалось %d" % (frames, fps * seconds)
    return late, over, frames, out


if __name__ == "__main__":
    if sys.platform != "win32":
        print("только для Windows — пропуск")
        sys.exit(0)
    assert os.path.isfile(MCCAP), "нет mccap.exe"
    late, over, frames, _ = run_case("720p 60, только приём", "1280x720", 60, 6, False)
    assert over <= frames * 0.02, "слишком много опозданий: %d из %d" % (over, frames)
    run_case("1080p 60, только приём", "1920x1080", 60, 5, False)
    run_case("1080p 120, только приём", "1920x1080", 120, 5, False)
    run_case("1080p 144, только приём", "1920x1080", 144, 5, False)
    run_case("1080p 60, время по часам", "1920x1080", 60, 6, False, wallclock=True)
    run_case("1080p 144, время по часам", "1920x1080", 144, 6, False, wallclock=True)
    _, _, _, out = run_case("720p 60, кодирование x264", "1280x720", 60, 6, True)
    info = subprocess.run([FFMPEG, "-hide_banner", "-i", out], capture_output=True, text=True, errors="replace").stderr
    print("файл:", re.search(r"Duration: [\d:.]+", info).group(0), re.search(r"\d+(?:\.\d+)? fps", info).group(0))

    # Настоящий захват экрана: на сборочном сервере нет видеокарты, так что это только справка.
    probe = subprocess.run([MCCAP, "--probe", "--fps", "60", "--seconds", "3"], capture_output=True, text=True, errors="replace")
    print("захват экрана здесь: код %s | %s | %s" % (probe.returncode, probe.stdout.strip(), probe.stderr.strip()[-300:]))
    print("ДВИЖОК ЗАХВАТА: OK")
