"""Окно без рамки со своей полосой заголовка (запускается при сборке на Windows):
перетаскивание и растягивание мышью ведёт сама Windows, разворачивание — на рабочую область."""
import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from midnightclips import winutil  # noqa: E402

HTML = ("<html><body style='margin:0;background:#FFE100'>"
        "<div style='height:34px;background:#000'></div></body></html>")
result = {"errors": []}


def mouse(down):
    ctypes.windll.user32.mouse_event(0x0002 if down else 0x0004, 0, 0, 0, 0)


def drag(hwnd, edge, start, delta):
    user32 = ctypes.windll.user32
    user32.SetCursorPos(*start)
    time.sleep(0.15)
    mouse(True)
    time.sleep(0.15)
    import threading
    out = {}
    started = threading.Event()
    worker = threading.Thread(target=lambda: out.update(ok=winutil.begin_drag(hwnd, edge, ready=started.set)), daemon=True)
    worker.start()   # как вызов из интерфейса: возвращается, когда кнопку отпустят
    started.wait(5)   # на загруженном сервере поток может стартовать не сразу — двигать мышь только после него
    time.sleep(0.05)
    for i in range(1, 21):
        user32.SetCursorPos(start[0] + delta[0] * i // 20, start[1] + delta[1] * i // 20)
        time.sleep(0.025)
    time.sleep(0.1)
    mouse(False)
    worker.join(3)
    time.sleep(0.3)
    return out.get("ok") and not worker.is_alive()


def check(window):
    try:
        result["step"] = "ждём окно"
        print("окно создаётся…", flush=True)
        time.sleep(3)
        hwnd = winutil.app_window("MidnightClips")
        assert hwnd, "окно не найдено"
        winutil.prepare_frameless(hwnd)
        x, y, w, h = winutil._rect(hwnd)
        print("окно:", (x, y, w, h), flush=True)

        result["step"] = "перетаскивание"
        assert drag(hwnd, "move", (x + 200, y + 15), (100, 60)), "перетаскивание не началось"
        nx, ny, nw, nh = winutil._rect(hwnd)
        print("после перетаскивания:", (nx, ny, nw, nh), flush=True)
        assert abs(nx - x - 100) <= 6 and abs(ny - y - 60) <= 6 and (nw, nh) == (w, h), "окно не переехало за мышью"

        result["step"] = "растягивание"
        assert drag(hwnd, "br", (nx + nw - 3, ny + nh - 3), (90, 50)), "растягивание не началось"
        rx, ry, rw, rh = winutil._rect(hwnd)
        print("после растягивания:", (rx, ry, rw, rh), flush=True)
        assert (rx, ry) == (nx, ny) and abs(rw - nw - 90) <= 6 and abs(rh - nh - 50) <= 6, "окно не растянулось"

        result["step"] = "разворачивание"
        before = winutil._rect(hwnd)
        assert winutil.toggle_maximize(hwnd) and winutil.is_maximized(hwnd), "не развернулось"
        assert winutil._rect(hwnd) == winutil._work_area(hwnd), "развёрнутое окно не на рабочей области"
        assert not winutil.toggle_maximize(hwnd) and winutil._rect(hwnd) == before, "не вернулось к прежнему размеру"

        ctypes.windll.user32.SetCursorPos(10, 10)
        assert not winutil.begin_drag(hwnd, "move"), "без зажатой кнопки перетаскивание начинаться не должно"
        winutil.minimize(hwnd)
        time.sleep(0.4)
        assert ctypes.windll.user32.IsIconic(hwnd), "не свернулось"
        result["step"] = "перетаскивание файлов"
        from webview.dom import DOMEventHandler   # то же, чем программа принимает видео из Проводника
        window.events.loaded.wait(10)
        window.dom.document.events.drop += DOMEventHandler(lambda e: None, True, True)
        result["ok"] = True
        print("ОКНО БЕЗ РАМКИ: OK", flush=True)
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(repr(exc))
    finally:
        print("шаг:", result.get("step"), "ошибки:", result["errors"], flush=True)
        if result.get("ok") or result["errors"]:
            os._exit(0 if result.get("ok") else 1)   # закрывать окно незачем: проверка закончена


if __name__ == "__main__":
    if sys.platform != "win32":
        print("только для Windows — пропуск")
        sys.exit(0)
    import threading
    import webview

    def watchdog():   # окно или WebView2 повисли — не держать сборку, показать, где встали
        time.sleep(90)
        print("ЗАВИСЛО. ошибки:", result["errors"], "шаг:", result.get("step"), flush=True)
        os._exit(1)
    threading.Thread(target=watchdog, daemon=True).start()
    winutil.set_dpi_aware()
    win = webview.create_window("MidnightClips", html=HTML, frameless=True, easy_drag=False,
                                width=640, height=420, x=60, y=60, min_size=(400, 300))
    webview.start(check, win)
    print("ОКНО БЕЗ РАМКИ: окно закрылось раньше проверки", result["errors"])
    sys.exit(1)
