"""List audio devices so you can set AUDIO_INPUT_DEVICE / AUDIO_OUTPUT_DEVICE in .env."""

import sounddevice as sd

default_in, default_out = sd.default.device
for i, d in enumerate(sd.query_devices()):
    io = []
    if d["max_input_channels"] > 0:
        io.append("IN" + (" (default)" if i == default_in else ""))
    if d["max_output_channels"] > 0:
        io.append("OUT" + (" (default)" if i == default_out else ""))
    print(f"[{i:2}] {d['name']:<50} {', '.join(io):<28} {int(d['default_samplerate'])} Hz")
