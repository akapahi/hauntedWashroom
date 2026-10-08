"""Runs one entity conversation per sensor trigger.

Flow: trigger -> (the dashboard winds the idle music down and plays res/<Persona>/approach.wav on its
own timeline) -> at ai_start_ms connect to the ElevenLabs agent -> entity speaks first (opening line) ->
visitor/entity exchange turns -> after the entity replies to visitor turn N-1 the agent is told the
next turn is the last -> visitor's turn N mutes the mic -> entity replies + says it's leaving ->
playback drains -> session ends -> cooldown -> idle.
"""

import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote
from urllib.parse import quote

from elevenlabs.client import ElevenLabs
from elevenlabs.conversational_ai.conversation import Conversation, ConversationInitiationData

from . import persona
from .agent_setup import MAX_CONVERSATION_SECONDS, describe_error, sync_agent
from .audio import EntityAudioInterface
from .logbus import bus
from .settings import RES_DIR, Settings, SettingsStore
from .shifter import VoicemeeterShifter

FAREWELL_IDLE_SECONDS = 1.5     # speaker quiet this long after the farewell => it's finished
FAREWELL_TIMEOUT_SECONDS = 20   # give up waiting for the farewell after this
NOTICE_IDLE_SECONDS = 0.3


@dataclass
class Session:
    settings: Settings
    started: float = field(default_factory=time.monotonic)
    connected: bool = False
    user_turns: int = 0
    silent_turns: int = 0         # times the visitor ignored the entity (drives its escalating mood)
    last_user_at: float = 0.0
    audio_mark: int = 0           # audio chunk count at the last visitor turn
    notice_sent: bool = False
    finishing: bool = False
    finish_started: float = 0.0
    finish_audio_mark: int = 0
    ended: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)


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


def approach_cue_url(character: persona.Persona) -> str | None:
    """The cue as the dashboard fetches it (/res/<folder>/approach.wav), or None."""
    cue = approach_cue(character)
    return "/res/" + quote(cue.relative_to(RES_DIR).as_posix()) if cue else None


def _is_speech(text: str) -> bool:
    # ASR sometimes returns "..." or empty strings for noise (flushes, hand dryers).
    return bool(re.search(r"\w", text or ""))


class EntityController:
    def __init__(self, store: SettingsStore):
        self.store = store
        self.client = ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])
        self.shifter = VoicemeeterShifter()
        self.agent_id: str | None = None
        self.persona_voices: dict[str, str] = {}  # persona id -> usable voice id
        self.state = "starting"
        self._lock = threading.Lock()
        self._agent_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._cooldown_until = 0.0
        self._publish_status()

    # ---- agent ----------------------------------------------------------------

    def apply_shift_settings(self):
        s = self.store.get()
        try:
            self.shifter.configure(s.shift_enabled, s.shift_outputs, s.shift_gain_db, s.shift_ms)
        except Exception as e:
            bus.emit("error", f"Voice shift setup failed: {e}", "error")

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

    # ---- control ----------------------------------------------------------------

    def trigger(self, source: str) -> tuple[bool, str]:
        with self._lock:
            if not self.agent_id:
                reason = "agent not ready"
            elif self.state in ("connecting", "active", "ending"):
                reason = "conversation already in progress"
            elif self.state == "cooldown":
                reason = f"cooling down ({max(0.0, self._cooldown_until - time.time()):.0f}s left)"
            else:
                reason = None
            if reason:
                bus.emit("trigger", f"Trigger from {source} ignored: {reason}")
                return False, reason
            self._stop_event.clear()
            self._set_state("connecting")
            threading.Thread(target=self._run, args=(self.store.get(), source), daemon=True, name="entity-session").start()
        return True, "started"

    def stop(self):
        """Ends the current conversation immediately (or skips the cooldown)."""
        self._stop_event.set()

    # ---- session ----------------------------------------------------------------

    def _run(self, settings: Settings, source: str):
        bus.emit("trigger", f"Triggered by {source}. Summoning the entity...")
        session = Session(settings)
        audio = EntityAudioInterface(
            half_duplex=settings.half_duplex,
            reverb_mix=settings.reverb_mix / 100,
            reverb_decay=settings.reverb_decay_ms / 1000,
        )
        conv: Conversation | None = None
        try:
            character = persona.pick_persona(settings.persona, list(self.persona_voices))
            if not character:
                raise RuntimeError("no persona has a usable voice")
            if settings.persona not in (persona.RANDOM, character.id):
                bus.emit("system", f"Persona '{settings.persona}' is unavailable, using {character.name}", "warn")
            bus.emit("system", f"Persona: {character.name}")
            cue_url = approach_cue_url(character)
            bus.set_status(persona=character.name, approach_cue=cue_url)
            if not cue_url:
                bus.emit("system", f"No approach cue for {character.name} (res/{character.name}/approach.wav)", "warn")

            # Timeline. The dashboard saw the state turn "connecting" and is running the tape wind-down and
            # the approach cue in the browser; the server's one job is to bring the AI in at ai_start_ms.
            hold = settings.ai_start_ms / 1000
            if hold:
                bus.emit("system", f"Timeline: the AI connects at {hold:.1f}s (tape and approach cue play on the dashboard)")
                self._stop_event.wait(hold)
            if self._stop_event.is_set():
                bus.emit("system", "Stopped before the entity arrived")
                return

            opening = random.choice(character.opening_lines)
            conv = Conversation(
                self.client,
                self.agent_id,
                requires_auth=True,
                audio_interface=audio,
                config=ConversationInitiationData(
                    conversation_config_override={
                        "agent": {
                            "prompt": {
                                "prompt": persona.system_prompt(character).replace(
                                    "{{max_words}}", str(settings.max_words)
                                )
                            },
                            "first_message": opening,
                        },
                        "tts": {"voice_id": self.persona_voices[character.id]},
                    },
                    dynamic_variables={"opening_line": opening, "max_words": settings.max_words},
                ),
                callback_agent_response=self._on_agent_response,
                callback_agent_response_correction=lambda orig, new: bus.emit("entity", f"(interrupted) {new}"),
                callback_user_transcript=lambda text: self._on_user_transcript(session, audio, text),
                callback_end_session=session.ended.set,
            )
            conv.start_session()
            self._supervise(conv, session, audio)
        except Exception as e:
            bus.emit("error", f"Conversation failed: {describe_error(e)}", "error")
        finally:
            if conv:
                if not session.ended.is_set():
                    conv.end_session()
                if conv._thread:
                    conv.wait_for_session_end()
                if conv._conversation_id:
                    bus.emit("system", f"Conversation ended (id {conv._conversation_id}, {session.user_turns} visitor turns)")
            self._cool_down(settings.cooldown)

    def _supervise(self, conv: Conversation, session: Session, audio: EntityAudioInterface):
        s = session.settings
        while True:
            now = time.monotonic()
            if self._stop_event.is_set():
                bus.emit("system", "Stopped from dashboard")
                return
            if session.ended.is_set():
                bus.emit("system", "Session closed by ElevenLabs", "warn")
                return
            if now - session.started > MAX_CONVERSATION_SECONDS + 10:
                bus.emit("system", "Max conversation duration reached", "warn")
                return

            if not session.connected and conv._conversation_id:
                session.connected = True
                self._set_state("active")
                bus.emit("system", f"Connected, the entity speaks first (max {s.max_turns} turns, ≤{s.max_words} words/reply)")

            if session.finishing:
                farewell_started = audio.chunks_received > session.finish_audio_mark
                if farewell_started and audio.idle_for() >= FAREWELL_IDLE_SECONDS:
                    bus.emit("system", "The entity has left")
                    return
                if now - session.finish_started > FAREWELL_TIMEOUT_SECONDS:
                    bus.emit("system", "Farewell timed out, ending session", "warn")
                    return
            elif session.connected:
                entity_replied = audio.chunks_received > session.audio_mark
                # Once the entity has finished replying to turn N-1, warn it that turn N is the last.
                if (
                    not session.notice_sent
                    and session.user_turns >= s.max_turns - 1
                    and entity_replied
                    and audio.idle_for() >= NOTICE_IDLE_SECONDS
                ):
                    self._send(conv, "contextual", persona.LAST_TURN_NOTICE)
                    session.notice_sent = True
                    bus.emit("system", "Told the entity the visitor's next reply is the last")

                if entity_replied:
                    silent_for = min(audio.idle_for(), now - session.last_user_at if session.last_user_at else 1e9)
                    if silent_for > s.silence_timeout:
                        self._on_silent_turn(conv, session, audio)

            self._stop_event.wait(0.1)

    def _on_agent_response(self, text: str):
        bus.emit("entity", text)
        # The agent response arrives as its audio starts streaming, so the glide happens while it speaks.
        if persona.SHIFT_TAG in text.lower():
            self.shifter.shift()

    def _count_turn(self, session: Session, audio: EntityAudioInterface, text: str) -> bool:
        """Registers a visitor turn (caller holds session.lock). Returns True if it was the last one."""
        session.user_turns += 1
        session.last_user_at = time.monotonic()
        session.audio_mark = audio.chunks_received  # the entity's reply to this turn comes after this mark
        turn, max_turns = session.user_turns, session.settings.max_turns
        bus.emit("visitor", f"[turn {turn}/{max_turns}] {text}")
        bus.set_status(turn=turn)
        if turn >= max_turns:
            bus.emit("system", "Final turn reached, the entity is saying goodbye")
            self._begin_farewell(session, audio)
            return True
        return False

    def _on_user_transcript(self, session: Session, audio: EntityAudioInterface, text: str):
        # Bracketed text is our own injected prompt, in case the server echoes it back as a transcript.
        if not _is_speech(text) or text.startswith("["):
            return
        with session.lock:
            if session.finishing:
                bus.emit("visitor", text)
                return
            self._count_turn(session, audio, text)

    def _on_silent_turn(self, conv: Conversation, session: Session, audio: EntityAudioInterface):
        """The visitor stayed quiet: that uses up a turn, and the entity taunts them (or leaves if it was the last)."""
        with session.lock:
            if session.finishing:
                return
            session.silent_turns += 1
            level = session.silent_turns
            last = self._count_turn(session, audio, f"(silent for {session.settings.silence_timeout}s)")
        mood = persona.silence_mood(level)
        if last:
            bus.emit("system", f"Silence #{level} on the final turn, the entity leaves ({mood})")
        else:
            bus.emit("system", f"Silence #{level}, the entity provokes them ({mood})")
        self._send(conv, "user", persona.silence_prompt(level, final=last))

    def _begin_farewell(self, session: Session, audio: EntityAudioInterface):
        session.finishing = True
        session.finish_started = time.monotonic()
        session.finish_audio_mark = audio.chunks_received
        audio.muted = True
        self._set_state("ending")

    def _send(self, conv: Conversation, kind: str, text: str):
        try:
            if kind == "contextual":
                conv.send_contextual_update(text)
            else:
                conv.send_user_message(text)
        except Exception as e:
            bus.emit("error", f"Could not send {kind} message: {e}", "error")

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
        extra = {"turn": 0} if self.state == "connecting" else {}
        bus.set_status(
            state=self.state,
            max_turns=self.store.get().max_turns,
            cooldown_until=self._cooldown_until or None,
            agent_id=self.agent_id,
            **extra,
        )
