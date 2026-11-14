@echo off
chcp 65001 >nul
rem Сборка MidnightClips.exe. Нужен Python 3.10-3.12. Результат: dist\MidnightClips\
cd /d "%~dp0"
python -m pip install -r requirements.txt pyinstaller || goto :fail

if not exist ffmpeg.exe (
  echo Скачиваю ffmpeg...
  powershell -NoProfile -Command "$ErrorActionPreference='Stop'; Invoke-WebRequest 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip' -OutFile ffmpeg.zip; Expand-Archive ffmpeg.zip -DestinationPath ffmpeg_tmp -Force; Copy-Item (Get-ChildItem ffmpeg_tmp -Recurse -Filter ffmpeg.exe | Select-Object -First 1).FullName ffmpeg.exe; Remove-Item ffmpeg.zip; Remove-Item ffmpeg_tmp -Recurse -Force" || goto :fail
)

rem Движок захвата (C++). Нужен компилятор MSVC; без него программа будет записывать через ffmpeg.
set ENGINE_ARG=
if not exist mccap.exe (
  where cl >nul 2>nul && (
    if not exist build mkdir build
    cl /nologo /O2 /MT /EHsc /std:c++20 /W3 /Fobuild\ /Femccap.exe engine\mccap.cpp || goto :fail
  )
)
if exist mccap.exe set ENGINE_ARG=--add-binary "mccap.exe;."

python -m PyInstaller --noconfirm --clean --windowed --name MidnightClips ^
  --icon assets\icon.ico ^
  --add-data "ui;ui" --add-data "assets;assets" --add-binary "ffmpeg.exe;." %ENGINE_ARG% ^
  --hidden-import pystray._win32 ^
  main.py || goto :fail

echo.
echo Готово: dist\MidnightClips\MidnightClips.exe
exit /b 0
:fail
echo Сборка не удалась.
exit /b 1
