"""Опыт: есть ли на сборочном сервере звук после установки виртуальной звуковой карты."""
import subprocess, sys, time, wave, struct, math, os, tempfile

print("Windows:", sys.getwindowsversion(), flush=True)
print(subprocess.run(["powershell", "-NoProfile", "-Command",
      "Get-Service Audiosrv,AudioEndpointBuilder | Format-Table -AutoSize Name,Status,StartType | Out-String; "
      "Get-PnpDevice -Class AudioEndpoint -ErrorAction SilentlyContinue | Format-Table -AutoSize Status,FriendlyName | Out-String"],
      capture_output=True, text=True, errors="replace").stdout, flush=True)
try:
    import pyaudiowpatch as pa
    p = pa.PyAudio()
    api = p.get_host_api_info_by_type(pa.paWASAPI)
    print("WASAPI: устройств", api["deviceCount"], "вывод по умолчанию", api["defaultOutputDevice"], "ввод по умолчанию", api["defaultInputDevice"], flush=True)
    for i in range(p.get_device_count()):
        d = p.get_device_info_by_index(i)
        if d["hostApi"] == api["index"]:
            print("  ", i, d["name"], "in", d["maxInputChannels"], "out", d["maxOutputChannels"], d["defaultSampleRate"], "loopback" if d.get("isLoopbackDevice") else "", flush=True)
    lb = p.get_default_wasapi_loopback()
    print("loopback по умолчанию:", lb["name"], lb["maxInputChannels"], lb["defaultSampleRate"], flush=True)
    wav = os.path.join(tempfile.gettempdir(), "tone.wav")
    with wave.open(wav, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(48000)
        w.writeframes(b"".join(struct.pack("<hh", *(int(12000 * math.sin(2 * math.pi * 440 * i / 48000)),) * 2) for i in range(48000 * 3)))
    import winsound
    got = []
    def cb(data, n, t, s):
        got.append(data); return (None, pa.paContinue)
    st = p.open(format=pa.paInt16, channels=lb["maxInputChannels"], rate=int(lb["defaultSampleRate"]), input=True,
                input_device_index=lb["index"], frames_per_buffer=480, stream_callback=cb)
    time.sleep(0.5)
    winsound.PlaySound(wav, winsound.SND_FILENAME | winsound.SND_ASYNC)
    time.sleep(3.5)
    st.stop_stream(); st.close()
    raw = b"".join(got)
    peak = max(abs(x) for x in struct.unpack("<%dh" % (len(raw) // 2), raw)) if raw else 0
    print("захвачено %.1f c звука, пик %d (ожидается около 12000)" % (len(raw) / 2 / lb["maxInputChannels"] / lb["defaultSampleRate"], peak), flush=True)
except Exception as exc:
    print("звук недоступен:", repr(exc), flush=True)
