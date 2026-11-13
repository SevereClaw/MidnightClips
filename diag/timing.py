"""Опыт на настоящей Windows: как ffmpeg отмеряет кадры при захвате экрана."""
import ctypes, os, re, subprocess, sys, threading, time

FF = os.path.abspath("ffmpeg.exe")

def animate(stop, static_after=None):
    import tkinter as tk
    root = tk.Tk(); root.geometry("500x400+50+50"); root.attributes("-topmost", True)
    c = tk.Canvas(root, width=500, height=400, bg="black"); c.pack()
    r = c.create_rectangle(0, 0, 60, 60, fill="yellow")
    t0 = time.perf_counter(); n = [0]
    def step():
        if stop.is_set(): root.destroy(); return
        if static_after is None or time.perf_counter() - t0 < static_after:
            n[0] += 1; x = (n[0] * 7) % 440; c.coords(r, x, 100, x + 60, 160)
        root.after(4, step)
    root.after(4, step); root.mainloop()

def run(name, inp, extra, seconds=8, static_after=None, tickle=False):
    stop = threading.Event()
    th = threading.Thread(target=animate, args=(stop, static_after), daemon=True); th.start()
    tk_stop = threading.Event()
    if tickle: threading.Thread(target=tickler, args=(tk_stop,), daemon=True).start()
    time.sleep(1.5)
    cmd = [FF, "-hide_banner", "-loglevel", "info", "-y"] + inp + ["-t", str(seconds)] + extra
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    wall = time.time() - t0
    stop.set(); tk_stop.set(); time.sleep(0.3)
    err = p.stderr
    stat = re.findall(r"frame=\s*(\d+).*?(?:dup=(\d+))?\s*(?:drop=(\d+))?\s*speed", err)
    pts = [float(x) for x in re.findall(r"pts_time:([\d.]+)", err)]
    line = "%-34s код=%s  стена=%.1fc" % (name, p.returncode, wall)
    if stat: line += "  кадров=%s dup=%s drop=%s" % stat[-1]
    if len(pts) > 5:
        d = sorted((b - a) * 1000 for a, b in zip(pts, pts[1:]))
        span = pts[-1] - pts[0]
        line += "  | снято %d за %.1fc = %.1f к/с; интервал мс: мин %.1f, медиана %.1f, 90%% %.1f, макс %.1f" % (
            len(pts), span, (len(pts) - 1) / span if span else 0, d[0], d[len(d)//2], d[int(len(d)*0.9)], d[-1])
    print(line, flush=True)
    if p.returncode != 0 or (not stat and len(pts) <= 5):
        print("   " + "\n   ".join(err.strip().splitlines()[-6:]), flush=True)

def tickler(stop):
    """1x1 окно, меняющее прозрачность: заставляет рабочий стол обновляться."""
    import tkinter as tk
    root = tk.Tk(); root.overrideredirect(True); root.geometry("1x1+0+0"); root.attributes("-topmost", True)
    a = [0]
    def step():
        if stop.is_set(): root.destroy(); return
        a[0] ^= 1; root.attributes("-alpha", 0.02 + 0.01 * a[0]); root.after(200, step)
    root.after(200, step); root.mainloop()

def sleep_probe():
    k = ctypes.windll.kernel32; out = []
    for ms in (1, 4, 16):
        t = time.perf_counter()
        for _ in range(20): k.Sleep(ms)
        out.append("Sleep(%d)=%.1fмс" % (ms, (time.perf_counter() - t) / 20 * 1000))
    return ", ".join(out)

print("Windows:", sys.getwindowsversion(), flush=True)
print("точность Sleep в этом процессе:", sleep_probe(), flush=True)
info = ["-vf", "showinfo", "-f", "null", "-"]
dda = lambda opts: ["-f", "lavfi", "-i", "ddagrab=%s,hwdownload,format=bgra" % opts]
gdi = lambda fps: ["-f", "gdigrab", "-framerate", str(fps), "-i", "desktop"]
print("--- реальные интервалы между кадрами (без выравнивания)")
run("gdigrab 60", gdi(60), ["-fps_mode", "passthrough"] + info)
run("gdigrab 144", gdi(144), ["-fps_mode", "passthrough"] + info)
run("ddagrab 60", dda("framerate=60"), ["-fps_mode", "passthrough"] + info)
run("ddagrab 144", dda("framerate=144"), ["-fps_mode", "passthrough"] + info)
run("ddagrab 500 dup=0", dda("framerate=500:dup_frames=0"), ["-fps_mode", "passthrough"] + info)
run("ddagrab 250 dup=1", dda("framerate=250"), ["-fps_mode", "passthrough"] + info)
print("--- как сейчас в программе (выравнивание до 60) и предлагаемый вариант")
run("ddagrab 60 -> cfr 60", dda("framerate=60"), ["-r", "60", "-fps_mode", "cfr", "-f", "null", "-"])
run("ddagrab 500 dup=0 -> cfr 60", dda("framerate=500:dup_frames=0"), ["-r", "60", "-fps_mode", "cfr", "-f", "null", "-"])
print("--- экран замирает через 3 с: что будет с dup=0")
run("dup=0, статика", dda("framerate=500:dup_frames=0"), ["-fps_mode", "passthrough"] + info, static_after=3)
run("dup=0, статика + тиклер", dda("framerate=500:dup_frames=0"), ["-fps_mode", "passthrough"] + info, static_after=3, tickle=True)
print("--- помогает ли timeBeginPeriod(1) в ДРУГОМ процессе")
ctypes.windll.winmm.timeBeginPeriod(1)
print("точность Sleep после timeBeginPeriod(1):", sleep_probe(), flush=True)
run("gdigrab 60 (+tBP у родителя)", gdi(60), ["-fps_mode", "passthrough"] + info)
run("ddagrab 144 (+tBP у родителя)", dda("framerate=144"), ["-fps_mode", "passthrough"] + info)
