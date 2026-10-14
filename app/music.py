"""Idle music on this machine's speakers, with the tape wind-down that kills it when the entity is triggered.

The track (res/Infinite_Dancefloor.mp3, or any .wav/.mp3 given to MusicPlayer) is decoded once at startup
(ffmpeg for anything that isn't a WAV) and looped through a sounddevice output stream.

Tape wind-down: over `duration` seconds the music slowly detunes (a tape reel losing speed, with a slow wow and
a faster flutter on top), gets muffled and resonant (a lowpass closing with rising Q), and distorts (gain driven
into a soft clipper). Late in the ride it stutters, drops out and finally crawls to a stop. The feel of it comes
from the tape_* settings; its length from the timeline.
"""

import math
import random
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path

import numpy as np
from scipy.signal import lfilter

from .audio import _device_index
from .logbus import bus

RATE = 44100
BLOCK = 1024                 # frames per block (~23 ms)
MIN_RATE = 0.0625            # the slowest the tape goes before it stops
TAPE_PARAMS = (
    "tape_die_at", "tape_floor_speed", "tape_wow_hz", "tape_wow_depth", "tape_flutter_hz", "tape_flutter_depth",
    "tape_filter_hz", "tape_q_max", "tape_drive_max", "tape_dropout", "tape_stutter",
)


def _decode(path: Path) -> np.ndarray | None:
    """float32 array (frames, 2) at RATE, or None if it can't be decoded."""
    if path.suffix.lower() == ".wav":
        with wave.open(str(path), "rb") as wf:
            raw = np.frombuffer(wf.readframes(wf.getnframes()), dtype={1: np.int8, 2: np.int16, 4: np.int32}[wf.getsampwidth()])
            x = raw.astype(np.float32) / float(2 ** (8 * wf.getsampwidth() - 1))
            x = x.reshape(-1, wf.getnchannels())
            if wf.getnchannels() == 1:
                x = np.repeat(x, 2, axis=1)
            x = x[:, :2]
            if wf.getframerate() != RATE:
                n = int(len(x) * RATE / wf.getframerate())
                idx = np.linspace(0, len(x) - 1, n)
                x = np.stack([np.interp(idx, np.arange(len(x)), x[:, c]) for c in range(2)], axis=1).astype(np.float32)
            return x
    if not shutil.which("ffmpeg"):
        bus.emit("system", f"ffmpeg not found, can't decode {path.name}; install it (apt install ffmpeg) or use a .wav", "warn")
        return None
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "2", "-ar", str(RATE), "-"],
        capture_output=True, timeout=120,
    )
    if r.returncode != 0:
        bus.emit("error", f"ffmpeg couldn't decode {path.name}: {r.stderr.decode(errors='replace').strip()[:200]}", "error")
        return None
    return np.frombuffer(r.stdout, dtype=np.float32).reshape(-1, 2).copy()


def _lowpass(freq: float, q: float) -> tuple[np.ndarray, np.ndarray]:
    """RBJ cookbook lowpass biquad (b, a) for scipy.signal.lfilter."""
    w0 = 2 * math.pi * min(freq, RATE * 0.45) / RATE
    alpha = math.sin(w0) / (2 * q)
    cw = math.cos(w0)
    b = np.array([(1 - cw) / 2, 1 - cw, (1 - cw) / 2])
    a = np.array([1 + alpha, -2 * cw, 1 - alpha])
    return b / a[0], a / a[0]


def _smooth(x: float) -> float:
    return x * x * (3 - 2 * x)


def _clamp01(x: float) -> float:
    return min(1.0, max(0.0, x))


class MusicPlayer:
    def __init__(self, path: Path | None):
        self.path = path
        self.track: np.ndarray | None = None
        self.volume = 0.5
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ending = threading.Event()    # the loop has finished producing audio and is closing the stream
        self._pos = 0.0                 # read position in frames (fractional while winding down)
        self._wind: dict | None = None  # {t0, duration, params, zi} while the tape is dying
        self.on_change = None           # called with "playing" | "winding" | "stopped"

    def _changed(self, what: str):
        if self.on_change:
            try:
                self.on_change(what)
            except Exception:
                pass

    @property
    def loaded(self) -> bool:
        return self.track is not None

    @property
    def playing(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def winding(self) -> bool:
        return self.playing and self._wind is not None and not self._ending.is_set()

    def load(self):
        if not self.path or not self.path.is_file():
            bus.emit("system", "No idle music file (res/Infinite_Dancefloor.mp3)", "warn")
            return
        try:
            self.track = _decode(self.path)
        except Exception as e:
            bus.emit("error", f"Couldn't load {self.path.name}: {e}", "error")
            return
        if self.track is not None and len(self.track) < BLOCK * 2:
            self.track = None
        if self.track is not None:
            bus.emit("system", f"Idle music: {self.path.name} ({len(self.track) / RATE:.0f}s)")

    # ---- control ----------------------------------------------------------------

    def start(self):
        """Start the loop from the top (no-op if it's already playing)."""
        with self._lock:
            if self.track is None:
                return
            if self.playing:
                if not self._ending.is_set():
                    return
                self._thread.join(timeout=2)   # a wind-down just finished: let it close its stream first
            self._stop.clear()
            self._ending.clear()
            self._pos = 0.0
            self._wind = None
            self._thread = threading.Thread(target=self._loop, daemon=True, name="idle-music")
            self._thread.start()

    def stop(self):
        self._stop.set()
        t = self._thread
        if t and t is not threading.current_thread():
            t.join(timeout=2)

    def wind_down(self, duration_s: float, params: dict):
        """Start the tape wind-down; the music stops by itself when it's done. No-op if not playing."""
        with self._lock:
            if not self.playing or self._wind is not None:
                return
            self._wind = {
                "t0": time.monotonic(),
                "duration": max(0.1, duration_s),
                "params": {k: float(params[k]) for k in TAPE_PARAMS},
                "zi": np.zeros((2, 2), dtype=np.float64),   # biquad state per channel
            }
        self._changed("winding")

    # ---- the stream ---------------------------------------------------------------

    def _loop(self):
        try:
            import sounddevice as sd   # raises OSError if the PortAudio library is missing (apt install libportaudio2)

            stream = sd.RawOutputStream(
                samplerate=RATE, channels=2, dtype="float32",
                device=_device_index("AUDIO_OUTPUT_DEVICE"), blocksize=BLOCK,
            )
            stream.start()
        except Exception as e:
            bus.emit("error", f"Couldn't open the speaker for the idle music: {e}", "error")
            return
        self._changed("playing")
        try:
            while not self._stop.is_set():
                block = self._next_block()
                if block is None:
                    break
                stream.write(np.clip(block, -1.0, 1.0).astype(np.float32).tobytes())
        except Exception as e:
            bus.emit("error", f"Idle music stopped: {e}", "error")
        finally:
            self._ending.set()
            self._wind = None
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
            self._changed("stopped")

    def _next_block(self) -> np.ndarray | None:
        track = self.track
        n = len(track)
        w = self._wind
        if w is None:
            start = int(self._pos) % n
            idx = (start + np.arange(BLOCK)) % n
            self._pos = (start + BLOCK) % n
            return track[idx] * self.volume

        T = w["params"]
        elapsed = time.monotonic() - w["t0"]
        p = min(1.0, elapsed / w["duration"])
        if p >= 1.0:
            return None   # the tape has stopped
        die_at = T["tape_die_at"] / 100
        dying = _clamp01((p - die_at) / (1 - die_at))   # the last stretch: it gives up

        # Detune: a slow sag toward floor speed, a wow and a flutter that both deepen as it goes,
        # then a plunge to the floor at the end.
        sag = 1 - (1 - T["tape_floor_speed"] / 100) * _smooth(_clamp01(p / die_at))
        wow = math.sin(2 * math.pi * T["tape_wow_hz"] * elapsed) * (T["tape_wow_depth"] / 100) * p
        flutter = math.sin(2 * math.pi * T["tape_flutter_hz"] * elapsed) * (T["tape_flutter_depth"] / 100) * p
        plunge = (1 - dying) ** 2
        rate = max(MIN_RATE, sag * (1 + wow + flutter) * plunge)

        # Stutter: once it's dying, now and then the tape snags and repeats a sliver.
        if dying > 0 and random.random() < (T["tape_stutter"] / 100) * dying:
            self._pos -= (0.08 + random.random() * 0.12) * RATE

        # Variable-speed read with linear interpolation (block-constant rate).
        pos = self._pos + rate * np.arange(BLOCK)
        i0 = np.floor(pos).astype(np.int64) % n
        frac = (pos - np.floor(pos))[:, None].astype(np.float32)
        x = track[i0] * (1 - frac) + track[(i0 + 1) % n] * frac
        self._pos = (self._pos + rate * BLOCK) % n

        # Muffle: an exponential sweep from open down to filter_hz, with the resonance rising so it starts to howl.
        freq = 20000 * (T["tape_filter_hz"] / 20000) ** _smooth(p)
        q = 0.7 + (T["tape_q_max"] - 0.7) * p * p
        b, a = _lowpass(freq, q)
        y = np.empty_like(x)
        for c in range(2):
            y[:, c], w["zi"][c] = lfilter(b, a, x[:, c], zi=w["zi"][c])

        # Distort: drive climbs into the soft clipper; pull the output back so it doesn't just get louder.
        drive = 1 + (T["tape_drive_max"] - 1) * p * p
        y = np.tanh(y * drive) / math.tanh(1.0) / math.sqrt(drive)

        dropout = random.random() < (T["tape_dropout"] / 100) * dying
        gain = 0.0 if dropout else (1 - dying) ** 0.6
        return y * (self.volume * gain)
