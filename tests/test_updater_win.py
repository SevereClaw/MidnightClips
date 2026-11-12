"""Проверка установщика обновлений на настоящей Windows (запускается при сборке).

Имитирует обновление: «старая» папка с пробелами, скобками и кириллицей в пути,
«новая» версия во временной папке, процесс, которого нужно дождаться.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from midnightclips import updater  # noqa: E402


def run_case(exe_args):
    base = tempfile.mkdtemp(prefix="mc upd ")
    dst = os.path.join(base, "New folder (3) тест")
    tmp = tempfile.mkdtemp(prefix="MidnightClips_update_")
    src = os.path.join(tmp, "new", "MidnightClips")
    os.makedirs(os.path.join(dst, "_internal"))
    os.makedirs(os.path.join(src, "_internal"))
    log = os.path.join(base, "update.log")
    whoami = os.path.join(os.environ["SystemRoot"], "System32", "whoami.exe")
    for folder, mark in ((dst, "old"), (src, "new")):
        shutil.copy(whoami, os.path.join(folder, "MidnightClips.exe"))
        with open(os.path.join(folder, "_internal", "version.txt"), "w") as f:
            f.write(mark)
    waiter = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(4)"])
    updater.launch_script(updater.build_script(waiter.pid, src, dst, tmp, log, exe_args), tmp)
    deadline = time.time() + 90
    text = ""
    while time.time() < deadline:
        if os.path.isfile(log):
            text = open(log, encoding="utf-8-sig", errors="replace").read()
            if "done" in text:
                break
        time.sleep(1)
    print("--- args=%r\n%s" % (exe_args, text))
    assert "copied: True" in text, "файлы не скопированы"
    assert "started" in text and "start failed" not in text, "программа не запущена"
    assert open(os.path.join(dst, "_internal", "version.txt")).read() == "new", "осталась старая версия"
    assert not os.path.exists(tmp), "временная папка не убрана"
    shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    if sys.platform != "win32":
        print("только для Windows — пропуск")
        sys.exit(0)
    run_case("")
    run_case("--minimized")
    print("УСТАНОВЩИК ОБНОВЛЕНИЙ: OK")
