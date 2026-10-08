"""Creates/updates the ElevenLabs Conversational AI agent so its config always matches persona.py."""

import os

from elevenlabs.client import ElevenLabs
from elevenlabs.core.api_error import ApiError

from . import persona
from .logbus import bus
from .settings import load_agent_id, save_agent_id

LLM = os.getenv("ENTITY_LLM", "claude-sonnet-5")
TEMPERATURE = 0.9
MAX_CONVERSATION_SECONDS = 300
AGENT_TAG = "washroom-entity"


def ensure_voice(client: ElevenLabs, voice_id: str) -> str:
    """Agents can only use voices in the account. If voice_id is a Voice Library voice, add it."""
    try:
        client.voices.get(voice_id)
        return voice_id
    except ApiError as e:
        if e.status_code not in (400, 404):
            raise
    shared = client.voices.get_shared(search=voice_id, page_size=5).voices
    match = next((v for v in shared if v.voice_id == voice_id), None)
    if not match:
        raise RuntimeError(f"Voice {voice_id} not found in your account or the ElevenLabs Voice Library")
    try:
        added = client.voices.share(match.public_owner_id, voice_id, new_name=match.name)
    except ApiError as e:
        bus.emit(
            "system",
            f"Couldn't add Voice Library voice '{match.name}' to your account ({describe_error(e)}). "
            "Trying it as-is; if the entity is silent or errors, add the voice in the ElevenLabs "
            "Voice Library or give the API key the voices_write permission.",
            "warn",
        )
        return voice_id
    bus.emit("system", f"Added Voice Library voice '{match.name}' to your ElevenLabs account")
    return added.voice_id or voice_id


def signed_url(client: ElevenLabs, agent_id: str) -> str:
    """A short-lived URL the browser uses to open the conversation websocket without seeing the API key."""
    return client.conversational_ai.conversations.get_signed_url(agent_id=agent_id).signed_url


def find_agent(client: ElevenLabs) -> str | None:
    """The agent this app created earlier, found by name and tag.

    Hosts like Render reset the disk on every deploy, so data/agent.json may be gone; without this the app
    would create a fresh agent each time.
    """
    try:
        page = client.conversational_ai.agents.list(search=persona.AGENT_NAME, page_size=30)
    except ApiError:
        return None
    for agent in page.agents:
        if agent.name == persona.AGENT_NAME and AGENT_TAG in (agent.tags or []):
            return agent.agent_id
    return None


def describe_error(e: Exception) -> str:
    """ElevenLabs ApiError str() includes every response header; keep just the useful part."""
    if isinstance(e, ApiError):
        detail = e.body.get("detail") if isinstance(e.body, dict) else e.body
        if isinstance(detail, dict):
            detail = detail.get("message") or detail
        return f"HTTP {e.status_code}: {detail}"
    return str(e)


# Per-conversation overrides the app sends (persona prompt, opening line, voice). The agent must allow them.
PLATFORM_SETTINGS = {
    "overrides": {
        "conversation_config_override": {
            "agent": {"first_message": True, "prompt": {"prompt": True}},
            "tts": {"voice_id": True},
        }
    }
}


def _conversation_config(base: persona.Persona, voice_id: str) -> dict:
    """The agent's stored defaults. Each conversation overrides prompt, first message and voice with its persona."""
    return {
        "agent": {
            "first_message": "{{opening_line}}",
            "language": "en",
            "hinglish_mode": False,
            "disable_first_message_interruptions": True,
            "dynamic_variables": {
                "dynamic_variable_placeholders": {
                    "opening_line": base.opening_lines[0],
                    "max_words": 25,
                }
            },
            "prompt": {
                "prompt": persona.system_prompt(base),
                "llm": LLM,
                "temperature": TEMPERATURE,
            },
        },
        # Only the voice is managed here. The TTS model (eleven_v3_conversational), expressive mode, audio
        # tags and voice settings are configured on the ElevenLabs website; the update is a PATCH, so
        # omitted fields are left untouched.
        "tts": {"voice_id": voice_id},
        # The app handles visitor silence itself; keep the agent from re-engaging on its own first.
        "turn": {"turn_timeout": 30},
        "conversation": {"max_duration_seconds": MAX_CONVERSATION_SECONDS},
    }


def check_persona_voices(client: ElevenLabs) -> dict[str, str]:
    """persona id -> usable voice id, for every persona whose voice is (or could be added to) the account."""
    voices = {}
    for p in persona.PERSONAS:
        try:
            voices[p.id] = ensure_voice(client, p.voice_id)
        except Exception as e:
            bus.emit("error", f"Persona '{p.name}' disabled: {describe_error(e)}", "error")
    return voices


def sync_agent(client: ElevenLabs) -> tuple[str, dict[str, str]]:
    """Create the agent on first run, otherwise push the current config.

    Returns the agent id and the usable voice for each available persona.
    """
    voices = check_persona_voices(client)
    if not voices:
        raise RuntimeError("No persona has a usable voice")
    base = next(p for p in persona.PERSONAS if p.id in voices)
    config = _conversation_config(base, voices[base.id])
    names = ", ".join(persona.PERSONA_BY_ID[i].name for i in voices)
    agent_id = load_agent_id()
    if not agent_id:
        agent_id = find_agent(client)
        if agent_id:
            bus.emit("system", f"Found the existing agent {agent_id} on ElevenLabs (set ELEVENLABS_AGENT_ID to skip this lookup)")
            save_agent_id(agent_id)

    if agent_id:
        try:
            client.conversational_ai.agents.update(
                agent_id, conversation_config=config, platform_settings=PLATFORM_SETTINGS, name=persona.AGENT_NAME
            )
            bus.emit("system", f"Agent {agent_id} synced (llm {LLM}; personas: {names})")
            return agent_id, voices
        except ApiError as e:
            if e.status_code in (401, 403):
                bus.emit(
                    "system",
                    f"Can't update agent {agent_id} ({describe_error(e)}); using it as configured on ElevenLabs",
                    "warn",
                )
                return agent_id, voices
            if e.status_code != 404:
                raise
            bus.emit("system", f"Saved agent {agent_id} no longer exists, creating a new one", "warn")

    created = client.conversational_ai.agents.create(
        conversation_config=config,
        platform_settings=PLATFORM_SETTINGS,
        name=persona.AGENT_NAME,
        tags=[AGENT_TAG],
    )
    save_agent_id(created.agent_id)
    bus.emit("system", f"Created ElevenLabs agent {created.agent_id} (personas: {names})")
    return created.agent_id, voices
