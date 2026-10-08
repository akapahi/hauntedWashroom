"""List audio devices so you can set AUDIO_INPUT_DEVICE / AUDIO_OUTPUT_DEVICE in .env."""

import pyaudio

p = pyaudio.PyAudio()
default_in = p.get_default_input_device_info()["index"] if p.get_device_count() else None
default_out = p.get_default_output_device_info()["index"] if p.get_device_count() else None
for i in range(p.get_device_count()):
    d = p.get_device_info_by_index(i)
    io = []
    if d["maxInputChannels"] > 0:
        io.append("IN" + (" (default)" if i == default_in else ""))
    if d["maxOutputChannels"] > 0:
        io.append("OUT" + (" (default)" if i == default_out else ""))
    print(f"[{i:2}] {d['name']:<50} {', '.join(io):<28} {int(d['defaultSampleRate'])} Hz")
p.terminate()
