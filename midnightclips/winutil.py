"""Мелкие обращения к Windows через ctypes. На других системах — безопасные заглушки."""
import ctypes
import os
import subprocess
import sys

from . import APP_NAME

WIN = sys.platform == "win32"
NO_WINDOW = 0x08000000 if WIN else 0

if WIN:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)

_mutex = None


def set_dpi_aware():
    """Чтобы размеры мониторов были в настоящих пикселях."""
    if not WIN:
        return
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def precise_timers():
    """Точные таймеры в нашем процессе (иначе шаг таймера в Windows — около 15 мс)."""
    if WIN:
        try:
            ctypes.WinDLL("winmm").timeBeginPeriod(1)
        except Exception:
            pass


def single_instance():
    """False, если программа уже запущена."""
    global _mutex
    if not WIN:
        return True
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    _mutex = kernel32.CreateMutexW(None, False, "Local\\%s_single_instance" % APP_NAME)
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def message_box(text, title=APP_NAME):
    if WIN:
        user32.MessageBoxW(None, text, title, 0x40)
    else:
        print(title + ": " + text)


def monitors():
    """Список мониторов: [{x, y, w, h, primary}]."""
    if not WIN:
        return [{"x": 0, "y": 0, "w": 1920, "h": 1080, "primary": True}]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    result = []
    proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                                   ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def callback(handle, hdc, rect, lparam):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(wintypes.HANDLE(handle), ctypes.byref(info)):
            r = info.rcMonitor
            result.append({"x": r.left, "y": r.top, "w": r.right - r.left,
                           "h": r.bottom - r.top, "primary": bool(info.dwFlags & 1)})
        return True

    try:
        user32.EnumDisplayMonitors(None, None, proc_type(callback), 0)
    except Exception:
        pass
    return result


def list_windows():
    """Обычные окна программ: [{hwnd, title, exe, area}] — для выбора окна записи."""
    if not WIN:
        return []
    result = []
    own = os.getpid()
    proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    user32.GetWindow.restype = wintypes.HWND

    def callback(hwnd, lparam):
        try:
            if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):   # 4 = GW_OWNER: диалоги пропускаем
                return True
            if user32.GetWindowLongW(hwnd, -20) & 0x00000080:                   # WS_EX_TOOLWINDOW
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            cloaked = wintypes.DWORD()
            try:   # скрытые окна магазинных приложений
                ctypes.WinDLL("dwmapi").DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)
            except Exception:
                pass
            if cloaked.value:
                return True
            title = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title, length + 1)
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == own:
                return True
            exe = ""
            handle = kernel32.OpenProcess(0x1000, False, pid.value)
            if handle:
                size = wintypes.DWORD(1024)
                buf = ctypes.create_unicode_buffer(size.value)
                if kernel32.QueryFullProcessImageNameW(wintypes.HANDLE(handle), 0, buf, ctypes.byref(size)):
                    exe = os.path.basename(buf.value).lower()
                kernel32.CloseHandle(wintypes.HANDLE(handle))
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
            if exe and area > 0:
                result.append({"hwnd": int(hwnd), "title": title.value, "exe": exe, "area": area,
                               "minimized": bool(user32.IsIconic(hwnd))})
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(proc_type(callback), 0)
    except Exception:
        pass
    return result


def find_window(exe):
    """Главное окно программы (самое большое, не свёрнутое) или None."""
    exe = (exe or "").lower()
    if not exe:
        return None
    found = [w for w in list_windows() if w["exe"] == exe and not w["minimized"]]
    return max(found, key=lambda w: w["area"])["hwnd"] if found else None


def foreground_app():
    """Имя программы в активном окне (для названия клипа), например «P3R»."""
    if not WIN:
        return ""
    try:
        user32.GetForegroundWindow.restype = wintypes.HWND
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid():
            return ""
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(wintypes.HANDLE(handle), 0, buf, ctypes.byref(size)):
                return ""
            name = os.path.splitext(os.path.basename(buf.value))[0]
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(handle))
        if name.lower() in ("explorer", "searchhost", "shellexperiencehost", "startmenuexperiencehost",
                            "applicationframehost", "lockapp", "dwm"):
            return "Рабочий стол"
        return name
    except Exception:
        return ""


def recycle(path):
    """Удалить файл в корзину (если не вышло — насовсем)."""
    if WIN:
        try:
            class SHFILEOPSTRUCTW(ctypes.Structure):
                _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                            ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                            ("fFlags", wintypes.WORD), ("fAnyOperationsAborted", wintypes.BOOL),
                            ("hNameMappings", wintypes.LPVOID), ("lpszProgressTitle", wintypes.LPCWSTR)]

            buf = ctypes.create_unicode_buffer(os.path.abspath(path) + "\0\0")
            op = SHFILEOPSTRUCTW()
            op.wFunc = 3  # FO_DELETE
            op.pFrom = ctypes.cast(buf, wintypes.LPCWSTR)
            op.fFlags = 0x0040 | 0x0010 | 0x0004 | 0x0400  # ALLOWUNDO | NOCONFIRMATION | SILENT | NOERRORUI
            if shell32.SHFileOperationW(ctypes.byref(op)) == 0 and not os.path.exists(path):
                return True
        except Exception:
            pass
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def open_path(path):
    if WIN:
        os.startfile(path)  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", path])


def reveal(path):
    if WIN:
        subprocess.Popen('explorer /select,"%s"' % os.path.normpath(path))
    else:
        open_path(os.path.dirname(path))


def copy_file_to_clipboard(path):
    """Положить файл в буфер обмена — чтобы вставить клип в Discord через Ctrl+V."""
    if not WIN:
        return False
    quoted = path.replace("'", "''")
    try:
        return subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Set-Clipboard -LiteralPath '%s'" % quoted],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=20, creationflags=NO_WINDOW).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def play_sound(path):
    if not WIN or not os.path.isfile(path):
        return
    try:
        import winsound
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception:
        pass


def set_autostart(enabled):
    """Запуск вместе с Windows (свёрнутым в трей)."""
    if not WIN:
        return False
    import winreg
    if getattr(sys, "frozen", False):
        command = '"%s" --minimized' % sys.executable
    else:
        exe = sys.executable.replace("python.exe", "pythonw.exe")
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
        command = '"%s" "%s" --minimized' % (exe, script)
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
        with key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False
