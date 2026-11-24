"""Связь со Steal Your Voice по именованному каналу (запускается при сборке на Windows):
поддельный сервер отвечает как панель; клиент получает состояние и шлёт команды, а без панели не висит."""
import ctypes
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from midnightclips import syv  # noqa: E402

got = []


def server(count):
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateNamedPipeW.restype = ctypes.c_void_p
    for _ in range(count):
        h = k32.CreateNamedPipeW(syv.PIPE, 3, 0, 4, 65536, 65536, 0, None)   # PIPE_ACCESS_DUPLEX, byte mode
        assert h and h != ctypes.c_void_p(-1).value, ctypes.get_last_error()
        k32.ConnectNamedPipe(ctypes.c_void_p(h), None)
        buf = ctypes.create_string_buffer(65536)
        n = ctypes.c_ulong()
        k32.ReadFile(ctypes.c_void_p(h), buf, 65536, ctypes.byref(n), None)
        request = json.loads(buf.raw[:n.value].decode("utf-8").strip())
        got.append(request)
        reply = {"ok": True, "connected": True, "muted": request.get("cmd") == "mute", "fx": False, "fxVoice": "ДЕМОН",
                 "presets": ["ЧИСТЫЙ", "СТРИМ", "Гирсешен"], "active": request.get("name", "")}
        data = (json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8")
        k32.WriteFile(ctypes.c_void_p(h), data, len(data), ctypes.byref(n), None)
        k32.FlushFileBuffers(ctypes.c_void_p(h))
        k32.DisconnectNamedPipe(ctypes.c_void_p(h))
        k32.CloseHandle(ctypes.c_void_p(h))


if __name__ == "__main__":
    if sys.platform != "win32":
        print("только для Windows — пропуск")
        sys.exit(0)
    started = time.time()
    assert syv.call("state") is None and time.time() - started < 2, "без панели вызов должен сразу вернуть None"
    worker = threading.Thread(target=server, args=(3,), daemon=True)
    worker.start()
    time.sleep(0.3)
    link = syv.Link()
    link.refresh(wait=True)
    assert link.state and link.state["presets"] == ["ЧИСТЫЙ", "СТРИМ", "Гирсешен"], link.state
    reply = link.send("preset", name="Гирсешен", temp=True)
    assert reply and reply["active"] == "Гирсешен", reply
    reply = link.send("mute")
    assert reply and reply["muted"] is True, reply
    worker.join(5)
    assert [g["cmd"] for g in got] == ["state", "preset", "mute"] and got[1]["temp"] is True, got
    print("запросы:", got)
    print("STEAL YOUR VOICE: OK")
