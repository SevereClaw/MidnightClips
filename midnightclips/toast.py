"""Окна поверх экрана: уведомление, точка «идёт запись» и «метроном» захвата.

Всё живёт в одном потоке tkinter и не забирает фокус у игры.

Метроном — окно в один пиксель в углу записываемого монитора, которое чуть меняет
цвет много раз в секунду. Оно нужно точному режиму захвата: тот выдаёт кадр только
когда экран обновился, и без метронома на неподвижной картинке запись бы замирала.
"""
import ctypes
import os
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
        self.tickle_on = False
        self.ready = threading.Event()
        self.menu_open = False   # открыто ли меню поверх игры

    # ------------------------------------------------- вызывается из любых потоков

    def show(self, title, text=""):
        self._queue.put(("toast", title, text))

    def set_indicator(self, on, rect=None, corner="tl"):
        """Точка в углу монитора rect={x,y,w,h}, пока идёт запись."""
        self._queue.put(("dot", bool(on), rect, corner))

    def set_tickle(self, on, rect=None, interval_ms=16):
        self._queue.put(("tickle", bool(on), rect, max(5, int(interval_ms))))

    def toggle_menu(self, rect, get_model, on_action):
        """Меню поверх игры (как Alt+Z в ShadowPlay): открыть или закрыть.
        get_model() -> {"status": строка, "tiles": [{"id", "title", "sub", "active"}]};
        on_action(id) -> True, если после действия меню нужно закрыть."""
        self._queue.put(("menu", rect, get_model, on_action))

    def close_menu(self):
        self._queue.put(("menu_close",))

    def menu_press(self, ident):
        """Нажать плитку меню (для проверок)."""
        self._queue.put(("menu_press", ident))

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
            self.tickle_on = on
            if not on:
                return
            rect = rect or {"x": 0, "y": 0}
            win = tk.Toplevel(root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.geometry("2x2+%d+%d" % (rect["x"], rect["y"]))
            win.configure(bg="#000000")
            _style(win)
            state["tick"] = win

        menu = {"win": None, "canvas": None, "model": None, "get": None, "act": None, "boxes": [], "hover": None,
                "timer": None, "size": (0, 0)}

        def menu_close():
            if menu["timer"]:
                try:
                    root.after_cancel(menu["timer"])
                except Exception:
                    pass
                menu["timer"] = None
            if menu["win"] is not None:
                try:
                    menu["win"].destroy()
                except Exception:
                    pass
            menu["win"] = menu["canvas"] = None
            self.menu_open = False

        @guard
        def menu_draw():
            canvas, model = menu["canvas"], menu["model"] or {}
            if canvas is None:
                return
            w, h = menu["size"]
            k = max(0.6, min(2.0, h / 1080.0))
            canvas.delete("all")
            # шапка: косая жёлтая полоса с названием
            canvas.create_polygon(0, int(70 * k), int(760 * k), int(70 * k), int(700 * k), int(190 * k), 0, int(190 * k),
                                  fill=YELLOW, outline="")
            canvas.create_text(int(60 * k), int(112 * k), text="MIDNIGHT CLIPS", anchor="w", fill=BLACK,
                               font=("Arial Black", int(30 * k), "italic"))
            canvas.create_text(int(62 * k), int(160 * k), text=model.get("status", ""), anchor="w", fill=BLACK,
                               font=("Segoe UI", int(13 * k), "bold"))
            tiles = model.get("tiles", [])
            cols = 3
            tw, th, gap, skew = int(400 * k), int(170 * k), int(34 * k), int(34 * k)
            rows = (len(tiles) + cols - 1) // cols
            x0 = (w - (cols * tw + (cols - 1) * gap)) // 2
            y0 = max(int(250 * k), (h - (rows * th + (rows - 1) * gap)) // 2)
            menu["boxes"] = []
            for i, tile in enumerate(tiles):
                x = x0 + (i % cols) * (tw + gap)
                y = y0 + (i // cols) * (th + gap)
                hover = menu["hover"] == tile["id"]
                active = bool(tile.get("active"))
                fill = RED if tile.get("alert") else (YELLOW if active else "#2A2A2A")
                ink = WHITE if (tile.get("alert") or not active) else BLACK
                if hover:
                    canvas.create_polygon(x + skew - 8, y - 8, x + tw + 8, y - 8, x + tw - skew + 8, y + th + 8, x - 8, y + th + 8,
                                          fill=WHITE, outline="")
                canvas.create_polygon(x + skew, y, x + tw, y, x + tw - skew, y + th, x, y + th, fill=fill, outline="")
                canvas.create_text(x + skew + int(22 * k), y + int(34 * k), text=str(i + 1), anchor="w", fill=ink,
                                   font=("Arial Black", int(15 * k), "italic"))
                canvas.create_text(x + int(46 * k), y + int(86 * k), text=tile.get("title", ""), anchor="w", fill=ink,
                                   font=("Arial Black", int(19 * k), "italic"), width=tw - int(90 * k))
                canvas.create_text(x + int(34 * k), y + int(134 * k), text=tile.get("sub", ""), anchor="w", fill=ink,
                                   font=("Segoe UI", int(12 * k)), width=tw - int(90 * k))
                menu["boxes"].append((x, y, x + tw, y + th, tile["id"]))
            canvas.create_text(w // 2, h - int(70 * k), text=model.get("hint", ""), fill=WHITE, font=("Segoe UI", int(13 * k)))

        def menu_refresh():
            if menu["win"] is None:
                return
            try:
                menu["model"] = menu["get"]()
            except Exception as exc:
                self.errors.append("menu model: %s" % exc)
            menu_draw()
            menu["timer"] = root.after(500, menu_refresh)

        def menu_act(ident):
            if menu["win"] is None or not ident:
                return
            act = menu["act"]

            def work():   # действие может идти долго — не в потоке окон
                try:
                    if act(ident):
                        self._queue.put(("menu_close",))
                except Exception as exc:
                    self.errors.append("menu action: %s" % exc)
            threading.Thread(target=work, daemon=True).start()

        def menu_at(x, y):
            for x1, y1, x2, y2, ident in menu["boxes"]:
                if x1 <= x <= x2 and y1 <= y <= y2:
                    return ident
            return None

        @guard
        def menu_toggle(rect, get_model, on_action):
            if menu["win"] is not None:
                menu_close()
                return
            rect = rect or {"x": 0, "y": 0, "w": root.winfo_screenwidth(), "h": root.winfo_screenheight()}
            win = tk.Toplevel(root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.attributes("-alpha", 0.9)
            win.geometry("%dx%d+%d+%d" % (rect["w"], rect["h"], rect["x"], rect["y"]))
            win.configure(bg=BLACK)
            canvas = tk.Canvas(win, width=rect["w"], height=rect["h"], bg=BLACK, highlightthickness=0, cursor="hand2")
            canvas.pack()
            menu.update(win=win, canvas=canvas, get=get_model, act=on_action, hover=None, size=(rect["w"], rect["h"]))

            def motion(event):
                ident = menu_at(event.x, event.y)
                if ident != menu["hover"]:
                    menu["hover"] = ident
                    menu_draw()

            def key(event):
                if event.keysym == "Escape":
                    menu_close()
                elif event.char and event.char.isdigit():
                    tiles = (menu["model"] or {}).get("tiles", [])
                    index = int(event.char) - 1
                    if 0 <= index < len(tiles):
                        menu_act(tiles[index]["id"])

            canvas.bind("<Motion>", motion)
            canvas.bind("<Button-1>", lambda e: menu_act(menu_at(e.x, e.y)))
            win.bind("<Key>", key)
            if WIN:   # в запись меню не попадает; фокус берём, чтобы работали Esc и цифры
                try:
                    user32 = ctypes.windll.user32
                    win.update_idletasks()
                    hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
                    user32.SetWindowLongW(hwnd, -20, user32.GetWindowLongW(hwnd, -20) | 0x00000080)
                    if not os.environ.get("MC_MENU_IN_CAPTURE"):   # (для проверок меню оставляют видимым в снимке)
                        user32.SetWindowDisplayAffinity(hwnd, 0x11)
                    user32.SetForegroundWindow(hwnd)
                except Exception:
                    pass
            try:
                win.focus_force()
            except Exception:
                pass
            self.menu_open = True
            menu_refresh()

        def tick():
            if state["tick_on"] and state["tick"] is not None:
                try:
                    state["flip"] = not state["flip"]
                    state["tick"].configure(bg="#0C0C0C" if state["flip"] else "#000000")
                    state["tick"].update_idletasks()   # перерисовать сразу, а не «когда-нибудь»
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
                elif item[0] == "menu":
                    menu_toggle(item[1], item[2], item[3])
                elif item[0] == "menu_close":
                    menu_close()
                elif item[0] == "menu_press":
                    menu_act(item[1])
            root.after(100, poll)

        root.after(50, poll)
        root.after(50, tick)
        self.ready.set()
        try:
            root.mainloop()
        except Exception as exc:
            self.errors.append("mainloop: %s" % exc)
