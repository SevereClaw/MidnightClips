"""Связь со Steal Your Voice (панель Voicemeeter): мьют микрофона, эффект голоса и пресеты из меню поверх игры.

Steal Your Voice слушает канал \\\\.\\pipe\\StealYourVoice.Link: одна строка JSON — запрос, одна — ответ.
Если панель не запущена, все вызовы просто возвращают None.
"""
import json
import sys
import threading
import time

PIPE = r"\\.\pipe\StealYourVoice.Link"
WIN = sys.platform == "win32"


def call(cmd, timeout=1.5, **args):
    """Отправить команду и получить ответ (dict) или None — панели нет или она не ответила вовремя."""
    if not WIN:
        return None
    result = {}

    def work():
        try:
            with open(PIPE, "r+b", buffering=0) as pipe:
                pipe.write((json.dumps(dict(args, cmd=cmd), ensure_ascii=False) + "\n").encode("utf-8"))
                data = b""
                while not data.endswith(b"\n") and len(data) < 256 * 1024:
                    chunk = pipe.read(65536)
                    if not chunk:
                        break
                    data += chunk
            result["reply"] = json.loads(data.decode("utf-8") or "null")
        except (OSError, ValueError):
            pass

    worker = threading.Thread(target=work, daemon=True, name="syv")
    worker.start()
    worker.join(timeout)   # окно панели занято — не ждать его, меню не должно подвисать
    reply = result.get("reply")
    return reply if isinstance(reply, dict) else None


class Link:
    """Последнее известное состояние панели: меню читает его мгновенно, обновляется оно в фоне."""

    def __init__(self):
        self.state = None
        self.checked = 0.0
        self._busy = threading.Lock()

    def refresh(self, wait=False):
        def work():
            if not self._busy.acquire(False):
                return
            try:
                reply = call("state")
                self.state = reply if reply and reply.get("ok") else None
                self.checked = time.time()
            finally:
                self._busy.release()
        if wait:
            work()
        else:
            threading.Thread(target=work, daemon=True).start()

    def send(self, cmd, **args):
        reply = call(cmd, timeout=3.0, **args)
        if reply and reply.get("ok"):
            self.state = reply
            self.checked = time.time()
        return reply
