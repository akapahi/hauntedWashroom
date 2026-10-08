"""Runs one entity conversation per sensor trigger. The voice itself runs in a browser, not here.

Flow: trigger -> state "connecting" (every open dashboard winds the idle music down and plays
res/<Persona>/approach.wav on its own timeline) -> ai_lead_ms before the cue ends, the dashboard with voice enabled claims the
session (POST /api/session/claim) and gets a signed ElevenLabs URL plus the persona overrides -> it runs the
conversation with the ElevenLabs JS SDK on its own mic and speaker, counts turns, tells the agent when the
next turn is the last, mutes the mic for the farewell, and reports everything here (POST /api/session/report)
-> when it says the session has ended (or Stop is pressed, or it disconnects) -> cooldown -> idle.

The server never touches audio, so it can run anywhere (Render, a Pi, a laptop); the machine in the washroom
only needs a browser with the dashboard open and "voice: this device" switched on.
"""

import os
import random
import secrets
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from elevenlabs.client import ElevenLabs

from . import persona
from .agent_setup import MAX_CONVERSATION_SECONDS, describe_error, signed_url, sync_agent
from .logbus import bus
from .settings import RES_DIR, Settings, SettingsStore

CLAIM_GRACE_SECONDS = 20        # how long after ai_start_ms we wait for a dashboard to pick the session up
FAREWELL_IDLE_SECONDS = 1.5     # speaker quiet this long after the farewell => it's finished
FAREWELL_TIMEOUT_SECONDS = 20   # give up waiting for the farewell after this
NOTICE_IDLE_SECONDS = 0.3

CONVERSATION_STATES = ("connecting", "active", "ending")


@dataclass
class Session:
    id: str
    settings: Settings
    character: persona.Persona
    opening: str
    source: str
    ai_start_ms: int                    # ms after the trigger when the dashboard connects the AI
    started: float = field(default_factory=time.monotonic)
    claimed_by: str | None = None       # dashboard client id that runs the voice
    conversation_id: str | None = None
    user_turns: int = 0
    end_reason: str | None = None
    claimed: threading.Event = field(default_factory=threading.Event)
    ended: threading.Event = field(default_factory=threading.Event)


def approach_cue(character: persona.Persona) -> Path | None:
    """res/<Persona name or id>/approach.wav, matched case-insensitively, or None."""
    if not RES_DIR.is_dir():
        return None
    names = {character.id.lower(), character.name.lower()}
    for d in RES_DIR.iterdir():
        if d.is_dir() and d.name.lower() in names and (d / "approach.wav").is_file():
            return d / "approach.wav"
    return None


def approach_cue_url(character: persona.Persona) -> str | None:
    """The cue as the dashboard fetches it (/res/<folder>/approach.wav), or None."""
    cue = approach_cue(character)
    return "/res/" + quote(cue.relative_to(RES_DIR).as_posix()) if cue else None


def approach_cue_seconds(character: persona.Persona) -> float | None:
    cue = approach_cue(character)
    if not cue:
        return None
    try:
        with wave.open(str(cue), "rb") as wf:
            return wf.getnframes() / wf.getframerate()
    except (wave.Error, OSError, ZeroDivisionError):
        return None


def ai_start_ms(settings: Settings, character: persona.Persona) -> int:
    """When the AI connects: ai_lead_ms before the approach cue ends (at the cue's start if there is no cue)."""
    seconds = approach_cue_seconds(character)
    if seconds is None:
        return settings.approach_start_ms
    return max(0, settings.approach_start_ms + round(seconds * 1000) - settings.ai_lead_ms)


def _text(value, limit: int = 2000) -> str:
    return str(value if value is not None else "")[:limit]


class EntityController:
    def __init__(self, store: SettingsStore):
        self.store = store
        self.client = ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])
        self.agent_id: str | None = None
        self.persona_voices: dict[str, str] = {}  # persona id -> usable voice id
        self.state = "starting"
        self.session: Session | None = None
        self._clients: dict[str, bool] = {}        # dashboard client id -> has voice enabled
        self._lock = threading.Lock()
        self._agent_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._cooldown_until = 0.0
        self._publish_status()

    # ---- agent ----------------------------------------------------------------

    def sync_agent(self):
        """Blocking: create/update the ElevenLabs agent and check every persona's voice."""
        with self._agent_lock:
            try:
                self.agent_id, self.persona_voices = sync_agent(self.client)
                bus.set_status(personas=list(self.persona_voices))
                if self.state in ("starting", "error"):
                    self._set_state("idle")
            except Exception as e:
                bus.emit("error", f"Agent setup failed: {describe_error(e)}", "error")
                if self.state == "starting":
                    self._set_state("error")
                raise

    # ---- dashboards -------------------------------------------------------------

    @property
    def voice_clients(self) -> int:
        return sum(1 for v in self._clients.values() if v)

    def client_update(self, client_id: str, voice: bool):
        """A dashboard said hello (or toggled its voice switch)."""
        with self._lock:
            self._clients[client_id] = voice
        bus.set_status(voice_clients=self.voice_clients)

    def client_gone(self, client_id: str):
        with self._lock:
            self._clients.pop(client_id, None)
            session = self.session
        bus.set_status(voice_clients=self.voice_clients)
        if session and session.claimed_by == client_id and not session.ended.is_set():
            bus.emit("error", "The dashboard running the voice disconnected", "error")
            self._end_session(session, "dashboard disconnected")

    # ---- control ----------------------------------------------------------------

    def trigger(self, source: str) -> tuple[bool, str]:
        with self._lock:
            settings = self.store.get()
            character = persona.pick_persona(settings.persona, list(self.persona_voices))
            if not self.agent_id:
                reason = "agent not ready"
            elif self.state in CONVERSATION_STATES:
                reason = "conversation already in progress"
            elif self.state == "cooldown":
                reason = f"cooling down ({max(0.0, self._cooldown_until - time.time()):.0f}s left)"
            elif not self.voice_clients:
                reason = "no dashboard with voice enabled is connected"
            elif not character:
                reason = "no persona has a usable voice"
            else:
                reason = None
            if reason:
                bus.emit("trigger", f"Trigger from {source} ignored: {reason}")
                return False, reason
            self._stop_event.clear()
            self.session = Session(
                id=secrets.token_hex(8),
                settings=settings,
                character=character,
                opening=random.choice(character.opening_lines),
                source=source,
                ai_start_ms=ai_start_ms(settings, character),
            )
            self._set_state("connecting")
            threading.Thread(target=self._run, args=(self.session,), daemon=True, name="entity-session").start()
        return True, "started"

    def stop(self):
        """Ends the current conversation immediately (or skips the cooldown)."""
        self._stop_event.set()

    # ---- the voice dashboard talks to us ----------------------------------------------

    def claim(self, session_id: str, client_id: str) -> dict:
        """The dashboard that will run the conversation asks for the signed URL and the persona overrides."""
        with self._lock:
            session = self.session
            if not session or session.id != session_id or self.state != "connecting":
                raise LookupError("no conversation is waiting to be picked up")
            if session.claimed_by:
                raise PermissionError("another dashboard is already running this conversation"
                                      if session.claimed_by != client_id else "this conversation was already picked up")
            if not self._clients.get(client_id):
                raise PermissionError("this dashboard hasn't enabled voice")
            session.claimed_by = client_id
        try:
            url = signed_url(self.client, self.agent_id)
        except Exception as e:
            bus.emit("error", f"Couldn't get a signed URL from ElevenLabs: {describe_error(e)}", "error")
            self._end_session(session, "no signed url")
            raise
        session.claimed.set()
        bus.emit("system", "The dashboard picked up the conversation, connecting to ElevenLabs...")
        s = session.settings
        return {
            "session_id": session.id,
            "signed_url": url,
            "persona": session.character.name,
            "overrides": {
                "agent": {
                    "prompt": {"prompt": persona.system_prompt(session.character).replace("{{max_words}}", str(s.max_words))},
                    "firstMessage": session.opening,
                    "language": session.character.language,
                },
                "tts": {"voiceId": self.persona_voices[session.character.id]},
            },
            "dynamic_variables": {"opening_line": session.opening, "max_words": s.max_words},
            "settings": {
                k: getattr(s, k)
                for k in (
                    "max_turns", "max_words", "silence_timeout", "half_duplex",
                    "reverb_mix", "reverb_decay_ms", "shift_enabled", "shift_width", "shift_ms",
                )
            },
            "prompts": {
                "last_turn_notice": persona.LAST_TURN_NOTICE,
                "shift_tag": persona.SHIFT_TAG,
                "silence": [
                    {
                        "mood": persona.silence_mood(level),
                        "prompt": persona.silence_prompt(level, final=False),
                        "final_prompt": persona.silence_prompt(level, final=True),
                    }
                    for level in range(1, len(persona.SILENCE_MOODS) + 1)
                ],
            },
            "limits": {
                "max_seconds": MAX_CONVERSATION_SECONDS,
                "farewell_idle_s": FAREWELL_IDLE_SECONDS,
                "farewell_timeout_s": FAREWELL_TIMEOUT_SECONDS,
                "notice_idle_s": NOTICE_IDLE_SECONDS,
            },
        }

    def report(self, session_id: str, client_id: str, event: dict) -> bool:
        """Something happened in the browser's conversation. Returns False if it's about a session that's over."""
        session = self.session
        if not session or session.id != session_id or session.claimed_by != client_id or session.ended.is_set():
            return False
        kind = event.get("event")
        text = _text(event.get("text"))
        s = session.settings
        if kind == "connected":
            session.conversation_id = _text(event.get("conversation_id"), 100) or None
            if self.state == "connecting":
                self._set_state("active")
            bus.emit("system", f"Connected, the entity speaks first (max {s.max_turns} turns, ≤{s.max_words} words/reply)")
        elif kind == "entity":
            bus.emit("entity", text)
        elif kind == "visitor":
            turn = event.get("turn")
            if isinstance(turn, int) and turn > 0:
                session.user_turns = turn
                bus.emit("visitor", f"[turn {turn}/{s.max_turns}] {text}")
                bus.set_status(turn=turn)
            else:
                bus.emit("visitor", text)
        elif kind == "system":
            bus.emit("system", text, "warn" if event.get("level") == "warn" else "info")
        elif kind == "error":
            bus.emit("error", text, "error")
        elif kind == "ending":
            if self.state in ("connecting", "active"):
                self._set_state("ending")
        elif kind == "ended":
            self._end_session(session, _text(event.get("reason"), 200) or "ended")
        else:
            raise ValueError(f"unknown event '{kind}'")
        return True

    def _end_session(self, session: Session, reason: str):
        session.end_reason = session.end_reason or reason
        session.ended.set()

    # ---- session ----------------------------------------------------------------

    def _run(self, session: Session):
        s = session.settings
        bus.emit("trigger", f"Triggered by {session.source}. Summoning the entity...")
        try:
            if s.persona not in (persona.RANDOM, session.character.id):
                bus.emit("system", f"Persona '{s.persona}' is unavailable, using {session.character.name}", "warn")
            bus.emit("system", f"Persona: {session.character.name}")
            cue_url = approach_cue_url(session.character)
            bus.set_status(persona=session.character.name, approach_cue=cue_url)
            if not cue_url:
                bus.emit("system", f"No approach cue for {session.character.name} (res/{session.character.name}/approach.wav)", "warn")

            # Timeline. The dashboards saw the state turn "connecting" and run the tape wind-down and the
            # approach cue; the one with voice enabled brings the AI in at session.ai_start_ms (sent in the status).
            hold = session.ai_start_ms / 1000
            bus.emit("system", f"Timeline: the dashboard connects the AI at {hold:.1f}s, "
                     f"{s.ai_lead_ms / 1000:.1f}s before the approach cue ends (tape, cue and voice all play there)")
            deadline = time.monotonic() + hold + CLAIM_GRACE_SECONDS
            while not session.claimed.is_set():
                if self._stop_event.is_set():
                    bus.emit("system", "Stopped before the entity arrived")
                    return
                if session.ended.is_set():
                    return
                if time.monotonic() > deadline:
                    bus.emit(
                        "error",
                        f"No dashboard picked up the conversation within {CLAIM_GRACE_SECONDS}s of the AI start. "
                        "Is the dashboard in the washroom open, with voice enabled and armed?",
                        "error",
                    )
                    return
                self._stop_event.wait(0.2)

            hard_stop = time.monotonic() + MAX_CONVERSATION_SECONDS + 30
            while not session.ended.is_set():
                if self._stop_event.is_set():
                    bus.emit("system", "Stopped from dashboard")
                    return
                if time.monotonic() > hard_stop:
                    bus.emit("system", "Max conversation duration reached", "warn")
                    return
                self._stop_event.wait(0.2)
            conv = f", id {session.conversation_id}" if session.conversation_id else ""
            bus.emit("system", f"Conversation ended ({session.end_reason}{conv}, {session.user_turns} visitor turns)")
        finally:
            with self._lock:
                self.session = None
            self._cool_down(s.cooldown)

    def _cool_down(self, seconds: int):
        if seconds > 0 and not self._stop_event.is_set():
            self._cooldown_until = time.time() + seconds
            self._set_state("cooldown")
            self._stop_event.wait(seconds)
        with self._lock:
            self._cooldown_until = 0.0
            self._set_state("idle")

    # ---- status -----------------------------------------------------------------

    def _set_state(self, state: str):
        self.state = state
        self._publish_status()

    def _publish_status(self):
        extra = {"turn": 0, "ai_start_ms": self.session.ai_start_ms} if self.state == "connecting" and self.session else {}
        bus.set_status(
            state=self.state,
            max_turns=self.store.get().max_turns,
            cooldown_until=self._cooldown_until or None,
            agent_id=self.agent_id,
            session_id=self.session.id if self.session else None,
            voice_clients=self.voice_clients,
            **extra,
        )
