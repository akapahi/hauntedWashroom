"""Moves the entity's voice around the room by crossfading Voicemeeter (Banana) output bus gains.

Setup: send the app's audio to a Voicemeeter input (AUDIO_OUTPUT_DEVICE = "Voicemeeter Input" or similar), route
that strip to every output in the shift list (e.g. A1 and A2), and put a speaker on each output in a different
spot. Exactly one output is "where the entity is" (at shift_gain_db); the others sit at -60 dB. When the entity
writes [shift], the gain glides from the current output to another one with an equal-power curve, so the
voice seems to travel rather than jump.

Talks to VoicemeeterRemote64.dll directly via ctypes (Windows only); elsewhere shifting is just logged.
"""

import ctypes
import math
import os
import random
import sys
import threading
import time
from pathlib import Path

from .logbus import bus

BUS_INDEX = {"A1": 0, "A2": 1, "A3": 2, "B1": 3, "B2": 4}
SILENT_DB = -60.0
STEPS_PER_SECOND = 40
DEFAULT_DLL_DIR = Path(r"C:\Program Files (x86)\VB\Voicemeeter")


def parse_outputs(value: str) -> list[str]:
    """'A1, a2' -> ['A1', 'A2']; raises ValueError for unknown names or fewer than two outputs."""
    outputs = [o.strip().upper() for o in value.split(",") if o.strip()]
    unknown = [o for o in outputs if o not in BUS_INDEX]
    if unknown:
        raise ValueError(f"Unknown Voicemeeter output(s): {', '.join(unknown)} (use {', '.join(BUS_INDEX)})")
    if len(set(outputs)) < 2:
        raise ValueError("Voice shifting needs at least two different outputs, e.g. A1,A2")
    return list(dict.fromkeys(outputs))


def _find_dll() -> Path | None:
    candidates = []
    if os.getenv("VOICEMEETER_DLL"):
        candidates.append(Path(os.environ["VOICEMEETER_DLL"]))
    try:
        import winreg

        key = r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\VB:Voicemeeter {17359A74-1236-5467}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
            uninstall = winreg.QueryValueEx(k, "UninstallString")[0]
        candidates.append(Path(uninstall).parent / "VoicemeeterRemote64.dll")
    except OSError:
        pass
    candidates.append(DEFAULT_DLL_DIR / "VoicemeeterRemote64.dll")
    return next((c for c in candidates if c.exists()), None)


class VoicemeeterShifter:
    def __init__(self):
        self._dll = None
        self._lock = threading.Lock()
        self._moving = threading.Event()
        self.outputs: list[str] = ["A1", "A2"]
        self.gain_db = 0.0
        self.duration = 2.5
        self.enabled = True
        self.location: str | None = None

    # ---- connection ----------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._dll is not None

    def connect(self) -> bool:
        if self._dll:
            return True
        if sys.platform != "win32":
            bus.emit("system", "Voice shifting needs Voicemeeter (Windows); [shift] will only be logged", "warn")
            return False
        path = _find_dll()
        if not path:
            bus.emit("system", "VoicemeeterRemote64.dll not found; set VOICEMEETER_DLL in .env to enable voice shifting", "warn")
            return False
        dll = ctypes.WinDLL(str(path))
        result = dll.VBVMR_Login()
        if result < 0:
            bus.emit("error", f"Voicemeeter login failed (code {result})", "error")
            return False
        if result == 1:
            bus.emit("system", "Voicemeeter isn't running; start Voicemeeter Banana for voice shifting", "warn")
        self._dll = dll
        bus.emit("system", "Connected to Voicemeeter for voice shifting")
        return True

    def close(self):
        with self._lock:
            if self._dll:
                self._dll.VBVMR_Logout()
                self._dll = None

    # ---- control -------------------------------------------------------------------

    def configure(self, enabled: bool, outputs: str, gain_db: float, duration_ms: int):
        """Apply settings; puts the entity at the first output if the output list changed."""
        new_outputs = parse_outputs(outputs)
        self.enabled = enabled
        self.gain_db = float(gain_db)
        self.duration = duration_ms / 1000
        if new_outputs != self.outputs or self.location not in new_outputs:
            self.outputs = new_outputs
            self.place(new_outputs[0])
        elif self.location:
            self.place(self.location)  # re-apply the gain level

    def place(self, output: str):
        """Jump (no fade) so the entity is at `output`."""
        if not self.enabled or not self.connect():
            return
        self._set_gains({o: (self.gain_db if o == output else SILENT_DB) for o in self.outputs})
        self.location = output
        bus.set_status(shift_location=output)

    def shift(self) -> str | None:
        """Start gliding to another output in the background. Returns the destination, or None if not possible."""
        if not self.enabled:
            bus.emit("system", "The entity tried to shift, but voice shifting is turned off")
            return None
        if not self.connect():
            return None
        if self._moving.is_set():
            bus.emit("system", "The entity tried to shift while already moving; ignored")
            return None
        source = self.location if self.location in self.outputs else self.outputs[0]
        target = random.choice([o for o in self.outputs if o != source])
        self._moving.set()
        threading.Thread(target=self._glide, args=(source, target), daemon=True, name="entity-shift").start()
        bus.emit("system", f"The entity shifts {source} → {target} over {self.duration:.1f}s")
        return target

    def _glide(self, source: str, target: str):
        try:
            start = time.monotonic()
            p = 0.0
            while p < 1.0:
                # Progress follows the clock, so API call overhead can't stretch the move.
                p = min(1.0, (time.monotonic() - start) / self.duration)
                # Equal-power crossfade keeps the perceived loudness steady mid-move.
                self._set_gains({
                    source: self._db(math.cos(p * math.pi / 2)),
                    target: self._db(math.sin(p * math.pi / 2)),
                })
                time.sleep(1 / STEPS_PER_SECOND)
            self.location = target
            bus.set_status(shift_location=target)
        except Exception as e:
            bus.emit("error", f"Voice shift failed: {e}", "error")
        finally:
            self._moving.clear()

    def _db(self, amplitude: float) -> float:
        if amplitude <= 1e-3:
            return SILENT_DB
        return max(SILENT_DB, self.gain_db + 20 * math.log10(amplitude))

    def _set_gains(self, gains: dict[str, float]):
        script = "".join(f"Bus[{BUS_INDEX[o]}].Gain={g:.2f};" for o, g in gains.items())
        with self._lock:
            if not self._dll:
                return
            result = self._dll.VBVMR_SetParameters(script.encode("ascii"))
        if result != 0:
            raise RuntimeError(f"Voicemeeter rejected '{script}' (code {result})")
