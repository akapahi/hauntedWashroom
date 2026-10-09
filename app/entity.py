"""One entity per connected dashboard. The voice itself runs in that dashboard's browser, not here.

Every dashboard that connects (websocket "hello") gets its own Client: its own state machine (idle ->
connecting -> active -> ending -> cooldown -> idle), its own session and its own ElevenLabs conversation.
Dashboards never share a conversation.

Flow for one client: trigger -> state "connecting" (that dashboard winds the idle music down and plays
res/<Persona>/approach.wav on its own timeline) -> ai_lead_ms before the cue ends, it claims the session
(POST /api/session/claim) and gets a signed ElevenLabs URL plus the persona overrides -> it runs the
conversation with the ElevenLabs JS SDK on its own mic and speaker, counts turns, tells the agent when the next
turn is the last, mutes the mic for the farewell, and reports everything here (POST /api/session/report)
-> when it says the session has ended (or Stop is pressed, or it disconnects) -> cooldown -> idle.

The ESP32 trigger starts a conversation on every idle dashboard that has voice enabled; the dashboard's own
Simulate button starts one on that dashboard only.

The server never touches audio, so it can run anywhere (Render, a Pi, a laptop).
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

CLAIM_GRACE_SECONDS = 20        # how long after the AI start we wait for the dashboard to pick the session up
FAREWELL_IDLE_SECONDS = 1.5     # speaker quiet this long after the farewell => it's finished
FAREWELL_TIMEOUT_SECONDS = 20   # give up waiting for the farewell after this
NOTICE_IDLE_SECONDS = 0.3

CONVERSATION_STATES = ("connecting", "active", "ending")


@dataclass
class Session:
    id: str
    client_id: str
    settings: Settings
    character: persona.Persona
    opening: str
    source: str
    ai_start_ms: int                    # ms after the trigger when the dashboard connects the AI
    started: float = field(default_factory=time.monotonic)
    claimed_by: str | None = None       # set once the dashboard has asked for the signed URL
    conversation_id: str | None = None
    user_turns: int = 0
    end_reason: str | None = None
    claimed: threading.Event = field(default_factory=threading.Event)
    ended: threading.Event = field(default_factory=threading.Event)


@dataclass
class Client:
    """A connected dashboard and the entity that haunts it."""
    id: str
    voice: bool = False                 # it has the mic and speakers (armed)
    state: str = "idle"
    session: Session | None = None
    cooldown_until: float = 0.0
    stop_event: threading.Event = field(default_factory=threading.Event)


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
        self.agent_state = "starting"             # starting | ready | error
        self.persona_voices: dict[str, str] = {}  # persona id -> usable voice id
        self.clients: dict[str, Client] = {}
        self._lock = threading.Lock()
        self._agent_lock = threading.Lock()
        self._publish_global()

    # ---- agent ----------------------------------------------------------------

    def sync_agent(self):
        """Blocking: create/update the ElevenLabs agent and check every persona's voice."""
        with self._agent_lock:
            try:
                self.agent_id, self.persona_voices = sync_agent(self.client)
                self.agent_state = "ready"
                bus.set_status(personas=list(self.persona_voices))
            except Exception as e:
                bus.emit("error", f"Agent setup failed: {describe_error(e)}", "error")
                if self.agent_state == "starting":
                    self.agent_state = "error"
                raise
            finally:
                self._publish_global()
                for c in list(self.clients.values()):
                    self._publish_client(c)

    # ---- dashboards -------------------------------------------------------------

    @property
    def voice_clients(self) -> int:
        return sum(1 for c in self.clients.values() if c.voice)

    def client_update(self, client_id: str, voice: bool):
        """A dashboard said hello (or toggled its voice switch)."""
        with self._lock:
            c = self.clients.get(client_id)
            if not c:
                c = self.clients[client_id] = Client(id=client_id)
            c.voice = voice
        self._publish_global()
        self._publish_client(c)

    def client_gone(self, client_id: str):
        with self._lock:
            c = self.clients.pop(client_id, None)
        self._publish_global()
        bus.drop_client_status(client_id)
        if not c:
            return
        c.stop_event.set()   # skips its cooldown too
        if c.session and not c.session.ended.is_set():
            bus.emit("error", "The dashboard disconnected during its conversation", "error", client=c.id)
            self._end_session(c.session, "dashboard disconnected")

    def states(self) -> dict[str, str]:
        return {c.id: c.state for c in self.clients.values()}

    # ---- control ----------------------------------------------------------------

    def trigger(self, client_id: str, source: str) -> tuple[bool, str]:
        """Start a conversation on one dashboard."""
        with self._lock:
            c = self.clients.get(client_id)
            settings = self.store.get()
            character = persona.pick_persona(settings.persona, list(self.persona_voices))
            if self.agent_state != "ready":
                reason = "agent not ready"
            elif not c:
                reason = "unknown dashboard (reload the page)"
            elif not c.voice:
                reason = "this dashboard hasn't enabled voice"
            elif c.state in CONVERSATION_STATES:
                reason = "conversation already in progress"
            elif c.state == "cooldown":
                reason = f"cooling down ({max(0.0, c.cooldown_until - time.time()):.0f}s left)"
            elif not character:
                reason = "no persona has a usable voice"
            else:
                reason = None
            if reason:
                bus.emit("trigger", f"Trigger from {source} ignored: {reason}", client=client_id)
                return False, reason
            c.stop_event.clear()
            c.session = Session(
                id=secrets.token_hex(8),
                client_id=c.id,
                settings=settings,
                character=character,
                opening=random.choice(character.opening_lines),
                source=source,
                ai_start_ms=ai_start_ms(settings, character),
            )
            self._set_state(c, "connecting")
            threading.Thread(target=self._run, args=(c, c.session), daemon=True, name=f"entity-{c.id[:8]}").start()
        return True, "started"

    def trigger_all(self, source: str) -> dict[str, tuple[bool, str]]:
        """The sensor fired: start a conversation on every dashboard that has voice enabled."""
        with self._lock:
            ids = [c.id for c in self.clients.values() if c.voice]
        if not ids:
            bus.emit("trigger", f"Trigger from {source} ignored: no dashboard with voice enabled is connected")
        return {cid: self.trigger(cid, source) for cid in ids}

    def stop(self, client_id: str | None = None):
        """Ends that dashboard's conversation immediately (or skips its cooldown); all of them if no id."""
        with self._lock:
            targets = [self.clients[client_id]] if client_id in self.clients else [] if client_id else list(self.clients.values())
        for c in targets:
            c.stop_event.set()

    # ---- the dashboard talks to us about its session ----------------------------------

    def _client_session(self, session_id: str, client_id: str) -> tuple[Client, Session] | None:
        c = self.clients.get(client_id)
        s = c.session if c else None
        return (c, s) if s and s.id == session_id else None

    def claim(self, session_id: str, client_id: str) -> dict:
        """The dashboard asks for the signed URL and the persona overrides for its own session."""
        with self._lock:
            found = self._client_session(session_id, client_id)
            if not found or found[0].state != "connecting":
                raise LookupError("no conversation is waiting to be picked up on this dashboard")
            c, session = found
            if session.claimed_by:
                raise PermissionError("this conversation was already picked up")
            if not c.voice:
                raise PermissionError("this dashboard hasn't enabled voice")
            session.claimed_by = client_id
        try:
            url = signed_url(self.client, self.agent_id)
        except Exception as e:
            bus.emit("error", f"Couldn't get a signed URL from ElevenLabs: {describe_error(e)}", "error", client=c.id)
            self._end_session(session, "no signed url")
            raise
        session.claimed.set()
        bus.emit("system", "The dashboard picked up the conversation, connecting to ElevenLabs...", client=c.id)
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
        found = self._client_session(session_id, client_id)
        if not found or not found[1].claimed_by or found[1].ended.is_set():
            return False
        c, session = found
        kind = event.get("event")
        text = _text(event.get("text"))
        s = session.settings
        if kind == "connected":
            session.conversation_id = _text(event.get("conversation_id"), 100) or None
            if c.state == "connecting":
                self._set_state(c, "active")
            bus.emit("system", f"Connected, the entity speaks first (max {s.max_turns} turns, ≤{s.max_words} words/reply)", client=c.id)
        elif kind == "entity":
            bus.emit("entity", text, client=c.id)
        elif kind == "visitor":
            turn = event.get("turn")
            if isinstance(turn, int) and turn > 0:
                session.user_turns = turn
                bus.emit("visitor", f"[turn {turn}/{s.max_turns}] {text}", client=c.id)
                bus.set_client_status(c.id, turn=turn)
            else:
                bus.emit("visitor", text, client=c.id)
        elif kind == "system":
            bus.emit("system", text, "warn" if event.get("level") == "warn" else "info", client=c.id)
        elif kind == "error":
            bus.emit("error", text, "error", client=c.id)
        elif kind == "ending":
            if c.state in ("connecting", "active"):
                self._set_state(c, "ending")
        elif kind == "ended":
            self._end_session(session, _text(event.get("reason"), 200) or "ended")
        else:
            raise ValueError(f"unknown event '{kind}'")
        return True

    def _end_session(self, session: Session, reason: str):
        session.end_reason = session.end_reason or reason
        session.ended.set()

    # ---- session ----------------------------------------------------------------

    def _run(self, c: Client, session: Session):
        s = session.settings
        bus.emit("trigger", f"Triggered by {session.source}. Summoning the entity...", client=c.id)
        try:
            if s.persona not in (persona.RANDOM, session.character.id):
                bus.emit("system", f"Persona '{s.persona}' is unavailable, using {session.character.name}", "warn", client=c.id)
            bus.emit("system", f"Persona: {session.character.name}", client=c.id)
            cue_url = approach_cue_url(session.character)
            bus.set_client_status(c.id, persona=session.character.name, approach_cue=cue_url)
            if not cue_url:
                bus.emit("system", f"No approach cue for {session.character.name} (res/{session.character.name}/approach.wav)", "warn", client=c.id)

            # Timeline. The dashboard saw its state turn "connecting" and runs the tape wind-down and the
            # approach cue; it brings the AI in at session.ai_start_ms (sent in its status).
            hold = session.ai_start_ms / 1000
            bus.emit("system", f"Timeline: the dashboard connects the AI at {hold:.1f}s, "
                     f"{s.ai_lead_ms / 1000:.1f}s before the approach cue ends", client=c.id)
            deadline = time.monotonic() + hold + CLAIM_GRACE_SECONDS
            while not session.claimed.is_set():
                if c.stop_event.is_set():
                    bus.emit("system", "Stopped before the entity arrived", client=c.id)
                    return
                if session.ended.is_set():
                    return
                if time.monotonic() > deadline:
                    bus.emit(
                        "error",
                        f"The dashboard didn't pick up the conversation within {CLAIM_GRACE_SECONDS}s of the AI start. "
                        "Is it still open, with voice enabled and armed?",
                        "error", client=c.id,
                    )
                    return
                c.stop_event.wait(0.2)

            hard_stop = time.monotonic() + MAX_CONVERSATION_SECONDS + 30
            while not session.ended.is_set():
                if c.stop_event.is_set():
                    bus.emit("system", "Stopped from dashboard", client=c.id)
                    return
                if time.monotonic() > hard_stop:
                    bus.emit("system", "Max conversation duration reached", "warn", client=c.id)
                    return
                c.stop_event.wait(0.2)
            conv = f", id {session.conversation_id}" if session.conversation_id else ""
            bus.emit("system", f"Conversation ended ({session.end_reason}{conv}, {session.user_turns} visitor turns)", client=c.id)
        finally:
            with self._lock:
                if c.session is session:
                    c.session = None
            self._cool_down(c, s.cooldown)

    def _cool_down(self, c: Client, seconds: int):
        if seconds > 0 and not c.stop_event.is_set():
            c.cooldown_until = time.time() + seconds
            self._set_state(c, "cooldown")
            c.stop_event.wait(seconds)
        with self._lock:
            c.cooldown_until = 0.0
            self._set_state(c, "idle")

    # ---- status -----------------------------------------------------------------

    def _set_state(self, c: Client, state: str):
        c.state = state
        self._publish_client(c)

    def _publish_global(self):
        bus.set_status(
            agent_state=self.agent_state,
            agent_id=self.agent_id,
            max_turns=self.store.get().max_turns,
            voice_clients=self.voice_clients,
        )

    def _publish_client(self, c: Client):
        if c.id not in self.clients:
            return   # it left; don't resurrect its status
        extra = {}
        if c.state == "connecting" and c.session:
            extra = {"turn": 0, "ai_start_ms": c.session.ai_start_ms}
        bus.set_client_status(
            c.id,
            state=c.state if self.agent_state == "ready" else self.agent_state,
            cooldown_until=c.cooldown_until or None,
            session_id=c.session.id if c.session else None,
            voice=c.voice,
            **extra,
        )
