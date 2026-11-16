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
import time

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
        get_model() -> {"status", "recording", "chips": [{"text", "accent"}], "last", "hint",
        "tiles": [{"id", "title", "sub", "active", "alert", "key", "icon", "switch"}]};
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
                "timer": None, "anim": None, "size": (0, 0), "t0": 0.0, "intro": 1.0, "march": 0.0,
                "flash": (None, 0.0), "clock": "", "blink": None}
        GREY, DIM, SOFT = "#2A2A2A", "#161616", "#BDBDBD"
        DAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
        MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
                  "октября", "ноября", "декабря"]
        ALPHA = 0.92

        def menu_close():
            for name in ("timer", "anim"):
                if menu[name]:
                    try:
                        root.after_cancel(menu[name])
                    except Exception:
                        pass
                    menu[name] = None
            if menu["win"] is not None:
                try:
                    menu["win"].destroy()
                except Exception:
                    pass
            menu["win"] = menu["canvas"] = None
            self.menu_open = False

        def ease(t):
            t = max(0.0, min(1.0, t))
            return 1 - (1 - t) ** 3

        def menu_icon(canvas, kind, cx, cy, r, ink, fill):
            """Значок плитки — из простых фигур, цветом текста плитки."""
            if kind == "clip":        # кадр плёнки со стрелкой «играть»
                canvas.create_rectangle(cx - r, cy - int(r * 0.72), cx + r, cy + int(r * 0.72), fill=ink, outline="")
                canvas.create_polygon(cx - int(r * 0.3), cy - int(r * 0.42), cx + int(r * 0.48), cy,
                                      cx - int(r * 0.3), cy + int(r * 0.42), fill=fill, outline="")
            elif kind == "short":     # часы: клип покороче
                width = max(2, r // 5)
                canvas.create_oval(cx - r, cy - r, cx + r, cy + r, outline=ink, width=width)
                canvas.create_line(cx, cy, cx, cy - int(r * 0.6), fill=ink, width=width)
                canvas.create_line(cx, cy, cx + int(r * 0.45), cy + int(r * 0.2), fill=ink, width=width)
            elif kind == "rec":       # кружок записи
                canvas.create_oval(cx - r, cy - r, cx + r, cy + r, outline=ink, width=max(2, r // 5))
                canvas.create_oval(cx - r // 2, cy - r // 2, cx + r // 2, cy + r // 2, fill=ink, outline="")
            elif kind == "stop":      # квадрат «стоп»
                canvas.create_oval(cx - r, cy - r, cx + r, cy + r, outline=ink, width=max(2, r // 5))
                canvas.create_rectangle(cx - r // 2, cy - r // 2, cx + r // 2, cy + r // 2, fill=ink, outline="", tags="pulse_b")
            elif kind == "copy":      # два листа
                d = int(r * 0.35)
                canvas.create_rectangle(cx - r + d, cy - r, cx + r, cy + r - d, outline=ink, width=max(2, r // 5))
                canvas.create_rectangle(cx - r, cy - r + d, cx + r - d, cy + r, fill=ink, outline="")
            elif kind == "grid":      # галерея
                g = max(2, r // 7)
                for left, right in ((cx - r, cx - g), (cx + g, cx + r)):
                    for top, bottom in ((cy - r, cy - g), (cy + g, cy + r)):
                        canvas.create_rectangle(left, top, right, bottom, fill=ink, outline="")
            elif kind == "gear":      # настройки: три ползунка
                width = max(2, r // 5)
                for n, pos in enumerate((-0.4, 0.5, -0.1)):
                    y = cy + (n - 1) * int(r * 0.75)
                    canvas.create_line(cx - r, y, cx + r, y, fill=ink, width=width)
                    kx = cx + int(r * pos)
                    canvas.create_oval(kx - width * 2, y - width * 2, kx + width * 2, y + width * 2, fill=ink, outline="")

        @guard
        def menu_draw():
            canvas, model = menu["canvas"], menu["model"] or {}
            if canvas is None:
                return
            w, h = menu["size"]
            k = max(0.5, min(2.0, h / 1080.0, w / 1400.0))

            def S(v):
                return int(v * k)

            t = menu["intro"]
            e = ease(t)
            canvas.delete("all")

            # фон: тёмная косая полоса и «ленты» по краям, которые едут
            canvas.create_polygon(int(w * 0.60), 0, int(w * 0.84), 0, int(w * 0.54), h, int(w * 0.30), h, fill=DIM, outline="")
            canvas.create_polygon(int(w * 0.86), 0, int(w * 0.89), 0, int(w * 0.59), h, int(w * 0.56), h, fill=DIM, outline="")
            band, period = S(20), S(48)
            off = int(menu["march"]) % max(1, period)
            for y1, y2, lean in ((0, band, 1), (h - band, h, -1)):
                canvas.create_rectangle(0, y1, w, y2, fill=BLACK, outline="")
                x = -2 * period + off
                while x < w + period:
                    canvas.create_polygon(x, y2, x + period // 2, y2, x + period // 2 + lean * band, y1, x + lean * band, y1,
                                          fill=YELLOW, outline="", tags="march")
                    x += period

            # шапка: косая жёлтая полоса с названием выезжает слева
            hx = -int((1 - e) * S(820))
            canvas.create_polygon(hx, S(206), hx + S(712), S(206), hx + S(706), S(196), hx, S(196), fill=WHITE, outline="")
            canvas.create_polygon(hx, S(70), hx + S(770), S(70), hx + S(706), S(196), hx, S(196), fill=YELLOW, outline="")
            canvas.create_text(hx + S(60), S(112), text="MIDNIGHT CLIPS", anchor="w", fill=BLACK,
                               font=("Arial Black", S(30), "italic"))
            sx = hx + S(62)
            if model.get("recording"):
                canvas.create_oval(sx, S(151), sx + S(18), S(169), fill=RED, outline=BLACK, width=2, tags="pulse_a")
                sx += S(28)
            canvas.create_text(sx, S(160), text=model.get("status", ""), anchor="w", fill=BLACK,
                               font=("Segoe UI", S(13), "bold"))

            # часы и дата справа
            now = time.localtime()
            cx = w - S(60) + int((1 - e) * S(420))
            menu["clock"] = time.strftime("%H:%M", now)
            canvas.create_text(cx, S(108), text=menu["clock"], anchor="e", fill=YELLOW,
                               font=("Arial Black", S(52), "italic"), tags="clock")
            canvas.create_text(cx - S(4), S(164), anchor="e", fill=WHITE, font=("Segoe UI", S(14), "bold"),
                               text="%s, %d %s" % (DAYS[now.tm_wday], now.tm_mday, MONTHS[now.tm_mon - 1]))

            # строка сведений: что и чем записывается
            x, y = S(60), S(246) + int((1 - e) * S(30))
            for chip in model.get("chips", []):
                accent = bool(chip.get("accent"))
                item = canvas.create_text(x + S(18), y, text=chip.get("text", ""), anchor="w",
                                          fill=BLACK if accent else WHITE, font=("Segoe UI", S(11), "bold"))
                x2 = canvas.bbox(item)[2]
                canvas.create_polygon(x + S(8), y - S(15), x2 + S(24), y - S(15), x2 + S(16), y + S(15), x, y + S(15),
                                      fill=YELLOW if accent else GREY, outline="")
                canvas.tag_raise(item)
                x = x2 + S(34)
                if x > w - S(420):
                    break

            # плитки: въезжают справа по очереди
            tiles = model.get("tiles", [])
            cols = 3
            tw, th, gap, skew = S(400), S(160), S(30), S(32)
            rows = (len(tiles) + cols - 1) // cols
            x0 = (w - (cols * tw + (cols - 1) * gap)) // 2
            y0 = max(S(290), (h - (rows * th + (rows - 1) * gap)) // 2 + S(36))
            flash_id = menu["flash"][0] if time.monotonic() < menu["flash"][1] else None
            menu["boxes"] = []
            for i, tile in enumerate(tiles):
                bx = x0 + (i % cols) * (tw + gap)
                by = y0 + (i // cols) * (th + gap)
                menu["boxes"].append((bx, by, bx + tw, by + th, tile["id"]))
                ti = ease(t * 1.7 - i * 0.075)
                if ti <= 0:
                    continue
                hover = menu["hover"] == tile["id"]
                x = bx + int((1 - ti) * S(220)) - (S(6) if hover else 0)
                y = by - (S(6) if hover else 0)
                active = bool(tile.get("active"))
                fill = RED if tile.get("alert") else (YELLOW if active else GREY)
                ink = WHITE if tile.get("alert") else (BLACK if active else SOFT)
                if tile["id"] == flash_id:
                    fill, ink = WHITE, BLACK
                if hover:   # «тень» — плитка приподнимается
                    d = S(12)
                    canvas.create_polygon(x + skew + d, y + d, x + tw + d, y + d, x + tw - skew + d, y + th + d, x + d, y + th + d,
                                          fill=WHITE, outline="")
                canvas.create_polygon(x + skew, y, x + tw, y, x + tw - skew, y + th, x, y + th, fill=fill, outline="")
                # номер — в чёрном «ярлыке»
                nx, ny = x + skew + S(8), y + S(12)
                canvas.create_polygon(nx, ny, nx + S(44), ny, nx + S(37), ny + S(34), nx - S(7), ny + S(34), fill=ink, outline="")
                canvas.create_text(nx + S(18), ny + S(16), text=str(i + 1), fill=fill, font=("Arial Black", S(14), "italic"))
                if tile.get("key"):
                    canvas.create_text(x + tw - S(22), y + S(28), text=tile["key"], anchor="e", fill=ink,
                                       font=("Segoe UI", S(11), "bold"))
                room = tw - S(170)
                canvas.create_text(x + S(44), y + S(82), text=tile.get("title", ""), anchor="w", fill=ink,
                                   font=("Arial Black", S(18), "italic"), width=room + S(20))
                canvas.create_text(x + S(30), y + S(128), text=tile.get("sub", ""), anchor="w", fill=ink,
                                   font=("Segoe UI", S(12)), width=room)
                if "switch" in tile:   # выключатель
                    on = bool(tile["switch"])
                    pw, ph = S(66), S(32)
                    px, py = x + tw - skew - S(96), y + th - S(54)
                    track = BLACK if on else "#555555"
                    canvas.create_oval(px, py, px + ph, py + ph, fill=track, outline="")
                    canvas.create_oval(px + pw - ph, py, px + pw, py + ph, fill=track, outline="")
                    canvas.create_rectangle(px + ph // 2, py, px + pw - ph // 2, py + ph, fill=track, outline="")
                    kx = px + pw - ph if on else px
                    m = S(5)
                    canvas.create_oval(kx + m, py + m, kx + ph - m, py + ph - m, fill=YELLOW if on else SOFT, outline="")
                    canvas.create_text(px + pw // 2, py - S(14), text="ВКЛ" if on else "ВЫКЛ", fill=ink,
                                       font=("Arial Black", S(10), "italic"))
                elif tile.get("icon"):
                    menu_icon(canvas, tile["icon"], x + tw - skew - S(62), y + th // 2 + S(14), S(24), ink, fill)

            # последний клип и подсказка
            ly = h - S(128) + int((1 - e) * S(60))
            last = model.get("last", "")
            if last:
                item = canvas.create_text(x0 + S(22), ly, text="ПОСЛЕДНИЙ КЛИП", anchor="w", fill=BLACK,
                                          font=("Arial Black", S(11), "italic"))
                x2 = canvas.bbox(item)[2]
                canvas.create_polygon(x0 + S(10), ly - S(16), x2 + S(26), ly - S(16), x2 + S(16), ly + S(16), x0, ly + S(16),
                                      fill=YELLOW, outline="")
                canvas.tag_raise(item)
                canvas.create_text(x2 + S(40), ly, text=last, anchor="w", fill=WHITE, font=("Segoe UI", S(13), "bold"))
            canvas.create_text(w // 2, h - S(66), text=model.get("hint", ""), fill=SOFT, font=("Segoe UI", S(12)))
            menu["blink"] = None

        def menu_refresh():
            if menu["win"] is None:
                return
            try:
                menu["model"] = menu["get"]()
            except Exception as exc:
                self.errors.append("menu model: %s" % exc)
            menu_draw()
            menu["timer"] = root.after(500, menu_refresh)

        @guard
        def menu_anim_step():
            canvas = menu["canvas"]
            now = time.monotonic()
            w, h = menu["size"]
            k = max(0.5, min(2.0, h / 1080.0, w / 1400.0))
            period = max(1, int(48 * k))
            if menu["intro"] < 1.0:
                menu["intro"] = min(1.0, (now - menu["t0"]) / 0.38)
                try:
                    menu["win"].attributes("-alpha", ALPHA * min(1.0, 0.25 + menu["intro"] * 1.5))
                except Exception:
                    pass
                menu_draw()
                return
            if menu["flash"][0] and now >= menu["flash"][1]:
                menu["flash"] = (None, 0.0)
                menu_draw()
            # ленты едут, огонёк записи мигает, часы идут
            step = max(1, int(2 * k))
            before = int(menu["march"]) % period
            menu["march"] = (menu["march"] + step) % period
            canvas.move("march", (int(menu["march"]) % period) - before, 0)
            blink = int(now * 2) % 2 == 0
            if blink != menu["blink"]:
                menu["blink"] = blink
                canvas.itemconfigure("pulse_a", fill=RED if blink else "#6E0014")
                canvas.itemconfigure("pulse_b", state="normal" if blink else "hidden")
            clock = time.strftime("%H:%M")
            if clock != menu["clock"]:
                menu["clock"] = clock
                canvas.itemconfigure("clock", text=clock)

        def menu_anim():
            if menu["win"] is None:
                return
            menu_anim_step()
            menu["anim"] = root.after(33, menu_anim)

        def menu_act(ident):
            if menu["win"] is None or not ident:
                return
            act = menu["act"]
            menu["flash"] = (ident, time.monotonic() + 0.16)   # плитка вспыхивает при нажатии
            menu_draw()

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

        def menu_step(dx, dy):
            """Стрелки на клавиатуре: перейти к соседней плитке."""
            ids = [box[4] for box in menu["boxes"]]
            if not ids:
                return
            if menu["hover"] not in ids:
                menu["hover"] = ids[0]
            else:
                i = ids.index(menu["hover"])
                col, row = i % 3 + dx, i // 3 + dy
                rows = (len(ids) + 2) // 3
                j = (row % rows) * 3 + col % 3
                menu["hover"] = ids[min(j, len(ids) - 1)]
            menu_draw()

        @guard
        def menu_toggle(rect, get_model, on_action):
            if menu["win"] is not None:
                menu_close()
                return
            rect = rect or {"x": 0, "y": 0, "w": root.winfo_screenwidth(), "h": root.winfo_screenheight()}
            win = tk.Toplevel(root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            instant = bool(os.environ.get("MC_MENU_IN_CAPTURE"))   # для проверок — сразу готовый вид
            win.attributes("-alpha", ALPHA if instant else ALPHA * 0.25)
            win.geometry("%dx%d+%d+%d" % (rect["w"], rect["h"], rect["x"], rect["y"]))
            win.configure(bg=BLACK)
            canvas = tk.Canvas(win, width=rect["w"], height=rect["h"], bg=BLACK, highlightthickness=0, cursor="hand2")
            canvas.pack()
            menu.update(win=win, canvas=canvas, get=get_model, act=on_action, hover=None, size=(rect["w"], rect["h"]),
                        t0=time.monotonic(), intro=1.0 if instant else 0.0, flash=(None, 0.0), blink=None)

            def motion(event):
                ident = menu_at(event.x, event.y)
                if ident != menu["hover"]:
                    menu["hover"] = ident
                    menu_draw()

            def key(event):
                moves = {"Left": (-1, 0), "Right": (1, 0), "Up": (0, -1), "Down": (0, 1)}
                if event.keysym == "Escape":
                    menu_close()
                elif event.keysym in moves:
                    menu_step(*moves[event.keysym])
                elif event.keysym in ("Return", "KP_Enter", "space"):
                    menu_act(menu["hover"])
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
            menu_anim()

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
