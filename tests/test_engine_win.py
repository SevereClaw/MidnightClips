"""Проверка движка захвата mccap на настоящей Windows (запускается при сборке).

Тестовая картинка -> именованный канал -> ffmpeg. Смотрим, что кадры идут ровно
с заданной частотой и что ffmpeg получает ровно столько кадров, сколько положено.
"""
import ctypes
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
    w, h, f = ready.split()[1:4]
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


PATTERN = r"""
import tkinter as tk
root = tk.Tk(); root.geometry("940x680+20+20"); root.attributes("-topmost", True); root.overrideredirect(True)
c = tk.Canvas(root, width=940, height=680, bg="#181818", highlightthickness=0); c.pack()
colors = ["#FFE100", "#E4002B", "#2743D8", "#2FCB4B", "#E63BC8", "#20D6E8", "#F4F4F4", "#000000", "#808080", "#FF7A00"]
for i, col in enumerate(colors):
    c.create_rectangle(20 + i * 90, 20, 100 + i * 90, 300, fill=col, outline="")
for i in range(32):
    v = "#%02x%02x%02x" % (i * 8, 255 - i * 8, (i * 16) % 256)
    c.create_rectangle(20 + i * 28, 330, 48 + i * 28, 520, fill=v, outline="")
c.create_oval(60, 540, 400, 670, fill="#FFE100", outline="#E4002B", width=12)
c.create_text(650, 600, text="MIDNIGHT CLIPS", fill="#FFFFFF", font=("Arial", 36, "bold"))
root.after(60000, root.destroy); root.mainloop()
"""


def dump(name, *extra):
    """Один готовый кадр настоящего экрана -> (ширина, высота, формат, байты)."""
    path = os.path.join(tempfile.gettempdir(), "mccap_%s.raw" % name)
    res = subprocess.run([MCCAP, "--probe", "--seconds", "1", "--dump", path, *extra],
                         capture_output=True, text=True, errors="replace")
    found = re.search(r"PROBE ok (\d+)x(\d+) (\w+) source=(\d+)x(\d+).* dumped=(\d)", res.stdout)
    assert res.returncode == 0 and found and found[6] == "1", (res.returncode, res.stdout, res.stderr[-500:])
    with open(path, "rb") as f:
        data = f.read()
    return int(found[1]), int(found[2]), found[3], data


def compare(name, ref, got, box=None, limit=3.0):
    """ref — кадр BGRA без видеокарты, got — NV12 с видеокарты. Сверяем яркость и цвет по точкам."""
    sw, sh, _, bgra = ref
    ow, oh, fmt, nv12 = got
    assert fmt == "nv12" and len(nv12) == ow * oh * 3 // 2, (fmt, len(nv12), ow, oh)
    fx, fy = sw / ow, sh / oh
    scaled = (ow, oh) != (sw & ~1, sh & ~1)

    def src(x, y):   # среднее по 2x2 точкам исходника вокруг (x, y)
        x0 = min(max(int(x - 0.5), 0), sw - 2)
        y0 = min(max(int(y - 0.5), 0), sh - 2)
        b = g = r = 0
        for dy in (0, 1):
            for dx in (0, 1):
                i = ((y0 + dy) * sw + x0 + dx) * 4
                b += bgra[i]; g += bgra[i + 1]; r += bgra[i + 2]
        return b / 4.0, g / 4.0, r / 4.0

    x1, y1, x2, y2 = box or (0, 0, ow, oh)
    step = 1 if box else 3
    ysum = ybad = n = 0
    for oy in range(y1, y2, step):
        for ox in range(x1, x2, step):
            if scaled:
                b, g, r = src((ox + 0.5) * fx, (oy + 0.5) * fy)
            else:
                i = (oy * sw + ox) * 4
                b, g, r = bgra[i], bgra[i + 1], bgra[i + 2]
            d = abs(nv12[oy * ow + ox] - (16 + 0.1826 * r + 0.6142 * g + 0.0620 * b))
            ysum += d; n += 1
            if d > 24:
                ybad += 1
    csum = m = 0
    uv = ow * oh
    for cy in range(y1 // 2, y2 // 2, step):
        for cx in range(x1 // 2, x2 // 2, step):
            b, g, r = src((cx * 2 + 1) * fx, (cy * 2 + 1) * fy)
            u = 128 - 0.1006 * r - 0.3386 * g + 0.4392 * b
            v = 128 + 0.4392 * r - 0.3989 * g - 0.0403 * b
            i = uv + cy * ow + cx * 2
            csum += abs(nv12[i] - u) + abs(nv12[i + 1] - v); m += 2
    print("   %-34s %dx%d: яркость расходится в среднем на %.2f (резко — %.1f%% точек), цвет — на %.2f" % (
        name, ow, oh, ysum / n, 100.0 * ybad / n, csum / m), flush=True)
    assert ysum / n < limit and csum / m < limit + 1.5, "картинка с видеокарты не совпадает: %s" % name
    assert ybad / n < (0.08 if scaled else 0.01), "слишком много резких расхождений: %s" % name


def gpu_checks():
    """Кадр, собранный на видеокарте (курсор, уменьшение, NV12), должен совпадать с обычным."""
    pattern = subprocess.Popen([sys.executable, "-c", PATTERN])
    try:
        time.sleep(2.5)
        ref = dump("cpu", "--cpu")
        assert ref[2] == "bgra"
        compare("без уменьшения", ref, dump("gpu"))
        compare("уменьшение до 480", ref, dump("gpu480", "--height", "480"), limit=7.0)
        compare("уменьшение до 360", ref, dump("gpu360", "--height", "360"), limit=9.0)
        for kind, title in (("color", "цветной курсор"), ("mono", "чёрно-белый курсор"), ("masked", "курсор с маской")):
            ref_c = dump("cpu_" + kind, "--cpu", "--fake-cursor", kind)
            got_c = dump("gpu_" + kind, "--fake-cursor", kind)
            compare(title, ref_c, got_c, box=(96, 96, 138, 138))
            # курсор действительно нарисован: область под ним отличается от кадра без курсора
            sw = ref[0]
            changed = sum(1 for y in range(100, 132) for x in range(100, 132)
                          if ref_c[3][(y * sw + x) * 4:(y * sw + x) * 4 + 3] != ref[3][(y * sw + x) * 4:(y * sw + x) * 4 + 3])
            assert changed > 200, "курсор не нарисован (%s): %d точек" % (kind, changed)
    finally:
        pattern.kill()


WINDOW = r"""
import tkinter as tk
root = tk.Tk(); root.title("mc-window-test"); root.geometry("640x480+100+100")
c = tk.Canvas(root, width=640, height=480, highlightthickness=0); c.pack()
c.create_rectangle(0, 0, 320, 480, fill="#E4002B", outline="")
c.create_rectangle(320, 0, 640, 480, fill="#2743D8", outline="")
root.after(60000, root.destroy); root.mainloop()
"""
COVER = r"""
import tkinter as tk
root = tk.Tk(); root.overrideredirect(True); root.attributes("-topmost", True); root.geometry("220x200+310+260")
root.configure(bg="#2FCB4B")
root.after(60000, root.destroy); root.mainloop()
"""
RED, BLUE, GREEN = (60, 124, 226), (78, 196, 110), (154, 88, 65)   # цвета в виде яркость/цвет (YUV)


def yuv_at(frame, fx, fy):
    w, h, _fmt, data = frame
    x, y = int(w * fx), int(h * fy)
    i = w * h + (y // 2) * w + (x // 2) * 2
    return data[y * w + x], data[i], data[i + 1]


def near(got, want, tol=16):
    return all(abs(g - w) <= tol for g, w in zip(got, want))


def window_checks():
    """Захват одного окна: в кадре только оно, даже если поверх лежит чужое окно."""
    import ctypes
    win = subprocess.Popen([sys.executable, "-c", WINDOW])
    cover = None
    try:
        hwnd = 0
        for _ in range(50):
            hwnd = ctypes.windll.user32.FindWindowW(None, "mc-window-test")
            if hwnd:
                break
            time.sleep(0.2)
        assert hwnd, "тестовое окно не появилось"
        time.sleep(1.5)
        cover = subprocess.Popen([sys.executable, "-c", COVER])
        time.sleep(2.0)
        probe = subprocess.run([MCCAP, "--probe", "--seconds", "1", "--window", str(hwnd), "--dump",
                                os.path.join(tempfile.gettempdir(), "mccap_win.raw")], capture_output=True, text=True, errors="replace")
        if probe.returncode == 7:
            print("ВНИМАНИЕ: захват окна НЕ ПРОВЕРЕН — на сервере он недоступен: " + probe.stderr.strip()[-200:], flush=True)
            return
        frame = dump("win", "--window", str(hwnd))
        left, right = yuv_at(frame, 0.40, 0.60), yuv_at(frame, 0.60, 0.60)
        screen = dump("mon")   # весь монитор: на том же месте должно быть видно зелёное окно сверху
        on_screen = yuv_at(screen, 420 / screen[0], 360 / screen[1])
        print("   окно %dx%d: слева %s (красный %s), справа %s (синий %s); на экране в этом месте %s (зелёный %s)" % (
            frame[0], frame[1], left, RED, right, BLUE, on_screen, GREEN), flush=True)
        assert near(left, RED) and near(right, BLUE), "в кадре окна не то, что в окне"
        assert near(on_screen, GREEN), "проверка некорректна: зелёное окно не перекрыло тестовое"
        small = dump("win240", "--window", str(hwnd), "--height", "240")
        print("   то же окно с уменьшением: %dx%d, слева %s, справа %s" % (small[0], small[1], yuv_at(small, 0.40, 0.60), yuv_at(small, 0.60, 0.60)), flush=True)
        assert small[1] == 240 and near(yuv_at(small, 0.40, 0.60), RED) and near(yuv_at(small, 0.60, 0.60), BLUE)

        # поток кадров из окна в ffmpeg
        pipe = r"\\.\pipe\mccap_wtest_%d" % os.getpid()
        helper = subprocess.Popen([MCCAP, "--window", str(hwnd), "--fps", "60", "--seconds", "4", "--pipe", pipe,
                                   "--parent", str(os.getpid())], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ready = helper.stdout.readline().decode().split()
        assert ready and ready[0] == "READY" and ready[4] == "nv12", ready
        ff = subprocess.run([FFMPEG, "-hide_banner", "-f", "rawvideo", "-pixel_format", "nv12", "-video_size",
                             "%sx%s" % (ready[1], ready[2]), "-framerate", "60", "-i", pipe, "-f", "null", "-"],
                            capture_output=True, text=True, errors="replace")
        helper.wait(timeout=20)
        got = re.findall(r"frame=\s*(\d+)", ff.stderr)
        print("   окно -> ffmpeg: %s кадров за 4 c" % (got[-1] if got else "?"), flush=True)
        assert got and abs(int(got[-1]) - 240) <= 8, ff.stderr[-500:]

        # окно закрыли — движок должен сам завершиться с кодом 6 (программа перейдёт на запись монитора)
        pipe2 = pipe + "_2"
        helper = subprocess.Popen([MCCAP, "--window", str(hwnd), "--fps", "60", "--seconds", "20", "--pipe", pipe2,
                                   "--parent", str(os.getpid())], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        helper.stdout.readline()
        reader = subprocess.Popen([FFMPEG, "-hide_banner", "-f", "rawvideo", "-pixel_format", "nv12", "-video_size",
                                   "%sx%s" % (ready[1], ready[2]), "-framerate", "60", "-i", pipe2, "-f", "null", "-"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
        win.kill()
        t0 = time.time()
        code = helper.wait(timeout=15)
        reader.wait(timeout=15)
        print("   окно закрыто: движок вышел с кодом %s через %.1f c" % (code, time.time() - t0), flush=True)
        assert code == 6, code
    finally:
        win.kill()
        if cover:
            cover.kill()


def encode_case(name, fps, seconds, *args, expect_code=None):
    """Движок сам кодирует (--encode): в канал идёт готовое видео, ffmpeg его только перекладывает.
    Возвращает (файл, ширина, высота)."""
    pipe = r"\\.\pipe\mccap_enc_%d_%d" % (os.getpid(), int(time.time() * 1000) % 100000)
    helper = subprocess.Popen(
        [MCCAP, "--fps", str(fps), "--seconds", str(seconds), "--pipe", pipe, "--parent", str(os.getpid()),
         "--stat", "1", "--encode", "libx264", "--bitrate", "30", "--no-cursor", *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    ready = helper.stdout.readline().decode().strip()
    if expect_code is not None:
        err = helper.stderr.read().decode(errors="replace")
        helper.wait(timeout=20)
        print("%-34s код %s (ожидался %s) | %s" % (name, helper.returncode, expect_code, err.strip()[-160:]), flush=True)
        assert helper.returncode == expect_code and not ready, (helper.returncode, ready, err[-500:])
        return None
    if not ready.startswith("READY"):
        raise AssertionError((ready, helper.stderr.read().decode(errors="replace")[-1500:]))
    w, h, f, fmt = ready.split()[1:5]
    assert fmt == "coded" and int(f) == fps, ready
    out = os.path.join(tempfile.gettempdir(), "mccap_enc_%d.ts" % (int(time.time() * 1000) % 100000))
    ff = subprocess.run([FFMPEG, "-hide_banner", "-y", "-analyzeduration", "1000000", "-f", "nut", "-i", pipe,
                         "-c", "copy", out], capture_output=True, text=True, errors="replace")
    (frames, elapsed, late, over, wmax, hi, trimmed, overflow), err = finish_helper(helper)
    assert ff.returncode == 0, ff.stderr[-1200:]
    notes = [l.strip() for l in ff.stderr.splitlines() if l.startswith("[") and "Guessed" not in l]
    if notes:
        print("  ffmpeg при чтении потока: %d сообщений, например: %s" % (len(notes), notes[len(notes) // 2][:160]), flush=True)
    # время кадров: строго подряд, с шагом 1/fps
    info = subprocess.run([FFMPEG, "-hide_banner", "-i", out, "-vf", "showinfo", "-f", "null", "-"],
                          capture_output=True, text=True, errors="replace").stderr
    times = [float(x) for x in re.findall(r"pts_time:\s*([\d.]+)", info)]
    size = re.search(r"Video: \w+.*?, (\d+)x(\d+)", info)
    assert size and (int(size[1]), int(size[2])) == (int(w) & ~1, int(h) & ~1), (ready, size and size.groups())
    steps = [b - a for a, b in zip(times, times[1:])]
    worst = max(abs(d - 1.0 / fps) for d in steps) if steps else 9
    enc_info = re.search(r"info: encoder .*", err)
    print("%-34s %sx%s, отправлено %d кадров за %.2f c, в файле %d, шаг времени ±%.2f мс, кодирование кадра макс %.1f мс | %s" % (
        name, w, h, frames, elapsed, len(times), worst * 1000, wmax, enc_info.group(0).strip() if enc_info else "?"), flush=True)
    assert len(times) == frames, "в файле %d кадров из %d" % (len(times), frames)
    assert abs(frames - fps * seconds) <= max(4, fps // 6) and overflow <= 3, (frames, overflow)
    assert worst < 0.002, "кадры идут неровно: %.2f мс" % (worst * 1000)
    assert "error:" not in err, err[-800:]
    return out, int(w) & ~1, int(h) & ~1


def decoded_frame(path, w, h, index=20):
    """Один кадр из файла в BGRA."""
    raw = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", path, "-vf", "select=eq(n\\,%d)" % index,
                          "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "bgra", "pipe:1"], capture_output=True).stdout
    assert len(raw) == w * h * 4, (len(raw), w, h)
    return raw


def bgra_diff(a, b, w, h, step=7):
    """Средняя разница двух кадров BGRA по сетке точек (по каналам B, G, R)."""
    total, count = 0, 0
    for y in range(4, h - 4, step):
        for x in range(4, w - 4, step):
            i = (y * w + x) * 4
            total += abs(a[i] - b[i]) + abs(a[i + 1] - b[i + 1]) + abs(a[i + 2] - b[i + 2])
            count += 3
    return total / max(1, count)


def encode_checks():
    # 1. Тестовая картинка: перевод цвета на процессоре, время кадров, число кадров.
    out, w, h = encode_case("тестовая картинка 720p 60", 60, 4, "--test", "--size", "1280x720")
    frame = decoded_frame(out, w, h)
    def pixel(x, y):
        i = (y * w + x) * 4
        return frame[i + 2], frame[i + 1], frame[i]   # R, G, B
    for bar, want in ((0, (0xF4, 0xF4, 0xF4)), (1, (0xFF, 0xE1, 0x00)), (5, (0xE4, 0x00, 0x2B)), (6, (0x27, 0x43, 0xD8))):
        got = pixel(int((bar + 0.5) * w / 7), 40)
        assert all(abs(g - t) <= 14 for g, t in zip(got, want)), "цвет полосы %d: %s вместо %s" % (bar, got, want)
    encode_case("тестовая картинка 1080p 60", 60, 4, "--test", "--size", "1920x1080")
    print("  цвета тестовой картинки после кодирования совпадают", flush=True)
    out, w, h = encode_case("HEVC, тестовая картинка 720p 60", 60, 4, "--test", "--size", "1280x720", "--encode", "libx265")
    frame = decoded_frame(out, w, h)
    got = pixel(int(5.5 * w / 7), 40)
    assert all(abs(g - t) <= 16 for g, t in zip(got, (0xE4, 0x00, 0x2B))), "HEVC: цвет полосы %s" % (got,)

    # 2. Настоящий экран. Кодировщика видеокарты на сервере нет, поэтому проверяем всё, кроме него самого:
    #    кадр готовится в текстуре кодировщика так же, как для NVENC, но перед сжатием скачивается (download).
    ref = dump("enc_ref", "--cpu", "--no-cursor")
    sw, sh = ref[0] & ~1, ref[1] & ~1
    crop = bytearray()
    for y in range(sh):
        crop += ref[3][y * ref[0] * 4:y * ref[0] * 4 + sw * 4]
    for name, mode in (("экран: кадр через обычную память", "cpu"), ("экран: кадр в текстуре кодировщика", "download")):
        out, w, h = encode_case(name, 60, 4, "--frames", mode)
        assert (w, h) == (sw, sh), (w, h, sw, sh)
        diff = bgra_diff(decoded_frame(out, w, h, 60), crop, w, h)
        print("  отличие от эталона: %.2f" % diff, flush=True)
        assert diff < 6.0, "картинка после кодирования не похожа на экран: %.2f" % diff
    out, w, h = encode_case("экран: уменьшение до 480, текстура", 60, 3, "--frames", "download", "--height", "480")
    assert h == 480, (w, h)
    # 3. Кодировщик, которому нельзя отдать кадр видеокарты, должен отказаться сразу — программа возьмёт другой способ.
    encode_case("отказ кодировщика (код 8)", 60, 2, "--frames", "gpu", expect_code=8)
    encode_case("нет такого кодировщика (код 8)", 60, 2, "--test", "--encode", "h264_none", expect_code=8)


def mux_check():
    """Движок сам кодирует видео и звук и упаковывает их: читаем его поток как есть и проверяем файл."""
    import threading
    pipe = r"\\.\pipe\mccap_mux_%d" % os.getpid()
    out = os.path.join(tempfile.gettempdir(), "mccap_mux_%d.ts" % os.getpid())
    helper = subprocess.Popen(
        [MCCAP, "--fps", "60", "--seconds", "6", "--pipe", pipe, "--parent", str(os.getpid()), "--stat", "1", "--test",
         "--size", "1280x720", "--encode", "libx264", "--bitrate", "20", "--mux", "--mux-audio", "--tracks", "1",
         "--track-other", "1"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    ready = helper.stdout.readline().decode().strip()
    assert ready.startswith("READY") and ready.split()[4] == "ts", (ready, helper.stderr.read().decode(errors="replace")[-1500:])

    def copy():
        with open(pipe, "rb", buffering=0) as src, open(out, "wb") as dst:
            while True:
                try:
                    data = src.read(262144)
                except OSError:
                    break
                if not data:
                    break
                dst.write(data)
    reader = threading.Thread(target=copy, daemon=True)
    reader.start()
    (frames, elapsed, late, over, wmax, hi, trimmed, overflow), err = finish_helper(helper)
    reader.join(20)
    assert not reader.is_alive(), "поток движка не закрылся"
    info = subprocess.run([FFMPEG, "-hide_banner", "-i", out, "-map", "0", "-f", "null", "-"],
                          capture_output=True, text=True, errors="replace").stderr
    streams = re.findall(r"Stream #0:\d+.*?: (Video|Audio): (\w+)", info)
    starts = re.findall(r"start: ([\d.]+)", info)
    decoded = [int(x) for x in re.findall(r"frame=\s*(\d+)", info)]
    audio_len = subprocess.run([FFMPEG, "-v", "error", "-i", out, "-map", "0:a:0", "-f", "s16le", "-ac", "1", "-ar", "48000", "-"],
                               capture_output=True).stdout
    seconds = len(audio_len) / 2 / 48000.0
    print("потоки: %s; кадров отправлено %d, в файле %d; звука %.2f c; файл %d КБ" % (
        ", ".join("%s %s" % s for s in streams), frames, decoded[-1] if decoded else 0, seconds, os.path.getsize(out) // 1024), flush=True)
    assert [s[0] for s in streams] == ["Video", "Audio", "Audio"] and streams[1][1] == "aac", streams
    assert decoded and abs(decoded[-1] - frames) <= 2 and abs(frames - 360) <= 10, (decoded[-1:], frames)
    assert abs(seconds - 6.0) < 0.35, "звук не той длины: %.2f c" % seconds
    assert "error:" not in err, err[-800:]


# ---- смена разрешения и поворот экрана во время записи

class DEVMODE(ctypes.Structure):
    _fields_ = [("dmDeviceName", ctypes.c_wchar * 32), ("dmSpecVersion", ctypes.c_ushort), ("dmDriverVersion", ctypes.c_ushort),
                ("dmSize", ctypes.c_ushort), ("dmDriverExtra", ctypes.c_ushort), ("dmFields", ctypes.c_ulong),
                ("dmPositionX", ctypes.c_long), ("dmPositionY", ctypes.c_long), ("dmDisplayOrientation", ctypes.c_ulong),
                ("dmDisplayFixedOutput", ctypes.c_ulong), ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short),
                ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short),
                ("dmFormName", ctypes.c_wchar * 32), ("dmLogPixels", ctypes.c_ushort), ("dmBitsPerPel", ctypes.c_ulong),
                ("dmPelsWidth", ctypes.c_ulong), ("dmPelsHeight", ctypes.c_ulong), ("dmDisplayFlags", ctypes.c_ulong),
                ("dmDisplayFrequency", ctypes.c_ulong), ("dmICMMethod", ctypes.c_ulong), ("dmICMIntent", ctypes.c_ulong),
                ("dmMediaType", ctypes.c_ulong), ("dmDitherType", ctypes.c_ulong), ("dmReserved1", ctypes.c_ulong),
                ("dmReserved2", ctypes.c_ulong), ("dmPanningWidth", ctypes.c_ulong), ("dmPanningHeight", ctypes.c_ulong)]


def display_mode(index=-1):
    dm = DEVMODE()
    dm.dmSize = ctypes.sizeof(DEVMODE)
    return dm if ctypes.windll.user32.EnumDisplaySettingsW(None, index, ctypes.byref(dm)) else None


def set_display(w, h, orientation):
    dm = display_mode()
    dm.dmPelsWidth, dm.dmPelsHeight, dm.dmDisplayOrientation = w, h, orientation
    dm.dmFields = 0x80000 | 0x100000 | 0x80   # ширина, высота, поворот
    return ctypes.windll.user32.ChangeDisplaySettingsW(ctypes.byref(dm), 0)


def gdi_frame(w, h):
    """Снимок экрана средствами Windows — эталон «как видит пользователь»."""
    raw = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "gdigrab", "-draw_mouse", "0", "-i", "desktop",
                          "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "bgra", "pipe:1"], capture_output=True).stdout
    return raw if len(raw) == w * h * 4 else None


def display_checks():
    import threading
    cur = display_mode()
    cw, ch = cur.dmPelsWidth, cur.dmPelsHeight
    try:
        # 1. Разрешение меняется посреди записи и возвращается: запись идёт дальше, размер кадра прежний.
        others, i = set(), 0
        while True:
            dm = display_mode(i)
            if dm is None:
                break
            if dm.dmBitsPerPel == cur.dmBitsPerPel and (dm.dmPelsWidth, dm.dmPelsHeight) != (cw, ch) and dm.dmPelsWidth >= 640:
                others.add((dm.dmPelsWidth, dm.dmPelsHeight))
            i += 1
        other = sorted(others, key=lambda m: abs(m[0] * m[1] - cw * ch))[0] if others else None
        if other is None:
            print("ВНИМАНИЕ: у экрана сервера один режим — смена разрешения не проверена", flush=True)
        else:
            results = []
            timers = [threading.Timer(2.5, lambda: results.append(set_display(other[0], other[1], 0))),
                      threading.Timer(5.5, lambda: results.append(set_display(cw, ch, 0)))]
            for t in timers:
                t.start()
            out, w, h = encode_case("смена разрешения на ходу %dx%d" % other, 60, 9, "--frames", "download")
            for t in timers:
                t.join()
            time.sleep(1.0)
            if results != [0, 0]:
                print("ВНИМАНИЕ: сервер не дал сменить разрешение (%s) — не проверено" % results, flush=True)
            else:
                assert (w, h) == (cw & ~1, ch & ~1), (w, h)
                for index in (60, 240, 500):   # до смены, во время и после возврата — кадры читаются, размер один
                    decoded_frame(out, w, h, index)
                ref = gdi_frame(cw, ch)
                if ref:
                    diff = bgra_diff(decoded_frame(out, w, h, 520), ref, w, h)
                    print("  после возврата разрешения отличие от экрана: %.2f" % diff, flush=True)
                    assert diff < 8.0, diff
                print("  запись пережила смену разрешения без перезапуска", flush=True)
        # 2. Монитор повёрнут боком: в записи картинка должна стоять так же, как на экране.
        for name, orientation in (("повёрнутый экран, 90", 1), ("перевёрнутый экран, 180", 2), ("повёрнутый экран, 270", 3)):
            rw, rh = (ch, cw) if orientation & 1 else (cw, ch)
            code = set_display(rw, rh, orientation)
            if code != 0:
                print("ВНИМАНИЕ: сервер не даёт повернуть экран (код %d) — поворот не проверен" % code, flush=True)
                break
            time.sleep(1.5)
            ref = gdi_frame(rw, rh)
            out, w, h = encode_case(name, 60, 3, "--frames", "download")
            assert (w, h) == (rw & ~1, rh & ~1), (w, h, rw, rh)
            if ref:
                diff = bgra_diff(decoded_frame(out, w, h, 60), ref, w, h)
                print("  отличие от экрана: %.2f" % diff, flush=True)
                assert diff < 8.0, "повёрнутая картинка не совпадает с экраном: %.2f" % diff
            else:
                print("  ВНИМАНИЕ: нет эталонного снимка — проверен только размер", flush=True)
    finally:
        set_display(cw, ch, 0)
        time.sleep(1.5)


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
    # читатель-имитация на загруженном сервере сам бывает медленным — пороги с запасом
    assert gaps <= n * 0.05, "кадры теряются при чтении порциями: %d из %d" % (gaps, n)
    assert med < 300, "картинка отстаёт: %.0f мс" % med
    gaps, med, mx, n = read_case("задержка 1,2 с на старте", "1280x720", 120, 7, startup_stall=1.2)
    assert med < 40, "отставание после задержки на старте не убрано: %.0f мс" % med
    gaps, med, mx, n = read_case("задержка + порции, 60 к/с", "1280x720", 60, 8, startup_stall=1.0, burst=8, pause=0.125)
    assert med < 300 and gaps <= n * 0.05, (gaps, med)

    print("обработка кадра на видеокарте против обычной:", flush=True)
    gpu_checks()

    print("захват одного окна:", flush=True)
    window_checks()

    print("кодирование в движке:", flush=True)
    encode_checks()

    print("запись одним движком (без ffmpeg):", flush=True)
    mux_check()

    print("смена разрешения и поворот экрана:", flush=True)
    display_checks()

    # Настоящий захват экрана: на сборочном сервере нет видеокарты, так что это только справка.
    probe = subprocess.run([MCCAP, "--probe", "--fps", "60", "--seconds", "3"], capture_output=True, text=True, errors="replace")
    print("захват экрана здесь: код %s | %s | %s" % (probe.returncode, probe.stdout.strip(), probe.stderr.strip()[-300:]))
    print("ДВИЖОК ЗАХВАТА: OK")
