"""Маленькое уведомление в углу экрана («Клип сохранён»). Не забирает фокус у игры."""
import ctypes
import queue
import sys
import threading

YELLOW, BLACK, WHITE = "#FFE100", "#0A0A0A", "#FFFFFF"


class Toast(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="toast")
        self._queue = queue.Queue()

    def show(self, title, text=""):
        self._queue.put((title, text))

    def run(self):
        try:
            import tkinter as tk
        except Exception:
            return
        try:
            root = tk.Tk()
            root.withdraw()
        except Exception:
            return
        state = {"win": None, "timer": None}

        def close():
            if state["win"] is not None:
                try:
                    state["win"].destroy()
                except Exception:
                    pass
                state["win"] = None

        def popup(title, text):
            close()
            if state["timer"]:
                root.after_cancel(state["timer"])
            win = tk.Toplevel(root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            try:
                win.attributes("-alpha", 0.96)
            except Exception:
                pass
            w, h = 360, 84
            x = win.winfo_screenwidth() - w - 28
            win.geometry("%dx%d+%d+%d" % (w, h, x, 28))
            canvas = tk.Canvas(win, width=w, height=h, bg=BLACK, highlightthickness=0)
            canvas.pack()
            canvas.create_polygon(0, 0, 74, 0, 50, h, 0, h, fill=YELLOW, outline="")
            canvas.create_rectangle(16, 24, 44, 46, fill=BLACK, outline="")
            canvas.create_oval(25, 30, 35, 40, fill="#E4002B", outline="")
            canvas.create_text(92, 28, text=title, anchor="w", fill=YELLOW,
                               font=("Arial Black", 14, "italic"))
            canvas.create_text(92, 56, text=text, anchor="w", fill=WHITE, font=("Segoe UI", 10),
                               width=w - 104)
            if sys.platform == "win32":
                try:  # не активировать окно и не показывать его в Alt+Tab
                    user32 = ctypes.windll.user32
                    hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
                    style = user32.GetWindowLongW(hwnd, -20)
                    user32.SetWindowLongW(hwnd, -20, style | 0x08000000 | 0x00000080)
                except Exception:
                    pass
            state["win"] = win
            state["timer"] = root.after(2800, close)

        def poll():
            try:
                title, text = self._queue.get_nowait()
            except queue.Empty:
                pass
            else:
                try:
                    popup(title, text)
                except Exception:
                    pass
            root.after(120, poll)

        root.after(120, poll)
        try:
            root.mainloop()
        except Exception:
            pass
