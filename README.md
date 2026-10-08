# Washroom Bhoot 👻

A sensor-triggered entity haunting a washroom, out to spook whoever walks in. When the ESP32 fires, the entity speaks first,
holds a short, sinister speech-to-speech conversation (ElevenLabs Conversational AI), announces that it's
leaving after N visitor turns (default 3), and goes quiet until the next trigger.

Audio runs on the machine's own mic and speaker. The web dashboard is only for logs, settings and a
simulate-sensor button.

```
ESP32 ──POST /api/trigger──▶  FastAPI server (Pi)  ──websocket──▶  ElevenLabs agent
                                 │   ▲                               (ASR → LLM → TTS)
                       mic/speaker   └── dashboard (logs, settings, simulate) on :8000
```

## 1. ElevenLabs API key permissions

The key in `.env` (`ELEVENLABS_API_KEY`) needs these permissions (ElevenLabs → Developers → API Keys → edit):

| Permission | Why |
|---|---|
| **ElevenAgents / Conversational AI: Write** | create/update the agent, start conversations |
| **Voices: Read** | check the configured voice |
| **Voices: Write** *(optional)* | auto-add a Voice Library voice to your account. Otherwise add it yourself: Voice Library → search the voice ID → "Add to my voices" |

Each persona's voice is set in `app/persona.py`. Voices must be in your account or the public Voice Library
(the app adds library voices automatically). A persona whose voice can't be found is disabled and logged.

## 2. Install

**Raspberry Pi / Debian / Ubuntu**

```bash
sudo apt install -y python3-venv python3-dev portaudio19-dev
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then put your ELEVENLABS_API_KEY in it
```

**Windows**

```powershell
python -m venv .venv; .venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Run

```bash
python run.py
```

Open `http://<machine-ip>:8000`. On first start the app creates an ElevenLabs agent called
"Washroom Bhoot" and saves its id to `data/agent.json`. On later starts it pushes the current persona
and voice to that agent.

Pick audio devices with `python list_devices.py`, then set `AUDIO_INPUT_DEVICE` / `AUDIO_OUTPUT_DEVICE`
in `.env` (leave them blank to use the system default).

## 4. ESP32 trigger

```
POST http://<pi-ip>:8000/api/trigger?sensor=<optional-name>
Header (only if TRIGGER_TOKEN is set in .env):  X-Trigger-Token: <token>

200 {"accepted": true,  "reason": "started", "state": "connecting"}
200 {"accepted": false, "reason": "conversation already in progress" | "cooling down (7s left)" | "agent not ready", ...}
401 bad token
```

The server ignores triggers while a conversation or cooldown is running, so the ESP32 can fire on every
detection. The trigger URL is printed in the logs on startup. A ready-made sketch is in
[esp32/entity_trigger/entity_trigger.ino](esp32/entity_trigger/entity_trigger.ino): call `triggerEntity()`
from your sensor code.

Quick test without hardware: `curl -X POST http://localhost:8000/api/trigger`

## 5. Dashboard

- **Simulate sensor** does the same thing as an ESP32 trigger.
- **Stop / skip cooldown** ends the current conversation immediately, or skips the cooldown.
- **Settings** apply to the next conversation and are saved in `data/settings.json`:
  - *Turns per conversation*: how many times the visitor speaks before the entity leaves.
  - *Reply length*: maximum words per entity reply.
  - *Provoke after silence*: if the visitor stays quiet this long, the entity provokes them. The silence
    counts as a turn. It gets worse each time: taunting, then agitated, then angry and offended.
  - *Cooldown*: how long after a conversation triggers are ignored.
  - *Timeline* (seconds after the trigger), drawn as bars so you can line things up:
    - *Detune starts / Detune length*: the idle music winds down like a tape (slowly detunes with a
      wow and flutter, muffles into a resonant howl, distorts, then stutters and dies).
    - *Approach starts*: `res/<Persona name>/approach.wav` (folder matched case-insensitively, the
      persona id works too) starts playing. Overlapping the tape's tail is the default.
    - *AI connects*: the server contacts the ElevenLabs agent; the entity speaks a second or two later.
    - **Preview timeline** runs the tape and approach cue with the values in the form (unsaved too)
      without calling the AI; a playhead shows where you are. Real triggers use the saved values.
      Stop, or anything that ends the conversation, cancels the dashboard side too.

    For now the idle loop, the tape wind-down and the approach cue all play **in the browser** that has
    the dashboard open; only the entity's own voice comes from the server's `AUDIO_OUTPUT_DEVICE`.
    The plan is to move all of it server-side later (`play_wav` in `app/audio.py` is the start).
  - *Persona*: a specific character, or random each conversation.
  - *Voice shifting*: see below. **Test shift** moves its voice once, without a conversation.
  - *Reverb amount / room size*: echo added to the entity's voice on this machine before it reaches the
    speaker. 0% turns it off.
  - *Half-duplex*: mutes the mic while the entity talks. Keep this on with an open speaker, otherwise the
    entity hears itself.
- **Idle music**: `res/Infinite_Dancefloor.mp3` loops on the dashboard page whenever the state is `idle`.
  A trigger starts the timeline (tape wind-down, approach cue); any other change of state pauses it. The ♪ button in the header turns it
  off (remembered in the browser); browsers block autoplay until you click the page once, and the button
  says so. The track plays on whatever device has the dashboard open, not through the entity's speaker.
- **Tape wind-down** panel: sliders for every part of the effect (duration, when it starts dying, how
  slow it sags, wow and flutter rate/depth, where the filter closes to, resonance, distortion drive,
  dropout and stutter odds). **Test tape** plays the music for 2 s and winds it down without triggering
  the entity. These live in the browser's localStorage, not on the server, so each dashboard device
  keeps its own.
- **Logs** stream live: triggers, what the visitor said (with the turn number), what the entity said,
  system events and errors. They are also written to `logs/entity.log`.

## Voice shifting (Voicemeeter Banana, Windows)

The entity can move around the room. When its reply starts with `[shift]` (the persona tells it to use it
sparingly, and to hint at the move without announcing it), the app crossfades Voicemeeter output bus gains so
its voice glides from one speaker to another. The output it's at sits at *Gain where it is*; the others sit
at -60 dB. The fade is equal-power, so the loudness stays steady while it moves.

Setup:
1. Put a speaker on each output you list (default `A1,A2`), in different spots in the room.
2. Send the app's audio into Voicemeeter: run `python list_devices.py` and set `AUDIO_OUTPUT_DEVICE` to a
   Voicemeeter input device (e.g. "Voicemeeter Input" / "VoiceMeeter VAIO").
3. In Voicemeeter, route that input strip to **all** of the listed outputs (A1 and A2 lit).
4. Start the app and press **Test shift** on the dashboard.

The app controls those buses' gains, so don't use them for other audio. On startup it places the entity at the
first output. Shifting needs Voicemeeter, so it is Windows-only; elsewhere `[shift]` is only logged.

## How a conversation ends

1. The entity speaks a random opening line from `app/persona.py`.
2. Each visitor utterance counts as one turn. Noise transcripts like "..." are ignored.
3. After the entity finishes replying to turn N-1, the app sends the agent a contextual update saying the
   next turn is the last.
4. On turn N the mic is muted, the entity replies and says it's leaving, and the session closes once the
   speaker has gone quiet.
5. If the visitor stays silent past the timeout, the silence counts as their turn and the entity provokes
   them: taunting the 1st time, agitated the 2nd, angry and offended from the 3rd on. If that was the last
   turn, it says goodbye in that mood instead.

## Personas

`app/persona.py` holds a list of personas: currently the Washroom Entity, Vecna and Pennywise. Each has its own
character prompt, voice ID and opening lines. The rules they all share are appended to every prompt: reply
format, `[shift]`, boundaries, the escalating reaction to silence, and leaving. Each conversation sends the
chosen persona's prompt, opening line and voice to the agent as overrides, so all of them use the same LLM and
the TTS settings configured on the ElevenLabs website. To add a character, append a `Persona(...)` to the list.

## Customising the entity

Edit `app/persona.py` (characters, opening lines, shared rules, farewell notice), then restart the server or click
**Resync agent**. The LLM defaults to `claude-sonnet-5`. Override it with `ENTITY_LLM` in `.env` using any
model ElevenLabs agents support, for example `gpt-4o-mini` or `claude-haiku-4-5`.

## Run on boot (Raspberry Pi, systemd)

```ini
# /etc/systemd/system/washroom-entity.service
[Unit]
Description=Washroom Bhoot
After=network-online.target sound.target
Wants=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/washroom_el
ExecStart=/home/pi/washroom_el/.venv/bin/python run.py
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable it with `sudo systemctl enable --now washroom-entity`. Run it as the desktop user (or add that user
to the `audio` group) so it can open the sound devices.

## Troubleshooting

- **`Invalid sample rate` / `-9997` on the Pi**: some USB mics can't do 16 kHz natively. Use the ALSA
  `default`/`pulse` device (it resamples) instead of a raw `hw:` device index.
- **The entity answers itself / turns get used up**: turn on half-duplex, point the speaker away from the
  mic, or lower the speaker volume.
- **`missing the permission convai_write`**: see section 1.
- **No sound**: run `python list_devices.py` and set `AUDIO_OUTPUT_DEVICE`.
