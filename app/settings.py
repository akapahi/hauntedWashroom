"""Runtime settings, editable from the dashboard and persisted to data/settings.json."""

import json
import os
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
RES_DIR = ROOT_DIR / "res"  # media: the idle loop, and res/<Persona>/approach.wav per character
SETTINGS_FILE = DATA_DIR / "settings.json"
AGENT_FILE = DATA_DIR / "agent.json"


@dataclass
class Settings:
    max_turns: int = 3          # visitor replies before the entity leaves
    max_words: int = 25         # upper bound on words per entity reply
    silence_timeout: int = 10   # seconds of visitor silence before the entity provokes them (uses up a turn)
    cooldown: int = 10          # seconds after a conversation during which triggers are ignored
    # Timeline, in ms after the trigger. All of it plays on this machine's speakers.
    detune_start_ms: int = 0        # idle music starts winding down like a tape
    detune_len_ms: int = 3000       # how long the wind-down takes
    approach_start_ms: int = 2000   # res/<Persona>/approach.wav starts (overlapping the tape's tail by default)
    ai_lead_ms: int = 1000          # the entity connects to ElevenLabs this long before the approach cue ends
    persona: str = "random"     # persona id from persona.py, or "random" (voices live in persona.py)
    # Idle music: res/Infinite_Dancefloor.mp3 (or .wav) loops whenever the entity is idle.
    idle_music: bool = True
    music_volume: int = 50      # %
    # The entity's voice.
    half_duplex: bool = True    # mute the mic while the entity speaks (prevents speaker echo)
    reverb_mix: int = 25        # reverb wet level, % (0 = off)
    reverb_decay_ms: int = 1500 # reverb decay time (room size)
    # Voice shifting: the entity's voice moves between PulseAudio sinks (speakers in different spots).
    shift_enabled: bool = True
    shift_outputs: str = ""     # PulseAudio sink names, comma-separated (pactl list short sinks); blank = off
    shift_level: int = 100      # volume % of the sink the entity is at (the others sit at 0)
    shift_ms: int = 2500        # how long the voice takes to travel
    # Tape wind-down: how the idle music dies when the entity is triggered (see music.py).
    tape_die_at: int = 72           # % of the way through, the point where it starts giving up
    tape_floor_speed: int = 55      # % speed it sags down to before the final plunge
    tape_wow_hz: float = 0.4        # slow pitch wobble, Hz
    tape_wow_depth: int = 7         # ±%
    tape_flutter_hz: float = 6.0    # fast pitch wobble, Hz
    tape_flutter_depth: float = 1.2 # ±%
    tape_filter_hz: int = 220       # the lowpass closes down to this
    tape_q_max: float = 10.0        # resonance at the end
    tape_drive_max: float = 14.0    # distortion drive at the end
    tape_dropout: int = 25          # % chance per block of a dropout while dying
    tape_stutter: int = 12          # % chance per block of a stutter while dying


# (min, max) for numeric settings
LIMITS = {
    "max_turns": (1, 10),
    "max_words": (5, 100),
    "silence_timeout": (5, 25),
    "cooldown": (0, 600),
    "detune_start_ms": (0, 60000),
    "detune_len_ms": (500, 30000),
    "approach_start_ms": (0, 60000),
    "ai_lead_ms": (0, 30000),
    "music_volume": (0, 100),
    "reverb_mix": (0, 100),
    "reverb_decay_ms": (300, 4000),
    "shift_level": (0, 100),
    "shift_ms": (300, 10000),
    "tape_die_at": (30, 95),
    "tape_floor_speed": (10, 100),
    "tape_wow_hz": (0.1, 3),
    "tape_wow_depth": (0, 30),
    "tape_flutter_hz": (1, 15),
    "tape_flutter_depth": (0, 10),
    "tape_filter_hz": (50, 5000),
    "tape_q_max": (0.7, 25),
    "tape_drive_max": (1, 40),
    "tape_dropout": (0, 100),
    "tape_stutter": (0, 100),
}


class SettingsStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._settings = Settings()
        DATA_DIR.mkdir(exist_ok=True)
        if SETTINGS_FILE.exists():
            try:
                self._apply(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                pass  # fall back to defaults on a corrupt file

    def get(self) -> Settings:
        with self._lock:
            return Settings(**asdict(self._settings))

    def update(self, changes: dict) -> tuple[Settings, set[str]]:
        """Validate and apply changes. Returns the new settings and the keys that changed."""
        with self._lock:
            before = asdict(self._settings)
            try:
                self._apply(changes, strict=True)
            except (ValueError, TypeError):
                self._settings = Settings(**before)  # all-or-nothing
                raise
            after = asdict(self._settings)
            SETTINGS_FILE.write_text(json.dumps(after, indent=2), encoding="utf-8")
            changed = {k for k in after if after[k] != before[k]}
            return Settings(**after), changed

    def _apply(self, data: dict, strict: bool = False):
        types = {f.name: f.type for f in fields(Settings)}
        for key, value in data.items():
            if key not in types:
                if strict:
                    raise ValueError(f"Unknown setting: {key}")
                continue
            if key in LIMITS:
                lo, hi = LIMITS[key]
                value = float(value) if types[key] in (float, "float") else int(value)
                if not lo <= value <= hi:
                    raise ValueError(f"{key} must be between {lo} and {hi}")
            elif types[key] in (bool, "bool"):
                value = bool(value)
            elif key == "shift_outputs":
                from .shifter import parse_outputs  # local import: shifter -> logbus -> settings

                value = ",".join(parse_outputs(str(value)))
            elif key == "persona":
                from .persona import PERSONA_BY_ID, RANDOM

                value = str(value).strip()
                if value != RANDOM and value not in PERSONA_BY_ID:
                    raise ValueError(f"Unknown persona '{value}' (use random, {', '.join(PERSONA_BY_ID)})")
            setattr(self._settings, key, value)


def load_agent_id() -> str | None:
    if os.getenv("ELEVENLABS_AGENT_ID"):
        return os.environ["ELEVENLABS_AGENT_ID"]
    if AGENT_FILE.exists():
        try:
            return json.loads(AGENT_FILE.read_text(encoding="utf-8")).get("agent_id")
        except (ValueError, OSError):
            return None
    return None


def save_agent_id(agent_id: str):
    DATA_DIR.mkdir(exist_ok=True)
    AGENT_FILE.write_text(json.dumps({"agent_id": agent_id}, indent=2), encoding="utf-8")
