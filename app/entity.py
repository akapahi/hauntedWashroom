"""Runs one entity conversation per sensor trigger, with every sound on this machine's speakers.

Flow: trigger -> the idle music winds down like a tape at detune_start_ms -> res/<Persona>/approach.wav plays
at approach_start_ms -> ai_lead_ms before the cue ends, connect to the ElevenLabs agent (mic + speaker here, via
PyAudio) -> entity speaks first (opening line) -> visitor/entity exchange turns -> after the entity replies to
visitor turn N-1 the agent is told the next turn is the last -> visitor's turn N mutes the mic -> entity replies
+ says it's leaving -> playback drains -> session ends -> cooldown -> idle (the music comes back).

The dashboard only shows logs and settings; it plays nothing itself.
"""

import os
import random
import re
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from elevenlabs.client import ElevenLabs
from elevenlabs.conversational_ai.conversation import Conversation, ConversationInitiationData

from . import persona
from .agent_setup import MAX_CONVERSATION_SECONDS, describe_error, sync_agent
from .audio import EntityAudioInterface, play_wav
from .logbus import bus
from .music import TAPE_PARAMS, MusicPlayer
from .settings import RES_DIR, Settings, SettingsStore
from .shifter import PulseShifter

MUSIC_FILES = ("Infinite_Dancefloor.wav", "Infinite_Dancefloor.mp3")
TAPE_TEST_LEAD_SECONDS = 2.0    # Test tape: play the music this long before winding it down
FAREWELL_IDLE_SECONDS = 1.5     # speaker quiet this long after the farewell => it's finished
FAREWELL_TIMEOUT_SECONDS = 20   # give up waiting for the farewell after this
NOTICE_IDLE_SECONDS = 0.3

BUSY_STATES = ("connecting", "active", "ending", "preview")


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
    """The cue's URL under /res (the dashboard only uses it to draw the timeline), or None."""
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


def _is_speech(text: str) -> bool:
    # ASR sometimes returns "..." or empty strings for noise (flushes, hand dryers).
    return bool(re.search(r"\w", text or ""))


class EntityController:
    def __init__(self, store: SettingsStore):
        self.store = store
        self.client = ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])
        self.shifter = PulseShifter()
        self.music = MusicPlayer(next((RES_DIR / f for f in MUSIC_FILES if (RES_DIR / f).is_file()), None))
        self.music.on_change = lambda what: bus.set_status(music=what)
        self.agent_id: str | None = None
        self.persona_voices: dict[str, str] = {}  # persona id -> usable voice id
        self.state = "starting"
        self._lock = threading.Lock()
        self._agent_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._cooldown_until = 0.0
        self._tape_testing = False
        self._publish_status()

    # ---- setup ----------------------------------------------------------------

    def setup_audio(self):
        """Blocking: decode the idle music, connect the PulseAudio outputs, start the music if idle."""
        self.music.load()
        self.apply_shift_settings()
        self.apply_music_settings()

    def apply_shift_settings(self):
        s = self.store.get()
        try:
            self.shifter.configure(s.shift_enabled, s.shift_outputs, s.shift_level, s.shift_ms)
        except Exception as e:
            bus.emit("error", f"Voice shift setup failed: {e}", "error")

    def apply_music_settings(self):
        """The idle loop runs whenever the entity is idle and the music is switched on."""
        s = self.store.get()
        self.music.volume = s.music_volume / 100
        if s.idle_music and self.state == "idle" and not self._tape_testing:
            self.music.start()
        elif not s.idle_music and not self.music.winding:
            self.music.stop()
        bus.set_status(music_available=self.music.loaded)

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
        return self._start(source, ai=True)

    def preview(self) -> tuple[bool, str]:
        """The timeline (tape wind-down + approach cue) without the AI, to line things up."""
        return self._start("dashboard (preview)", ai=False)

    def _start(self, source: str, ai: bool) -> tuple[bool, str]:
        with self._lock:
            settings = self.store.get()
            character = persona.pick_persona(settings.persona, list(self.persona_voices))
            if ai and not self.agent_id:
                reason = "agent not ready"
            elif self.state in BUSY_STATES:
                reason = "conversation already in progress"
            elif self.state == "cooldown":
                reason = f"cooling down ({max(0.0, self._cooldown_until - time.time()):.0f}s left)"
            elif self.state not in ("idle",) and ai:
                reason = f"not ready ({self.state})"
            elif not character:
                reason = "no persona has a usable voice"
            else:
                reason = None
            if reason:
                bus.emit("trigger", f"Trigger from {source} ignored: {reason}")
                return False, reason
            self._stop_event.clear()
            self._set_state("connecting" if ai else "preview")
            threading.Thread(target=self._run, args=(settings, character, source, ai), daemon=True, name="entity-session").start()
        return True, "started"

    def stop(self):
        """Ends the current conversation or preview immediately (or skips the cooldown)."""
        self._stop_event.set()

    def test_tape(self) -> tuple[bool, str]:
        """Play the idle music for a moment, then wind it down, without triggering the entity."""
        if not self.music.loaded:
            return False, "no idle music loaded"
        if self.state != "idle":
            return False, f"not idle ({self.state})"
        if self._tape_testing or self.music.winding:
            return False, "already testing"
        self._tape_testing = True
        threading.Thread(target=self._tape_test, daemon=True, name="tape-test").start()
        return True, "started"

    def _tape_test(self):
        try:
            s = self.store.get()
            self.music.volume = s.music_volume / 100
            self.music.start()
            time.sleep(TAPE_TEST_LEAD_SECONDS)
            bus.emit("system", f"Test tape: winding down over {s.detune_len_ms / 1000:.1f}s")
            self.music.wind_down(s.detune_len_ms / 1000, self._tape_params(s))
            while self.music.winding:
                time.sleep(0.1)
            time.sleep(0.5)
        finally:
            self._tape_testing = False
            self.apply_music_settings()

    def test_shift(self) -> str | None:
        return self.shifter.shift()

    # ---- session ----------------------------------------------------------------

    @staticmethod
    def _tape_params(settings: Settings) -> dict:
        return {k: getattr(settings, k) for k in TAPE_PARAMS}

    def _wait_until(self, t: float) -> bool:
        """Sleep until monotonic time t. False if Stop was pressed meanwhile."""
        while True:
            left = t - time.monotonic()
            if left <= 0:
                return not self._stop_event.is_set()
            if self._stop_event.wait(min(0.1, left)):
                return False

    def _run(self, settings: Settings, character: persona.Persona, source: str, ai: bool):
        bus.emit("trigger", f"Triggered by {source}. Summoning the entity..." if ai else "Preview: running the timeline without the AI")
        t0 = time.monotonic()
        session = Session(settings)
        audio: EntityAudioInterface | None = None
        conv: Conversation | None = None
        cue_thread: threading.Thread | None = None
        try:
            if settings.persona not in (persona.RANDOM, character.id):
                bus.emit("system", f"Persona '{settings.persona}' is unavailable, using {character.name}", "warn")
            bus.emit("system", f"Persona: {character.name}")
            cue = approach_cue(character)
            bus.set_status(persona=character.name, approach_cue=approach_cue_url(character))
            if not cue:
                bus.emit("system", f"No approach cue for {character.name} (res/{character.name}/approach.wav)", "warn")
            ai_at = ai_start_ms(settings, character) / 1000
            bus.emit(
                "system",
                f"Timeline: tape at {settings.detune_start_ms / 1000:.1f}s, approach at {settings.approach_start_ms / 1000:.1f}s"
                + (f", AI at {ai_at:.1f}s ({settings.ai_lead_ms / 1000:.1f}s before the cue ends)" if ai else ""),
            )

            # 1. The tape winds down.
            if not self._wait_until(t0 + settings.detune_start_ms / 1000):
                return
            if self.music.playing:
                self.music.wind_down(settings.detune_len_ms / 1000, self._tape_params(settings))
            else:
                bus.emit("system", "Idle music isn't playing, nothing to wind down")

            # 2. The approach cue.
            if not self._wait_until(t0 + settings.approach_start_ms / 1000):
                return
            if cue:
                cue_thread = threading.Thread(target=self._play_cue, args=(cue,), daemon=True, name="approach-cue")
                cue_thread.start()

            if not ai:
                while cue_thread and cue_thread.is_alive():
                    if self._stop_event.wait(0.1):
                        return
                while self.music.winding:
                    if self._stop_event.wait(0.1):
                        return
                bus.emit("system", "Preview finished")
                return

            # 3. The entity.
            if not self._wait_until(t0 + ai_at):
                return
            audio = EntityAudioInterface(
                half_duplex=settings.half_duplex,
                reverb_mix=settings.reverb_mix / 100,
                reverb_decay=settings.reverb_decay_ms / 1000,
            )
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
                                "prompt": persona.system_prompt(character).replace("{{max_words}}", str(settings.max_words))
                            },
                            "first_message": opening,
                            "language": character.language,
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
            bus.emit("error", f"{'Conversation' if ai else 'Preview'} failed: {describe_error(e)}", "error")
        finally:
            if conv:
                if not session.ended.is_set():
                    conv.end_session()
                if conv._thread:
                    conv.wait_for_session_end()
                if conv._conversation_id:
                    bus.emit("system", f"Conversation ended (id {conv._conversation_id}, {session.user_turns} visitor turns)")
            if self._stop_event.is_set():
                bus.emit("system", "Stopped from dashboard")
                if self.music.winding:
                    self.music.stop()
            if cue_thread and cue_thread.is_alive():
                cue_thread.join(timeout=2)
            if ai:
                self._cool_down(settings.cooldown)
            else:
                with self._lock:
                    self._set_state("idle")

    def _play_cue(self, cue: Path):
        try:
            play_wav(cue, self._stop_event)
        except Exception as e:
            bus.emit("error", f"Approach cue failed: {e}", "error")

    def _supervise(self, conv: Conversation, session: Session, audio: EntityAudioInterface):
        s = session.settings
        while True:
            now = time.monotonic()
            if self._stop_event.is_set():
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
        if state == "idle":
            self.apply_music_settings()   # the music comes back before the dashboard hears we're idle
        self._publish_status()

    def _publish_status(self):
        extra = {"turn": 0} if self.state in ("connecting", "preview") else {}
        bus.set_status(
            state=self.state,
            max_turns=self.store.get().max_turns,
            cooldown_until=self._cooldown_until or None,
            agent_id=self.agent_id,
            shift_available=self.shifter.available,
            **extra,
        )
