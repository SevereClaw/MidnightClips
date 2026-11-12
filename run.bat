@echo off
chcp 65001 >nul
rem Запуск из исходников (нужен Python 3.10-3.12 и ffmpeg.exe в этой папке или в PATH)
cd /d "%~dp0"
python -m pip install -r requirements.txt --quiet
python main.py
