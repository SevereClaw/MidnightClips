"""Окна поверх экрана: уведомление, точка «идёт запись» и «метроном» захвата.

Всё живёт в одном потоке tkinter и не забирает фокус у игры.

Метроном — окно в один пиксель в углу записываемого монитора, которое чуть меняет
цвет много раз в секунду. Оно нужно точному режиму захвата: тот выдаёт кадр только
когда экран обновился, и без метронома на неподвижной картинке запись бы замирала.
"""
import ctypes
import queue
import sys
import threading

YELLOW, BLACK, WHITE, RED = "#FFE100", "#0A0A0A", "#FFFFFF", "#E4002B"
KEY = "#010203"   # этот цвет становится прозрачным у окна-точки
WIN = sys.platform == "win32"


def _style(win, click_through=False, hide_from_capture=False):
    """Не активировать окно, не показывать в Alt+Tab; по желанию — пропускать клики и не попадать в запись."""
    if not WIN:
        return
    try:
        user32 = ctypes.windll.user32
        win.update_idletasks()
        hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
        style = user32.GetWindowLongW(hwnd, -20)
        style |= 0x08000000 | 0x00000080            # NOACTIVATE | TOOLWINDOW
        if click_through:
            style |= 0x00080000 | 0x00000020        # LAYERED | TRANSPARENT
        user32.SetWindowLongW(hwnd, -20, style)
        if hide_from_capture:
            user32.SetWindowDisplayAffinity(hwnd, 0x11)   # WDA_EXCLUDEFROMCAPTURE (Windows 10 2004+)
    except Exception:
        pass


class Toast(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="toast")
        self._queue = queue.Queue()
        self.errors = []      # для проверки: что пошло не так
        self.ticks = 0        # сколько раз «тикнул» метроном
        self.ready = threading.Event()

    # ------------------------------------------------- вызывается из любых потоков

    def show(self, title, text=""):
        self._queue.put(("toast", title, text))

    def set_indicator(self, on, rect=None, corner="tl"):
        """Точка в углу монитора rect={x,y,w,h}, пока идёт запись."""
        self._queue.put(("dot", bool(on), rect, corner))

    def set_tickle(self, on, rect=None, interval_ms=16):
        self._queue.put(("tickle", bool(on), rect, max(5, int(interval_ms))))

    # ------------------------------------------------------------------ поток окон

    def run(self):
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
        except Exception as exc:
            self.errors.append("tk: %s" % exc)
            return
        state = {"toast": None, "toast_timer": None, "dot": None, "dot_key": None,
                 "tick": None, "tick_key": None, "tick_ms": 16, "tick_on": False, "flip": False}

        def guard(fn):
            def wrapped(*args):
                try:
                    fn(*args)
                except Exception as exc:
                    self.errors.append("%s: %s" % (fn.__name__, exc))
            return wrapped

        def close(name):
            if state[name] is not None:
                try:
                    state[name].destroy()
                except Exception:
                    pass
                state[name] = None

        @guard
        def popup(title, text):
            close("toast")
            if state["toast_timer"]:
                root.after_cancel(state["toast_timer"])
            win = tk.Toplevel(root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.attributes("-alpha", 0.96)
            w, h = 360, 84
            win.geometry("%dx%d+%d+%d" % (w, h, win.winfo_screenwidth() - w - 28, 28))
            canvas = tk.Canvas(win, width=w, height=h, bg=BLACK, highlightthickness=0)
            canvas.pack()
            canvas.create_polygon(0, 0, 74, 0, 50, h, 0, h, fill=YELLOW, outline="")
            canvas.create_rectangle(16, 24, 44, 46, fill=BLACK, outline="")
            canvas.create_oval(25, 30, 35, 40, fill=RED, outline="")
            canvas.create_text(92, 28, text=title, anchor="w", fill=YELLOW, font=("Arial Black", 14, "italic"))
            canvas.create_text(92, 56, text=text, anchor="w", fill=WHITE, font=("Segoe UI", 10), width=w - 104)
            _style(win)
            state["toast"] = win
            state["toast_timer"] = root.after(2800, lambda: close("toast"))

        @guard
        def dot(on, rect, corner):
            key = (on, tuple(sorted((rect or {}).items())), corner)
            if key == state["dot_key"]:
                return
            state["dot_key"] = key
            close("dot")
            if not on:
                return
            rect = rect or {"x": 0, "y": 0, "w": root.winfo_screenwidth(), "h": root.winfo_screenheight()}
            size, margin = 18, 16
            x = rect["x"] + (margin if "l" in corner else rect["w"] - size - margin)
            y = rect["y"] + (margin if "t" in corner else rect["h"] - size - margin)
            win = tk.Toplevel(root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.geometry("%dx%d+%d+%d" % (size, size, x, y))
            win.configure(bg=KEY)
            if WIN:
                win.attributes("-transparentcolor", KEY)
            canvas = tk.Canvas(win, width=size, height=size, bg=KEY, highlightthickness=0)
            canvas.pack()
            canvas.create_oval(1, 1, size - 2, size - 2, fill=RED, outline=BLACK, width=2)
            _style(win, click_through=True, hide_from_capture=True)   # в самой записи точки не будет
            state["dot"] = win

        @guard
        def tickle(on, rect, interval):
            key = (on, tuple(sorted((rect or {}).items())))
            state["tick_ms"] = interval
            if key == state["tick_key"]:
                return
            state["tick_key"] = key
            close("tick")
            state["tick_on"] = on
            if not on:
                return
            rect = rect or {"x": 0, "y": 0}
            win = tk.Toplevel(root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.geometry("1x1+%d+%d" % (rect["x"], rect["y"]))
            win.configure(bg="#000000")
            _style(win)
            state["tick"] = win

        def tick():
            if state["tick_on"] and state["tick"] is not None:
                try:
                    state["flip"] = not state["flip"]
                    state["tick"].configure(bg="#020202" if state["flip"] else "#000000")
                    self.ticks += 1
                except Exception as exc:
                    self.errors.append("tick: %s" % exc)
                    state["tick_on"] = False
            root.after(state["tick_ms"], tick)

        def poll():
            for _ in range(20):
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item[0] == "toast":
                    popup(item[1], item[2])
                elif item[0] == "dot":
                    dot(item[1], item[2], item[3])
                elif item[0] == "tickle":
                    tickle(item[1], item[2], item[3])
            root.after(100, poll)

        root.after(50, poll)
        root.after(50, tick)
        self.ready.set()
        try:
            root.mainloop()
        except Exception as exc:
            self.errors.append("mainloop: %s" % exc)
