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
    ok = winutil.begin_drag(hwnd, edge)
    time.sleep(0.15)
    for i in range(1, 21):
        user32.SetCursorPos(start[0] + delta[0] * i // 20, start[1] + delta[1] * i // 20)
        time.sleep(0.025)
    mouse(False)
    time.sleep(0.6)
    return ok


def check(window):
    try:
        time.sleep(3)
        hwnd = winutil.app_window("MidnightClips")
        assert hwnd, "окно не найдено"
        winutil.prepare_frameless(hwnd)
        x, y, w, h = winutil._rect(hwnd)
        print("окно:", (x, y, w, h), flush=True)

        assert drag(hwnd, "move", (x + 300, y + 15), (140, 80)), "перетаскивание не началось"
        nx, ny, nw, nh = winutil._rect(hwnd)
        print("после перетаскивания:", (nx, ny, nw, nh), flush=True)
        assert abs(nx - x - 140) <= 6 and abs(ny - y - 80) <= 6 and (nw, nh) == (w, h), "окно не переехало за мышью"

        assert drag(hwnd, "br", (nx + nw - 3, ny + nh - 3), (90, 50)), "растягивание не началось"
        rx, ry, rw, rh = winutil._rect(hwnd)
        print("после растягивания:", (rx, ry, rw, rh), flush=True)
        assert (rx, ry) == (nx, ny) and abs(rw - nw - 90) <= 6 and abs(rh - nh - 50) <= 6, "окно не растянулось"

        before = winutil._rect(hwnd)
        assert winutil.toggle_maximize(hwnd) and winutil.is_maximized(hwnd), "не развернулось"
        assert winutil._rect(hwnd) == winutil._work_area(hwnd), "развёрнутое окно не на рабочей области"
        assert not winutil.toggle_maximize(hwnd) and winutil._rect(hwnd) == before, "не вернулось к прежнему размеру"

        ctypes.windll.user32.SetCursorPos(10, 10)
        assert not winutil.begin_drag(hwnd, "move"), "без зажатой кнопки перетаскивание начинаться не должно"
        winutil.minimize(hwnd)
        time.sleep(0.4)
        assert ctypes.windll.user32.IsIconic(hwnd), "не свернулось"
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(repr(exc))
    finally:
        window.destroy()


if __name__ == "__main__":
    if sys.platform != "win32":
        print("только для Windows — пропуск")
        sys.exit(0)
    import webview
    winutil.set_dpi_aware()
    win = webview.create_window("MidnightClips", html=HTML, frameless=True, easy_drag=False,
                                width=900, height=600, x=120, y=120, min_size=(400, 300))
    webview.start(check, win)
    print("ошибки:", result["errors"])
    assert result.get("ok"), result["errors"]
    print("ОКНО БЕЗ РАМКИ: OK")
