@echo off
chcp 65001 >nul
rem Сборка MidnightClips.exe. Нужен Python 3.10-3.12. Результат: dist\MidnightClips\
cd /d "%~dp0"
python -m pip install -r requirements.txt pyinstaller || goto :fail

rem FFmpeg: программа, её библиотеки и заголовки. С компилятором MSVC движок соберётся с кодировщиком.
python tools\ffmpeg_sdk.py --optional || goto :fail

rem Движок захвата (C++). Нужен компилятор MSVC; без него программа будет записывать через ffmpeg.
set ENGINE_ARG=
set ENGINE_FF=
if exist build\fflib\avcodec.lib set ENGINE_FF=/DMC_FFMPEG /Iffmpeg_sdk\include
if not exist mccap.exe (
  where cl >nul 2>nul && (
    if not exist build mkdir build
    cl /nologo /O2 /MT /EHsc /std:c++20 /W3 %ENGINE_FF% /Fobuild\ /Femccap.exe engine\mccap.cpp /link /LIBPATH:build\fflib || goto :fail
  )
)
if exist mccap.exe set ENGINE_ARG=--add-binary "mccap.exe;."

python -m PyInstaller --noconfirm --clean --windowed --name MidnightClips ^
  --icon assets\icon.ico ^
  --add-data "ui;ui" --add-data "assets;assets" --add-binary "ffmpeg.exe;." %ENGINE_ARG% ^
  --hidden-import pystray._win32 ^
  main.py || goto :fail

rem Библиотеки FFmpeg — рядом с ffmpeg.exe и движком.
for %%f in (av*.dll sw*.dll postproc*.dll) do copy /y "%%f" dist\MidnightClips\_internal\ >nul

echo.
echo Готово: dist\MidnightClips\MidnightClips.exe
exit /b 0
:fail
echo Сборка не удалась.
exit /b 1
