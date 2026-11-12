"""Автообновление из релизов GitHub.

Проверяет последний релиз, скачивает MidnightClips-win64.zip, после выхода
программы копирует новые файлы поверх старых и запускает её снова.
Работает только в собранной версии (exe), из исходников — выключено.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile

from . import APP_NAME, __version__

REPO = "SevereClaw/MidnightClips"
ASSET = "MidnightClips-win64.zip"
API_URL = "https://api.github.com/repos/%s/releases/latest" % REPO
EXE = "MidnightClips.exe"

# Скрипт без кириллицы: пути приходят аргументами, так что кодировка не важна.
APPLY_PS1 = r"""param([int]$ProcId, [string]$Src, [string]$Dst, [string]$Tmp, [string]$ExeArgs)
try { Wait-Process -Id $ProcId -Timeout 40 -ErrorAction SilentlyContinue } catch {}
Start-Sleep -Milliseconds 700
$ok = $false
for ($i = 0; $i -lt 5 -and -not $ok; $i++) {
  try { Copy-Item -Path (Join-Path $Src '*') -Destination $Dst -Recurse -Force -ErrorAction Stop; $ok = $true }
  catch { Start-Sleep -Seconds 2 }
}
$exe = Join-Path $Dst 'MidnightClips.exe'
if ($ExeArgs) { Start-Process -FilePath $exe -ArgumentList $ExeArgs -WorkingDirectory $Dst }
else { Start-Process -FilePath $exe -WorkingDirectory $Dst }
Start-Sleep -Seconds 2
Remove-Item -LiteralPath $Tmp -Recurse -Force -ErrorAction SilentlyContinue
"""


def parse_version(text):
    """'v0.1.12' -> (0, 1, 12)"""
    return tuple(int(n) for n in re.findall(r"\d+", text or "")[:4])


class Updater:
    def __init__(self, data_dir, on_event):
        self.on_event = on_event            # on_event("update", {...})
        self.enabled = bool(getattr(sys, "frozen", False)) and sys.platform == "win32"
        self.install_dir = os.path.dirname(sys.executable) if self.enabled else ""
        self._marker = os.path.join(data_dir, "update_attempt.json")
        self._lock = threading.Lock()
        self.status = "idle"                # idle / checking / downloading / ready / current / error / disabled
        self.latest = ""                    # номер найденной версии
        self.message = ""
        self._src = None                    # распакованная новая версия
        self._tmp = None
        if not self.enabled:
            self.status = "disabled"

    def snapshot(self):
        return {"status": self.status, "latest": self.latest, "message": self.message,
                "current": __version__}

    def _set(self, status, message=""):
        self.status, self.message = status, message
        self.on_event("update", self.snapshot())

    # ------------------------------------------------------------ проверка

    def fetch_latest(self):
        request = urllib.request.Request(API_URL, headers={
            "User-Agent": APP_NAME + "/" + __version__, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(request, timeout=20) as response:
            release = json.loads(response.read().decode("utf-8"))
        url = ""
        for asset in release.get("assets", []):
            if asset.get("name") == ASSET:
                url = asset.get("browser_download_url", "")
        return release.get("tag_name", ""), url

    def check(self, download=True):
        """Проверить и (если есть новее) скачать. Возвращает True, когда обновление готово к установке."""
        if not self.enabled:
            return False
        if not self._lock.acquire(False):
            return self.status == "ready"
        try:
            if self.status == "ready":
                return True
            self._set("checking")
            try:
                tag, url = self.fetch_latest()
            except Exception as exc:
                self._set("error", "Не удалось проверить обновления: %s" % exc)
                return False
            if not url or parse_version(tag) <= parse_version(__version__):
                self._set("current")
                return False
            self.latest = ".".join(str(n) for n in parse_version(tag))
            if not download:
                self._set("idle")
                return False
            self._set("downloading")
            try:
                self._download(url)
            except Exception as exc:
                self._cleanup()
                self._set("error", "Не удалось скачать обновление: %s" % exc)
                return False
            self._set("ready")
            return True
        finally:
            self._lock.release()

    def _download(self, url):
        self._tmp = tempfile.mkdtemp(prefix="MidnightClips_update_")
        archive = os.path.join(self._tmp, ASSET)
        request = urllib.request.Request(url, headers={"User-Agent": APP_NAME + "/" + __version__})
        with urllib.request.urlopen(request, timeout=60) as response, open(archive, "wb") as out:
            shutil.copyfileobj(response, out, 1024 * 1024)
        unpacked = os.path.join(self._tmp, "new")
        with zipfile.ZipFile(archive) as z:
            z.extractall(unpacked)
        os.remove(archive)
        self._src = find_app_dir(unpacked)
        if not self._src:
            raise RuntimeError("в архиве нет " + EXE)

    def _cleanup(self):
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)
        self._tmp = self._src = None

    # ------------------------------------------------------------ установка

    def already_tried(self):
        """Эту версию уже пытались поставить, а запущена всё ещё старая (например, папка только для чтения)."""
        try:
            with open(self._marker, "r", encoding="utf-8") as f:
                return json.load(f).get("version") == self.latest
        except (OSError, ValueError):
            return False

    def apply(self, exe_args=""):
        """Запустить установщик. После этого программа должна сразу завершиться."""
        if self.status != "ready" or not self._src:
            return False
        try:
            with open(self._marker, "w", encoding="utf-8") as f:
                json.dump({"version": self.latest, "time": time.time()}, f)
            script = os.path.join(self._tmp, "apply.ps1")
            with open(script, "w", encoding="ascii") as f:
                f.write(APPLY_PS1)
            cmd = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                   "-WindowStyle", "Hidden", "-File", script,
                   "-ProcId", str(os.getpid()), "-Src", self._src, "-Dst", self.install_dir,
                   "-Tmp", self._tmp, "-ExeArgs", exe_args]
            subprocess.Popen(
                cmd, cwd=tempfile.gettempdir(), close_fds=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=0x00000008 | 0x00000200 | 0x08000000)  # отдельно от нас, без окна
            return True
        except Exception as exc:
            self._set("error", "Не удалось запустить установку: %s" % exc)
            return False


def find_app_dir(root):
    for folder, _dirs, files in os.walk(root):
        if EXE in files:
            return folder
    return None
