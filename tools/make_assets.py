"""Рисует значок и генерирует звуки. Запуск: python tools/make_assets.py"""
import math
import os
import struct
import wave

from PIL import Image, ImageDraw

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
os.makedirs(ROOT, exist_ok=True)


def icon():
    s = 1024
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((40, 40, s - 40, s - 40), 190, fill="#000000")
    # антенна
    d.line((512, 300, 360, 150), fill="#FFFFFF", width=34)
    d.line((512, 300, 664, 150), fill="#FFFFFF", width=34)
    # корпус и экран
    d.rounded_rectangle((150, 290, s - 150, s - 170), 110, fill="#FFE100")
    d.rounded_rectangle((215, 355, s - 215, s - 235), 80, fill="#000000")
    bars = ["#F4F4F4", "#FFE100", "#20D6E8", "#2FCB4B", "#E63BC8", "#E4002B", "#2743D8"]
    x0, x1, y0, y1 = 245, s - 245, 385, s - 265
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((x0, y0, x1, y1), 56, fill=255)
    stripes = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    sd = ImageDraw.Draw(stripes)
    w = (x1 - x0) / len(bars)
    for i, color in enumerate(bars):
        sd.rectangle((x0 + i * w, y0, x0 + (i + 1) * w + 1, y1), fill=color)
    img.paste(stripes, (0, 0), mask)
    d.ellipse((s - 330, s - 400, s - 190, s - 260), fill="#E4002B", outline="#000000", width=26)
    img.resize((256, 256), Image.LANCZOS).save(os.path.join(ROOT, "icon.png"))
    img.resize((256, 256), Image.LANCZOS).save(
        os.path.join(ROOT, "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def tone(name, notes, volume=0.32):
    """notes: [(частота Гц, длительность с)]"""
    rate = 44100
    frames = bytearray()
    for freq, dur in notes:
        n = int(rate * dur)
        for i in range(n):
            env = min(1.0, i / 300) * (1 - i / n) ** 1.6
            v = math.sin(2 * math.pi * freq * i / rate) + 0.3 * math.sin(4 * math.pi * freq * i / rate)
            frames += struct.pack("<h", int(32767 * volume * env * v / 1.3))
    with wave.open(os.path.join(ROOT, name + ".wav"), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(bytes(frames))


if __name__ == "__main__":
    icon()
    tone("saved", [(784, 0.09), (1047, 0.09), (1568, 0.22)])
    tone("rec_on", [(660, 0.10), (990, 0.18)])
    tone("rec_off", [(990, 0.10), (660, 0.18)])
    tone("error", [(220, 0.14), (175, 0.26)])
    print("готово:", sorted(os.listdir(ROOT)))
