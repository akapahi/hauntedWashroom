"""Moves the entity's voice around the room by crossfading PulseAudio sink volumes (Raspberry Pi / Linux).

Setup: plug a speaker into each sound output you want to use (the headphone jack, HDMI, USB sound cards) and put
them in different spots. `pactl list short sinks` shows their names; list two or more of them in the dashboard's
"Outputs" setting. The app then builds a PulseAudio combine sink ("washroom_entity") that copies its audio to all
of them and makes it the default sink, so everything the app plays (idle music, cues, the entity) reaches every
speaker. Exactly one of them is "where the entity is" (at shift_level %); the others sit at 0. When the entity
writes [shift], the volumes glide from the current sink to another one along an equal-power curve, so the voice
seems to travel rather than jump.

Everything goes through `pactl`, which also talks to PipeWire's PulseAudio server on recent Raspberry Pi OS.
Without pactl (e.g. Windows), shifting is only logged.
"""

import math
import random
import shutil
import subprocess
import threading
import time

from .logbus import bus

COMBINE_SINK = "washroom_entity"
STEPS_PER_SECOND = 20


def parse_outputs(value: str) -> list[str]:
    """'alsa_output.a, alsa_output.b' -> ['alsa_output.a', 'alsa_output.b']; blank is allowed (shifting off)."""
    outputs = [o.strip() for o in value.split(",") if o.strip()]
    if any(" " in o or "," in o for o in outputs):
        raise ValueError("Sink names can't contain spaces")
    if outputs and len(set(outputs)) < 2:
        raise ValueError("Voice shifting needs at least two different sinks (pactl list short sinks)")
    return list(dict.fromkeys(outputs))


def _percent(amplitude: float, level: int) -> int:
    # pactl percentages are on PulseAudio's cubic scale: 100% = unity, 50% ≈ -18 dB.
    # So a linear amplitude a maps to 100 * cbrt(a) percent.
    if amplitude <= 1e-3:
        return 0
    return max(0, min(150, round(level * amplitude ** (1 / 3))))


class PulseShifter:
    def __init__(self):
        self._lock = threading.Lock()
        self._moving = threading.Event()
        self.outputs: list[str] = []
        self.level = 100
        self.duration = 2.5
        self.enabled = True
        self.location: str | None = None
        self._warned = False

    # ---- pactl ----------------------------------------------------------------------

    @property
    def available(self) -> bool:
        return shutil.which("pactl") is not None

    def _pactl(self, *args: str, quiet: bool = False) -> str | None:
        """Runs pactl; returns its stdout, or None on failure (logged unless quiet)."""
        try:
            r = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired) as e:
            if not quiet:
                bus.emit("error", f"pactl {' '.join(args)}: {e}", "error")
            return None
        if r.returncode != 0:
            if not quiet:
                bus.emit("error", f"pactl {' '.join(args)}: {r.stderr.strip() or 'failed'}", "error")
            return None
        return r.stdout

    def sinks(self) -> list[str]:
        out = self._pactl("list", "short", "sinks", quiet=True) or ""
        return [line.split("\t")[1] for line in out.splitlines() if "\t" in line]

    def _ensure_combine_sink(self) -> bool:
        """A combine sink over the configured outputs, as the default sink, so the app's audio reaches all of them."""
        missing = [o for o in self.outputs if o not in self.sinks()]
        if missing:
            bus.emit("error", f"PulseAudio sink(s) not found: {', '.join(missing)} (see `pactl list short sinks`)", "error")
            return False
        modules = self._pactl("list", "short", "modules") or ""
        wanted = f"slaves={','.join(self.outputs)}"
        for line in modules.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[1] == "module-combine-sink" and f"sink_name={COMBINE_SINK}" in parts[2]:
                if wanted in parts[2]:
                    break
                self._pactl("unload-module", parts[0])   # outputs changed: rebuild it
        else:
            if self._pactl("load-module", "module-combine-sink", f"sink_name={COMBINE_SINK}", wanted) is None:
                return False
            bus.emit("system", f"Created PulseAudio combine sink '{COMBINE_SINK}' over {', '.join(self.outputs)}")
        self._pactl("set-default-sink", COMBINE_SINK)
        return True

    # ---- control -------------------------------------------------------------------

    def configure(self, enabled: bool, outputs: str, level: int, duration_ms: int):
        """Apply settings; puts the entity at the first output if the output list changed."""
        new_outputs = parse_outputs(outputs)
        self.enabled = enabled
        self.level = int(level)
        self.duration = duration_ms / 1000
        if not enabled or not new_outputs:
            self.outputs = new_outputs
            self.location = None
            bus.set_status(shift_location=None)
            return
        if not self.available:
            if not self._warned:
                bus.emit("system", "pactl not found: voice shifting needs PulseAudio (or PipeWire); [shift] will only be logged", "warn")
                self._warned = True
            self.outputs = new_outputs
            return
        changed = new_outputs != self.outputs
        self.outputs = new_outputs
        if changed or self.location not in new_outputs:
            if self._ensure_combine_sink():
                self.place(new_outputs[0])
        else:
            self.place(self.location)  # re-apply the level

    def place(self, output: str):
        """Jump (no fade) so the entity is at `output`."""
        if not self._ready():
            return
        self._set_volumes({o: (1.0 if o == output else 0.0) for o in self.outputs})
        self.location = output
        bus.set_status(shift_location=output)

    def shift(self) -> str | None:
        """Start gliding to another output in the background. Returns the destination, or None if not possible."""
        if not self.enabled:
            bus.emit("system", "The entity tried to shift, but voice shifting is turned off")
            return None
        if not self.outputs:
            bus.emit("system", "The entity tried to shift, but no PulseAudio outputs are configured")
            return None
        if not self.available:
            bus.emit("system", "The entity tried to shift (no PulseAudio here, so only logged)")
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

    def _ready(self) -> bool:
        return self.enabled and bool(self.outputs) and self.available

    def _glide(self, source: str, target: str):
        try:
            start = time.monotonic()
            p = 0.0
            while p < 1.0:
                # Progress follows the clock, so pactl's latency can't stretch the move.
                p = min(1.0, (time.monotonic() - start) / self.duration)
                # Equal-power crossfade keeps the perceived loudness steady mid-move.
                self._set_volumes({source: math.cos(p * math.pi / 2), target: math.sin(p * math.pi / 2)})
                time.sleep(1 / STEPS_PER_SECOND)
            self.location = target
            bus.set_status(shift_location=target)
        except Exception as e:
            bus.emit("error", f"Voice shift failed: {e}", "error")
        finally:
            self._moving.clear()

    def _set_volumes(self, amplitudes: dict[str, float]):
        with self._lock:
            for sink, amp in amplitudes.items():
                if self._pactl("set-sink-volume", sink, f"{_percent(amp, self.level)}%", quiet=True) is None:
                    raise RuntimeError(f"couldn't set the volume of sink {sink}")
