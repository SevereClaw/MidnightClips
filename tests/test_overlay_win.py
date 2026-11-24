"""Проверка окон поверх экрана на настоящей Windows (запускается при сборке):
уведомление, точка записи и «метроном» точного захвата не должны падать."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from midnightclips import winutil  # noqa: E402
from midnightclips.toast import Toast  # noqa: E402

if __name__ == "__main__":
    if sys.platform != "win32":
        print("только для Windows — пропуск")
        sys.exit(0)
    winutil.set_dpi_aware()
    winutil.precise_timers()
    rect = (winutil.monitors() or [{"x": 0, "y": 0, "w": 1024, "h": 768}])[0]
    print("монитор:", rect)
    os.environ["MC_MENU_IN_CAPTURE"] = "1"
    toast = Toast()
    toast.start()
    assert toast.ready.wait(15), "поток окон не запустился: %s" % toast.errors
    toast.show("Клип сохранён", "Проверка.mp4")
    for corner in ("tl", "tr", "bl", "br"):
        toast.set_indicator(True, rect, corner)
        time.sleep(0.4)
    toast.set_tickle(True, rect, 8)
    time.sleep(0.5)
    start = toast.ticks
    time.sleep(2.0)
    rate = (toast.ticks - start) / 2.0
    print("метроном: %.0f тиков в секунду при шаге 8 мс" % rate)
    toast.set_tickle(False)
    toast.set_indicator(False)
    time.sleep(0.6)
    stopped = toast.ticks
    time.sleep(0.5)
    # меню поверх игры (Alt+Z): открывается, плитки нажимаются, закрывается
    pressed = []
    flags = {"rec": False, "discord": True}

    def model():
        return {"status": "Идёт запись  00:12" if flags["rec"] else "Клипы включены — в буфере последние 60 с",
                "recording": flags["rec"], "hint": "Цифры 1–9, стрелки с Enter или мышь — выбрать.   Alt+Z или Esc — закрыть",
                "last": "Проверка 2026-11-16  ·  3 мин назад  ·  48 МБ",
                "chips": [{"text": "game.exe", "accent": True}, {"text": "NVIDIA NVENC"}, {"text": "60 кадров/с"},
                          {"text": "Свободно 212 ГБ"}],
                "tiles": [{"id": "clip", "title": "Сохранить клип", "sub": "Последние 60 с", "active": True, "key": "Alt+F10", "icon": "clip"},
                          {"id": "clip_a", "title": "Клип: 30 с", "sub": "Последние 30 с", "active": True, "key": "Alt+F8", "icon": "short"},
                          {"id": "clip_b", "title": "Клип: 15 с", "sub": "Последние 15 с", "active": True, "key": "Alt+F7", "icon": "short"},
                          {"id": "record", "title": "Остановить запись" if flags["rec"] else "Начать запись", "sub": "Alt+F9",
                           "active": True, "alert": flags["rec"], "key": "Alt+F9", "icon": "stop" if flags["rec"] else "rec"},
                          {"id": "buffer", "title": "Клипы", "sub": "Буфер повтора работает, клавиши клипов заняты", "active": True, "switch": True},
                          {"id": "mic", "title": "Микрофон", "sub": "Не записывается", "active": False, "switch": False},
                          {"id": "copy", "title": "Скопировать", "sub": "Последний клип — и Ctrl+V в Discord", "active": True, "icon": "copy"},
                          {"id": "clips", "title": "Галерея", "sub": "Сегодня клипов: 4", "active": True, "icon": "grid"},
                          {"id": "settings", "title": "Настройки", "sub": "Открыть настройки программы", "active": True, "icon": "gear"}],
                "rows": [{"label": "ЗВУК В ЗАПИСИ", "items": [
                            {"id": "snd:@game", "name": "Игра", "on": True},
                            {"id": "snd:discord.exe", "name": "Discord", "on": flags["discord"]},
                            {"id": "snd:mp3player.exe", "name": "Mp3player", "on": False}]},
                         {"label": "STEAL YOUR VOICE", "items": [
                            {"id": "syv:mute", "name": "Микрофон везде", "on": True},
                            {"id": "syv:fx", "name": "Голос: Демон", "on": False},
                            {"id": "syv:preset:ЧИСТЫЙ", "name": "Чистый", "active": True},
                            {"id": "syv:preset:СТРИМ", "name": "Стрим", "active": False},
                            {"id": "syv:preset:Гирсешен", "name": "Гирсешен", "active": False}]}]}

    def action(ident):
        pressed.append(ident)
        if ident == "record":
            flags["rec"] = not flags["rec"]
        if ident == "snd:discord.exe":
            flags["discord"] = not flags["discord"]
        return ident == "clip"

    toast.toggle_menu(rect, model, action)
    time.sleep(1.0)
    assert toast.menu_open, "меню не открылось: %s" % toast.errors
    import subprocess
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    subprocess.run([os.path.join(root_dir, "ffmpeg.exe"), "-hide_banner", "-loglevel", "error", "-y", "-f", "gdigrab",
                    "-i", "desktop", "-frames:v", "1", os.path.join(root_dir, "overlay_menu.png")])   # снимок — посмотреть глазами
    import base64
    small = os.path.join(root_dir, "overlay_menu.jpg")
    subprocess.run([os.path.join(root_dir, "ffmpeg.exe"), "-hide_banner", "-loglevel", "error", "-y", "-i",
                    os.path.join(root_dir, "overlay_menu.png"), "-q:v", "6", small])
    if os.path.isfile(small):
        data = base64.b64encode(open(small, "rb").read()).decode()
        for i in range(0, len(data), 3000):
            print("SHOT:" + data[i:i + 3000], flush=True)
    toast.menu_press("snd:discord.exe")
    time.sleep(0.6)
    assert toast.menu_open and not flags["discord"], "выключатель звука программы должен сработать, меню — остаться"
    toast.menu_press("record")
    time.sleep(1.0)
    assert toast.menu_open and flags["rec"], "после «записи» меню должно остаться открытым"
    toast.menu_press("clip")
    time.sleep(1.0)
    assert not toast.menu_open, "после «сохранить клип» меню должно закрыться"
    toast.toggle_menu(rect, model, action)
    time.sleep(0.7)
    toast.toggle_menu(rect, model, action)   # повторное нажатие Alt+Z закрывает
    time.sleep(0.7)
    assert not toast.menu_open and pressed == ["snd:discord.exe", "record", "clip"], (toast.menu_open, pressed)
    print("меню поверх игры: открытие, нажатия и закрытие — ok", flush=True)

    # анимация: меню появляется и исчезает плавно — много кадров, а не десяток
    del os.environ["MC_MENU_IN_CAPTURE"]
    toast.toggle_menu(rect, model, action)
    time.sleep(1.0)
    assert toast.menu_open, toast.errors
    opened = toast.menu_frames[0]
    toast.toggle_menu(rect, model, action)
    time.sleep(0.05)
    assert not toast.menu_open and toast.menu_alive, "меню должно закрываться с анимацией, а не пропадать сразу"
    time.sleep(0.6)
    assert not toast.menu_alive, "меню не закрылось"
    closed = toast.menu_frames[1]
    print("анимация меню: %d кадров за появление (%.0f в секунду), %d за исчезновение (%.0f в секунду)" % (
        opened, opened / 0.30, closed, closed / 0.17), flush=True)
    assert opened >= 12 and closed >= 6, "анимация меню идёт рывками: %s" % toast.menu_frames
    os.environ["MC_MENU_IN_CAPTURE"] = "1"
    print("ошибки:", toast.errors)
    assert not toast.errors, toast.errors
    assert rate >= 55, "метроном слишком редкий: %.0f/с" % rate
    assert toast.ticks == stopped, "метроном не остановился"
    assert toast.is_alive()
    print("ОКНА ПОВЕРХ ЭКРАНА: OK")
