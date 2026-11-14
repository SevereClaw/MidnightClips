"""Звук в движке захвата — проверка на настоящей Windows с виртуальной звуковой картой
(ставится при сборке). Проверяем: звук компьютера и микрофон записываются, громкость
применяется, длина звука равна длине видео, звук совпадает с картинкой и не уезжает.
"""
import math
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MCCAP = os.path.join(ROOT, "mccap.exe")
FFMPEG = os.path.join(ROOT, "ffmpeg.exe")
TMP = tempfile.gettempdir()


def make_wav(name, seconds, freq, amp=12000):
    path = os.path.join(TMP, name)
    with wave.open(path, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(48000)
        w.writeframes(b"".join(struct.pack("<hh", *(int(amp * math.sin(2 * math.pi * freq * i / 48000)),) * 2)
                               for i in range(int(48000 * seconds))))
    return path


def has_audio_device():
    try:
        import pyaudiowpatch as pa
        p = pa.PyAudio()
        p.get_default_wasapi_loopback()
        return True
    except Exception as exc:
        print("звуковой карты нет:", repr(exc))
        return False


def record(name, seconds, engine_args, play=None, play_at=1.5, test_pattern=True, side=None):
    """Движок (+звук) -> ffmpeg -> файл с несжатым звуком. Возвращает (путь, сообщения движка)."""
    pipe = r"\\.\pipe\mccap_au_%d_%d" % (os.getpid(), int(time.time() * 1000) % 100000)
    args = [MCCAP, "--fps", "60", "--seconds", str(seconds), "--pipe", pipe, "--audio-pipe", pipe + "_a",
            "--parent", str(os.getpid()), "--stat", "2"] + (["--test", "--size", "640x360"] if test_pattern else []) + engine_args
    helper = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    ready = helper.stdout.readline().decode().strip().split()
    assert ready and ready[0] == "READY", ready
    out = os.path.join(TMP, "mccap_audio_%s.mkv" % name)
    ff = subprocess.Popen(
        [FFMPEG, "-hide_banner", "-y", "-f", "rawvideo", "-pixel_format", ready[4], "-video_size", "%sx%s" % (ready[1], ready[2]),
         "-framerate", ready[3], "-i", pipe, "-f", "s16le", "-ar", "48000", "-ac", "2", "-i", pipe + "_a",
         "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", out],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    t0 = time.time()
    if side:
        side(t0)
    if play:
        import winsound
        time.sleep(max(0, play_at - (time.time() - t0)))
        winsound.PlaySound(play, winsound.SND_FILENAME | winsound.SND_ASYNC)
    err = helper.stderr.read().decode(errors="replace")
    helper.wait(timeout=30)
    ff_err = ff.communicate(timeout=60)[1].decode(errors="replace")
    assert ff.returncode == 0, ff_err[-800:]
    return out, err


def audio_of(path):
    raw = subprocess.run([FFMPEG, "-v", "error", "-i", path, "-map", "0:a", "-f", "s16le", "-ac", "1", "-ar", "48000", "-"],
                         capture_output=True).stdout
    return struct.unpack("<%dh" % (len(raw) // 2), raw)


def video_seconds(path):
    err = subprocess.run([FFMPEG, "-hide_banner", "-i", path, "-map", "0:v", "-f", "null", "-"], capture_output=True, text=True, errors="replace").stderr
    return int(re.findall(r"frame=\s*(\d+)", err)[-1]) / 60.0


def onset(samples, start=0, level=1500):
    """Начало устойчивого звука: громко и сейчас, и ещё через 50 мс (одиночный щелчок не считается)."""
    step = 48
    for i in range(start, len(samples) - 4800, step):
        if max(abs(x) for x in samples[i:i + step]) > level and max(abs(x) for x in samples[i + 2400:i + 2880]) > level:
            for j in range(i, i + step):
                if abs(samples[j]) > level:
                    return j
    return -1


def profile(samples):
    """Пик по четвертям секунды — чтобы видеть, где в записи звук."""
    return " ".join("%d" % (max(abs(x) for x in samples[i:i + 12000]) // 1000) for i in range(0, len(samples) - 11999, 12000))


def check(name, engine_args, low, high, tone):
    path, err = record(name, 5, engine_args, play=tone)
    a = audio_of(path)
    peak = max(abs(x) for x in a) if a else 0
    first = onset(a)
    v = video_seconds(path)
    print("%-28s звук %.2f c, видео %.2f c; пик %5d (ждём %d–%d); тон начался на %.2f c" % (
        name, len(a) / 48000, v, peak, low, high, first / 48000 if first >= 0 else -1), flush=True)
    print("      по четвертям секунды (тысячи): " + profile(a), flush=True)
    notes = sorted(set(re.findall(r"info: audio (.+)", err)))
    print("      движок: " + "; ".join(n.strip() for n in notes), flush=True)
    if "--mic" in engine_args and "CABLE Output" in engine_args:
        assert "mic opened" in " ".join(notes), "микрофон не открылся\n" + err[-900:]
    assert abs(len(a) / 48000 - v) < 0.2, "длина звука и видео расходится\n" + err[-600:]
    assert low <= peak <= high, "громкость не та: %d\n%s" % (peak, err[-900:])
    assert 0.9 < first / 48000 < 2.6, "тон не там, где его включили: %.2f" % (first / 48000)


FLASH = r"""
import sys, time, tkinter as tk, winsound
start, beep = float(sys.argv[1]), sys.argv[2]
root = tk.Tk(); root.geometry("600x450+60+60"); root.attributes("-topmost", True); root.overrideredirect(True)
root.configure(bg="#000000")
def at(when, fn):
    root.after(max(1, int((start + when - time.time()) * 1000)), fn)
def on():
    root.configure(bg="#FFFFFF"); root.update_idletasks()
    winsound.PlaySound(beep, winsound.SND_FILENAME | winsound.SND_ASYNC)
for t in (3.0, 6.0):
    at(t, on); at(t + 0.25, lambda: root.configure(bg="#000000"))
at(9.5, root.destroy)
root.mainloop()
"""


def sync_check(beep):
    """Вспышка на экране и щелчок в один момент — в записи они должны совпасть, оба раза одинаково."""
    def side(t0):
        subprocess.Popen([sys.executable, "-c", FLASH, repr(t0), beep])
    path, err = record("sync", 9, [], test_pattern=False, side=side)
    raw = subprocess.run([FFMPEG, "-v", "error", "-i", path, "-map", "0:v", "-vf", "crop=400:300:160:140,scale=4:4,format=gray",
                          "-f", "rawvideo", "-"], capture_output=True).stdout
    bright = [sum(raw[i:i + 16]) / 16.0 for i in range(0, len(raw) - 15, 16)]
    flashes, dark = [], True
    for i, b in enumerate(bright):
        if dark and b > 150:
            flashes.append(i / 60.0); dark = False
        elif b < 60:
            dark = True
    a = audio_of(path)
    beeps, pos = [], 0
    while True:
        i = onset(a, pos)
        if i < 0:
            break
        beeps.append(i / 48000.0)
        pos = i + 48000        # следующий щелчок ищем через секунду
    print("вспышки на %s c, щелчки на %s c" % (["%.3f" % f for f in flashes], ["%.3f" % b for b in beeps]), flush=True)
    assert len(flashes) == 2 and len(beeps) == 2, "не нашёл две вспышки и два щелчка\n" + err[-600:]
    offsets = [b - f for f, b in zip(flashes, beeps)]
    print("звук относительно картинки: %+.0f мс и %+.0f мс (разница между событиями %.0f мс)" % (
        offsets[0] * 1000, offsets[1] * 1000, abs(offsets[0] - offsets[1]) * 1000), flush=True)
    assert abs(offsets[0] - offsets[1]) < 0.06, "звук уезжает относительно картинки"
    assert -0.12 < offsets[0] < 0.25, "звук не совпадает с картинкой"
    assert abs((flashes[1] - flashes[0]) - 3.0) < 0.08 and abs((beeps[1] - beeps[0]) - 3.0) < 0.08, "время в записи идёт неровно"


def tone_level(samples, freq, start, seconds):
    """Амплитуда тона заданной частоты на отрезке записи (алгоритм Гёрцеля)."""
    part = samples[int(start * 48000):int((start + seconds) * 48000)]
    w = 2 * math.pi * freq / 48000
    coeff = 2 * math.cos(w)
    s1 = s2 = 0.0
    for x in part:
        s0 = x + coeff * s1 - s2
        s2, s1 = s1, s0
    power = s1 * s1 + s2 * s2 - coeff * s1 * s2
    return 2 * math.sqrt(max(power, 0.0)) / max(1, len(part))


FFPLAY = os.path.join(ROOT, "ffplay.exe")


def app_check(name, engine_args, want_a, want_b, tone_a, tone_b):
    """Две программы звучат одновременно: ffplay (440 Гц) и PowerShell (1000 Гц).
    В записи у каждой должна быть своя громкость."""
    def side(t0):
        time.sleep(0.3)
        subprocess.Popen([FFPLAY, "-nodisp", "-autoexit", "-loglevel", "quiet", tone_a],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.Popen(["powershell", "-NoProfile", "-Command",
                          "(New-Object Media.SoundPlayer '%s').PlaySync()" % tone_b],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    path, err = record(name, 7, engine_args, side=side)
    a = audio_of(path)
    la, lb = tone_level(a, 440, 3.5, 1.5), tone_level(a, 1000, 3.5, 1.5)
    print("%-34s ffplay (440 Гц): %5.0f, ждём %s; PowerShell (1000 Гц): %5.0f, ждём %s" % (
        name, la, "%d–%d" % want_a, lb, "%d–%d" % want_b), flush=True)
    opened = sorted(set(re.findall(r"info: audio (\S+) opened", err)))
    print("      движок открыл: %s" % ", ".join(opened), flush=True)
    assert want_a[0] <= la <= want_a[1] and want_b[0] <= lb <= want_b[1], "громкость программ не та\n" + err[-1500:]


def app_checks():
    tone_a = make_wav("tone440.wav", 6.0, 440)
    tone_b = make_wav("tone1000.wav", 6.0, 1000)
    full, quarter, half, none = (10000, 13500), (2400, 3600), (5000, 6900), (0, 250)
    app_check("обе программы как есть", [], full, full, tone_a, tone_b)
    app_check("ffplay 25%", ["--app-vol", "ffplay.exe=0.25"], quarter, full, tone_a, tone_b)
    app_check("PowerShell выключен", ["--app-vol", "powershell.exe=0"], full, none, tone_a, tone_b)
    app_check("остальные 50%, ffplay 100%", ["--vol-system", "0.5", "--app-vol", "ffplay.exe=1"], full, half, tone_a, tone_b)
    # список программ со звуком — для настроек
    p1 = subprocess.Popen([FFPLAY, "-nodisp", "-autoexit", "-loglevel", "quiet", tone_a], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)
    listed = subprocess.run([MCCAP, "--list-audio-apps"], capture_output=True, text=True, errors="replace").stdout
    p1.kill()
    names = [line[4:] for line in listed.splitlines() if line.startswith("APP ")]
    print("программы со звуком:", names, flush=True)
    assert "ffplay.exe" in names, listed


if __name__ == "__main__":
    if sys.platform != "win32":
        print("только для Windows — пропуск")
        sys.exit(0)
    if not has_audio_device():
        print("ВНИМАНИЕ: звук в движке НЕ ПРОВЕРЕН — на сервере нет звуковой карты")
        sys.exit(0)
    tone = make_wav("tone.wav", 2.0, 440)
    check("звук компьютера", [], 10500, 13500, tone)
    check("звук компьютера, 25%", ["--vol-system", "0.25"], 2500, 3500, tone)
    check("только микрофон", ["--audio-system", "0", "--mic", "CABLE Output"], 6000, 14000, tone)
    check("компьютер + микрофон", ["--mic", "CABLE Output"], 2000, 27000, tone)
    check("микрофон не найден", ["--mic", "Нет такого микрофона"], 10500, 13500, tone)
    path, err = record("тишина", 4, [])
    a = audio_of(path)
    print("%-28s звук %.2f c, пик %d" % ("тишина", len(a) / 48000, max(abs(x) for x in a)), flush=True)
    assert abs(len(a) / 48000 - 4.0) < 0.25 and max(abs(x) for x in a) < 200
    sync_check(make_wav("beep.wav", 0.12, 1000, 14000))
    if os.path.isfile(FFPLAY):
        app_checks()
    else:
        print("ВНИМАНИЕ: громкость по программам не проверена — нет ffplay.exe")
    print("ЗВУК В ДВИЖКЕ: OK")
