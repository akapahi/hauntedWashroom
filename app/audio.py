"""Local mic/speaker I/O for the ElevenLabs conversation (PyAudio, 16 kHz mono PCM).

Differs from the SDK's DefaultAudioInterface in that it:
- can mute the mic (sends silence) e.g. while the entity says goodbye,
- optionally runs half-duplex, muting the mic while the entity is speaking so the speaker
  doesn't echo back into the mic and get transcribed as the visitor,
- tracks playback so the app knows when the entity has finished talking,
- adds reverb to the entity's voice (see reverb.py), letting the tail ring out after it stops,
- lets you pick devices via AUDIO_INPUT_DEVICE / AUDIO_OUTPUT_DEVICE (see list_devices.py).
"""

import os
import queue
import threading
import time
import wave
from pathlib import Path
from typing import Callable

from elevenlabs.conversational_ai.conversation import AudioInterface

from .reverb import Reverb

SAMPLE_RATE = 16000
INPUT_FRAMES_PER_BUFFER = 4000   # 250 ms
OUTPUT_FRAMES_PER_BUFFER = 1000  # 62.5 ms
ECHO_TAIL_SECONDS = 0.4          # keep the mic muted briefly after playback (room reverb)


def _device_index(env_name: str) -> int | None:
    value = os.getenv(env_name, "").strip()
    return int(value) if value else None


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as wf:
        return wf.getnframes() / wf.getframerate()


def play_wav(path: Path, stop: threading.Event | None = None) -> float:
    """Blocking: play a WAV file on the entity's output device (AUDIO_OUTPUT_DEVICE), at the file's own
    rate and channel count. Returns the seconds actually played; stops early if `stop` is set."""
    import pyaudio

    with wave.open(str(path), "rb") as wf:
        p = pyaudio.PyAudio()
        try:
            stream = p.open(
                format=p.get_format_from_width(wf.getsampwidth()),
                channels=wf.getnchannels(),
                rate=wf.getframerate(),
                output=True,
                output_device_index=_device_index("AUDIO_OUTPUT_DEVICE"),
            )
            try:
                frame_bytes = wf.getnchannels() * wf.getsampwidth()
                played = 0
                while not (stop and stop.is_set()):
                    data = wf.readframes(OUTPUT_FRAMES_PER_BUFFER)
                    if not data:
                        break
                    stream.write(data)
                    played += len(data) // frame_bytes
                return played / wf.getframerate()
            finally:
                stream.stop_stream()
                stream.close()
        finally:
            p.terminate()


class EntityAudioInterface(AudioInterface):
    def __init__(self, half_duplex: bool = True, reverb_mix: float = 0.0, reverb_decay: float = 1.5):
        import pyaudio

        self.pyaudio = pyaudio
        self.half_duplex = half_duplex
        self.reverb = Reverb(reverb_mix, reverb_decay, SAMPLE_RATE) if reverb_mix > 0 else None
        self.muted = False
        self.chunks_received = 0
        self.last_play_end = time.monotonic()
        self._writing = False
        self._started = False
        self._stopped = False
        self.input_callback: Callable[[bytes], None] | None = None

    def start(self, input_callback: Callable[[bytes], None]):
        self.input_callback = input_callback
        self.output_queue: queue.Queue[bytes] = queue.Queue()
        self.should_stop = threading.Event()
        self.p = self.pyaudio.PyAudio()
        self.in_stream = self.p.open(
            format=self.pyaudio.paInt16,
            channels=1,
            rate=SAMPLE_RATE,
            input=True,
            input_device_index=_device_index("AUDIO_INPUT_DEVICE"),
            stream_callback=self._in_callback,
            frames_per_buffer=INPUT_FRAMES_PER_BUFFER,
            start=True,
        )
        self.out_stream = self.p.open(
            format=self.pyaudio.paInt16,
            channels=1,
            rate=SAMPLE_RATE,
            output=True,
            output_device_index=_device_index("AUDIO_OUTPUT_DEVICE"),
            frames_per_buffer=OUTPUT_FRAMES_PER_BUFFER,
            start=True,
        )
        self.last_play_end = time.monotonic()
        self.output_thread = threading.Thread(target=self._output_loop, daemon=True, name="entity-audio-out")
        self.output_thread.start()
        self._started = True

    def stop(self):
        # The SDK may call stop() more than once (its own error path + ours).
        if not self._started or self._stopped:
            return
        self._stopped = True
        self.should_stop.set()
        self.output_thread.join(timeout=2)
        for stream in (self.in_stream, self.out_stream):
            try:
                stream.stop_stream()
                stream.close()
            except OSError:
                pass
        self.p.terminate()

    def output(self, audio: bytes):
        self.chunks_received += 1
        self.output_queue.put(audio)

    def interrupt(self):
        try:
            while True:
                self.output_queue.get(block=False)
        except queue.Empty:
            pass
        if self.reverb:
            self.reverb.reset()

    @property
    def is_playing(self) -> bool:
        return self._writing or not self.output_queue.empty()

    def idle_for(self) -> float:
        """Seconds since the speaker last played anything (0 while playing)."""
        if self.is_playing:
            return 0.0
        return time.monotonic() - self.last_play_end

    def _output_loop(self):
        ringing = False  # reverb tail still playing: don't block waiting for audio, or the tail stutters
        while not self.should_stop.is_set():
            try:
                audio = self.output_queue.get(block=not ringing, timeout=0.05)
                if self.reverb:
                    audio = self.reverb.process(audio)
            except queue.Empty:
                audio = self.reverb.flush(OUTPUT_FRAMES_PER_BUFFER) if self.reverb else None
                ringing = audio is not None
                if not ringing:
                    continue
            self._writing = True
            try:
                self.out_stream.write(audio)
            finally:
                self._writing = False
                self.last_play_end = time.monotonic()

    def _in_callback(self, in_data, frame_count, time_info, status):
        if self.input_callback:
            if self.muted or (self.half_duplex and self.idle_for() < ECHO_TAIL_SECONDS):
                in_data = bytes(len(in_data))  # silence keeps the stream (and server VAD) alive
            self.input_callback(in_data)
        return (None, self.pyaudio.paContinue)
