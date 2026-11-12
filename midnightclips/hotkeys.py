"""Глобальные горячие клавиши через RegisterHotKey (работают и поверх игр)."""
import ctypes
import queue
import sys
import threading

WIN = sys.platform == "win32"
MODS = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "win": 0x8}
WM_HOTKEY, WM_APP, WM_QUIT = 0x0312, 0x8000, 0x0012

VK = {
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "backspace": 0x08, "insert": 0x2D, "delete": 0x2E,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28,
    "left": 0x25, "right": 0x27, "pause": 0x13, "scrolllock": 0x91, "printscreen": 0x2C,
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC, ";": 0xBA, "'": 0xDE,
    ",": 0xBC, ".": 0xBE, "/": 0xBF,
}
VK.update({"f%d" % i: 0x6F + i for i in range(1, 25)})
VK.update({chr(c): c - 32 for c in range(ord("a"), ord("z") + 1)})
VK.update({str(d): 0x30 + d for d in range(10)})
VK.update({"num%d" % d: 0x60 + d for d in range(10)})


def parse(combo):
    """'Alt+F10' -> (модификаторы, код клавиши) или None."""
    mods, key = 0, None
    for part in [p.strip().lower() for p in combo.split("+") if p.strip()]:
        if part in MODS:
            mods |= MODS[part]
        elif part in VK and key is None:
            key = VK[part]
        else:
            return None
    return (mods, key) if key is not None else None


class Hotkeys(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="hotkeys")
        self._pending = queue.Queue()
        self._ready = threading.Event()
        self._tid = 0
        self._ids = {}         # имя -> id
        self._callbacks = {}   # id -> функция
        self.status = {}       # имя -> True/False (удалось ли занять сочетание)

    def bind(self, name, combo, callback):
        """Назначить (или переназначить) сочетание. Пустая строка — снять."""
        self._pending.put((name, combo, callback))
        if WIN and self._ready.wait(3):
            ctypes.windll.user32.PostThreadMessageW(self._tid, WM_APP, 0, 0)

    def shutdown(self):
        if WIN and self._ready.is_set():
            ctypes.windll.user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)

    def _apply(self):
        user32 = ctypes.windll.user32
        while True:
            try:
                name, combo, callback = self._pending.get_nowait()
            except queue.Empty:
                return
            ident = self._ids.setdefault(name, len(self._ids) + 1)
            user32.UnregisterHotKey(None, ident)
            self._callbacks.pop(ident, None)
            if not combo:
                self.status[name] = True
                continue
            parsed = parse(combo)
            ok = bool(parsed and user32.RegisterHotKey(None, ident, parsed[0] | 0x4000, parsed[1]))
            self.status[name] = ok
            if ok:
                self._callbacks[ident] = callback

    def run(self):
        if not WIN:
            return
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # создать очередь сообщений
        self._tid = ctypes.windll.kernel32.GetCurrentThreadId()
        self._ready.set()
        self._apply()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                callback = self._callbacks.get(msg.wParam)
                if callback:
                    threading.Thread(target=callback, daemon=True).start()
            elif msg.message == WM_APP:
                self._apply()
        for ident in self._ids.values():
            user32.UnregisterHotKey(None, ident)
