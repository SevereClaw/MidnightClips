"""Подготовка FFmpeg для сборки (запускается из build.bat и на сервере сборки).

Скачивает сборку FFmpeg с отдельными библиотеками (DLL), кладёт ffmpeg.exe и библиотеки
в корень проекта, а заголовки — в ffmpeg_sdk\\include. Если доступен компилятор MSVC,
делает библиотеки импорта (build\\fflib\\*.lib): с ними движок mccap кодирует видео сам.
"""
import glob
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl-shared.zip"
SDK = os.path.join(ROOT, "ffmpeg_sdk")
LIBS = os.path.join(ROOT, "build", "fflib")
NEEDED = ["avcodec", "avformat", "avutil"]


def fetch():
    if os.path.isdir(os.path.join(SDK, "include", "libavcodec")) and glob.glob(os.path.join(ROOT, "avcodec-*.dll")):
        return
    archive = os.path.join(ROOT, "ffmpeg.zip")
    print("Скачиваю FFmpeg...", flush=True)
    urllib.request.urlretrieve(URL, archive)
    tmp = os.path.join(ROOT, "ffmpeg_tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    with zipfile.ZipFile(archive) as z:
        z.extractall(tmp)
    top = [d for d in glob.glob(os.path.join(tmp, "*")) if os.path.isdir(os.path.join(d, "bin"))][0]
    for old in glob.glob(os.path.join(ROOT, "*.dll")):
        os.remove(old)
    for path in glob.glob(os.path.join(top, "bin", "*")):
        name = os.path.basename(path).lower()
        if name.endswith(".dll") or name in ("ffmpeg.exe", "ffplay.exe"):
            shutil.copy2(path, os.path.join(ROOT, os.path.basename(path)))
    shutil.rmtree(SDK, ignore_errors=True)
    os.makedirs(SDK)
    shutil.copytree(os.path.join(top, "include"), os.path.join(SDK, "include"))
    shutil.rmtree(tmp, ignore_errors=True)
    os.remove(archive)


def import_libs():
    """Библиотеки импорта для MSVC — из списка функций самих DLL."""
    if not shutil.which("dumpbin") or not shutil.which("lib"):
        print("Компилятор MSVC не найден — движок соберётся без кодировщика", flush=True)
        return False
    os.makedirs(LIBS, exist_ok=True)
    for name in NEEDED:
        dlls = glob.glob(os.path.join(ROOT, name + "-*.dll"))
        if not dlls:
            print("Нет библиотеки %s" % name, flush=True)
            return False
        dll = dlls[0]
        out = subprocess.run(["dumpbin", "/nologo", "/exports", dll], capture_output=True, text=True, errors="replace").stdout
        names = re.findall(r"^\s+\d+\s+[0-9A-Fa-f]+\s+[0-9A-Fa-f]{8}\s+(\S+)", out, re.M)
        if len(names) < 50:
            print("Не удалось прочитать список функций %s" % dll, flush=True)
            return False
        definition = os.path.join(LIBS, name + ".def")
        with open(definition, "w", encoding="ascii") as f:
            f.write('LIBRARY "%s"\nEXPORTS\n' % os.path.basename(dll))
            f.writelines("    %s\n" % n for n in names)
        done = subprocess.run(["lib", "/nologo", "/def:" + definition, "/out:" + os.path.join(LIBS, name + ".lib"),
                               "/machine:x64"], capture_output=True, text=True, errors="replace")
        if done.returncode != 0:
            print(done.stdout + done.stderr, flush=True)
            return False
        print("%s: %d функций" % (os.path.basename(dll), len(names)), flush=True)
    return True


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):   # в консоли Windows русские буквы могут не печататься
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    fetch()
    sys.exit(0 if import_libs() or "--optional" in sys.argv else 1)
