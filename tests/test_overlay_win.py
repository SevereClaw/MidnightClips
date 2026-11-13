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
    print("ошибки:", toast.errors)
    assert not toast.errors, toast.errors
    assert rate >= 55, "метроном слишком редкий: %.0f/с" % rate
    assert toast.ticks == stopped, "метроном не остановился"
    assert toast.is_alive()
    print("ОКНА ПОВЕРХ ЭКРАНА: OK")
