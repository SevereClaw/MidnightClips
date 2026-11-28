"""Дорожка «Голос без эффекта» (голос до Steal Your Voice): когда пишется и как выбирается (работает и на Linux)."""
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from midnightclips import tools  # noqa: E402
from midnightclips.config import Config  # noqa: E402
from midnightclips.recorder import Recorder  # noqa: E402

FF = shutil.which("ffmpeg") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ffmpeg.exe")


def main():
    tmp = tempfile.mkdtemp(prefix="mc_clean_")
    try:
        # ---- когда дорожка пишется
        cfg = Config(os.path.join(tmp, "s.json"))
        cfg.update({"mic": "Voicemeeter Out B2 (VB-Audio Voicemeeter VAIO)", "separate_tracks": True,
                    "syv_mic_device": "Микрофон (Fifine K669)", "audio_mode": "all"})
        rec = Recorder(cfg, None, os.path.join(tmp, "buf"), os.path.join(tmp, "log.txt"), lambda *_: None,
                       engine="mccap.exe")
        plan = rec.audio_plan()
        print("дорожки:", plan["names"])
        assert plan["names"][1:3] == ["Микрофон", tools.CLEAN_VOICE] and plan["clean_track"] == 2, plan
        assert plan["clean_mic"] == "Микрофон (Fifine K669)"
        for change, why in (({"clean_voice": False}, "галочка снята"),
                            ({"mic": "Микрофон (Fifine K669)"}, "микрофон записывается напрямую"),
                            ({"syv_mic_device": "Voicemeeter VAIO3 Input"}, "вход Voicemeeter — виртуальный"),
                            ({"syv_mic_device": ""}, "Steal Your Voice ещё не сообщил")):
            saved = {k: cfg[k] for k in change}
            cfg.update(change)
            assert not rec.audio_plan()["clean_track"], why
            cfg.update(saved)
        cfg.update({"separate_tracks": False})
        assert rec.audio_plan()["names"] == ["Всё вместе"]
        cfg.update({"separate_tracks": True})

        # ---- как выбирается: «всё» — это всё, кроме запасного голоса
        extra = tools.extra_tracks(["Всё вместе", "Микрофон", tools.CLEAN_VOICE, "Игра"])
        assert extra == {2}
        assert tools.audio_choice([1, 3], 4, extra=extra) == (["-map", "0:v", "-map", "0:a:0"], True)
        assert tools.audio_choice([2, 3], 4, extra=extra)[0][:2] == ["-filter_complex", "[0:a:2][0:a:3]amix=inputs=2:normalize=0[mcaudio]"]
        assert tools.audio_choice([1, 2, 3], 4, extra=extra)[1] is False   # оба голоса — сведение, не общая
        assert tools.audio_choice([1, 2, 3], 4) == (["-map", "0:v", "-map", "0:a:0"], True)   # без запаски — как раньше

        # ---- на настоящем файле: обрезка с «Голос без эффекта» вместо «Микрофона»
        clip = os.path.join(tmp, "Клип.mp4")
        subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30",
                        "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=48000",    # общая: голос с эффектом
                        "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=48000",    # «Микрофон» (с эффектом)
                        "-f", "lavfi", "-i", "sine=frequency=1200:sample_rate=48000",   # «Голос без эффекта»
                        "-t", "4", "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:a",
                        "-metadata:s:a:0", "handler_name=Всё вместе", "-metadata:s:a:1", "handler_name=Микрофон",
                        "-metadata:s:a:2", "handler_name=" + tools.CLEAN_VOICE,
                        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", clip], check=True)
        assert tools.audio_tracks(FF, clip) == ["Всё вместе", "Микрофон", tools.CLEAN_VOICE], tools.audio_tracks(FF, clip)
        events = []
        t = tools.ClipTools(cfg, FF, os.path.join(tmp, "work"), lambda k, p: events.append((k, p)))
        import time
        events.clear()
        t.start(clip, "trim", None, None, [1])   # только «Микрофон» — это клип как есть, сохранять нечего
        while t.busy:
            time.sleep(0.1)
        assert "ползунками" in [p for k, p in events if k == "job_done"][-1]["text"]
        for picked, want in (([2], 1200), ([1, 2], 0)):
            events.clear()
            t.start(clip, "trim", None, None, picked)
            while t.busy:
                time.sleep(0.1)
            done = [p for k, p in events if k == "job_done"][-1]
            assert done["ok"], done
            out = subprocess.run([FF, "-hide_banner", "-i", done["path"], "-map", "0:a:0", "-af",
                                  "astats=metadata=1:reset=0", "-f", "null", "-"], capture_output=True).stderr.decode()
            n = len(tools.audio_tracks(FF, done["path"]))
            freq = dominant(done["path"])
            print("выбрано %s → дорожек %d, тон %d Гц" % (picked, n, freq))
            assert n == 1 and (abs(freq - want) < 60 if want else freq > 400), (freq, out[-300:])   # оба голоса — сведены
        print("ГОЛОС БЕЗ ЭФФЕКТА: ВСЁ ПРОШЛО")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def dominant(path):
    """Частота тона в файле — по числу переходов через ноль."""
    raw = subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-i", path, "-map", "0:a:0", "-ac", "1", "-ar", "48000",
                          "-f", "s16le", "-"], capture_output=True).stdout
    import array
    a = array.array("h", raw)[48000:96000]
    cross = sum(1 for i in range(1, len(a)) if (a[i - 1] < 0) != (a[i] < 0))
    return cross / 2


if __name__ == "__main__":
    main()
