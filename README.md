# Washroom Bhoot 👻 (Raspberry Pi edition)

A sensor-triggered entity haunting a washroom, out to spook whoever walks in. When the ESP32 fires, the idle music
winds down like a dying tape, the chosen character's approach cue plays, and the entity speaks first, holds a
short, sinister speech-to-speech conversation (ElevenLabs Conversational AI), announces that it's leaving after N
visitor turns (default 3), and goes quiet until the next trigger.

**Everything audible happens on the Pi**: the idle loop, the tape wind-down, the approach cue, the entity's voice
(with reverb) and the mic, all through PulseAudio. The web dashboard is only a remote control: logs, settings, a
simulate-sensor button, previews.

```
ESP32 ──POST /api/trigger──▶  FastAPI server (Raspberry Pi)  ──websocket──▶  ElevenLabs agent
                                 │  mic + speakers (PulseAudio)                 (ASR → LLM → TTS)
                                 └── dashboard (logs, settings, simulate) on :8000
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

## 2. Install on the Raspberry Pi

Raspberry Pi OS (Bookworm or later, 64-bit recommended). Its sound server is PipeWire with a PulseAudio front
end, which is what the app talks to; older images with plain PulseAudio work the same.

```bash
sudo apt install -y python3-venv libportaudio2 ffmpeg pulseaudio-utils
git clone <this repo> washroom && cd washroom
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # numpy/scipy come from piwheels, no compiling
cp .env.example .env                     # then put your ELEVENLABS_API_KEY in it
```

`ffmpeg` decodes the idle music (an .mp3); a `res/Infinite_Dancefloor.wav` works without it.

**Windows / other machines** work too for development (`python -m venv .venv; .venv\Scripts\activate;
pip install -r requirements.txt`); voice shifting is only logged there, since it needs PulseAudio.

## 3. Sound setup

1. Plug in your mic and speakers. `pactl list short sinks` lists the outputs (headphone jack, HDMI, USB sound
   cards), `pactl list short sources` the inputs. `pactl set-default-source <name>` picks the mic.
2. Leave `AUDIO_INPUT_DEVICE` / `AUDIO_OUTPUT_DEVICE` blank in `.env`: sounddevice then uses PulseAudio's defaults.
   `python list_devices.py` shows the indexes if you ever need to pin one.
3. **Voice shifting** (the entity moving around the room): put speakers in different spots, each on its own
   output, and enter their sink names in the dashboard's *Outputs* field (comma-separated; the field suggests
   the sinks it found). The app loads a PulseAudio `module-combine-sink` called `washroom_entity` over them and
   makes it the default sink, so the music, the cues and the entity reach every speaker. One output is "where
   the entity is" (at *Volume where it is*); the others sit at 0. When the entity writes `[shift]`, the
   volumes crossfade (equal-power) so the voice glides across the room. **Test shift** does it on demand.
   Leave *Outputs* blank to use a single speaker and no shifting.
4. The idle music loops whenever the entity is idle (♪ button or the *Idle music* setting). **Test tape** plays
   it for 2 s and winds it down so you can tune the effect without triggering the entity.

If the Pi has no sound after a reboot, make sure the app runs as the desktop user (PulseAudio/PipeWire is a
per-user service; see the systemd unit below).

## 4. Run

```bash
python run.py
```

Open `http://<pi-ip>:8000`. On first start the app creates an ElevenLabs agent called "Washroom Entity" (or
finds the one it created before, by name and tag) and saves its id to `data/agent.json`. On later starts it
pushes the current personas and voices to that agent. Set `DASHBOARD_PASSWORD` in `.env` if the dashboard is
reachable by people who shouldn't touch it.

## 5. ESP32 trigger

```
POST http://<pi-ip>:8000/api/trigger?sensor=<optional-name>
Header (only if TRIGGER_TOKEN is set in .env):  X-Trigger-Token: <token>

200 {"accepted": true,  "reason": "started", "state": "connecting"}
200 {"accepted": false, "reason": "conversation already in progress" | "cooling down (7s left)" | "agent not ready", ...}
401 bad token
```

The server ignores triggers while a conversation, a preview or a cooldown is running, so the ESP32 can fire on
every detection. The trigger URL is printed in the logs on startup. A ready-made sketch is in
[esp32/entity_trigger/entity_trigger.ino](esp32/entity_trigger/entity_trigger.ino): set the URL (plain http on the
LAN) and call `triggerEntity()` from your sensor code.

Quick test without hardware: `curl -X POST http://localhost:8000/api/trigger`

## 6. Dashboard

- **Simulate sensor** does the same thing as an ESP32 trigger.
- **Stop / skip cooldown** ends the current conversation or preview immediately, or skips the cooldown.
- **♪** (header) switches the idle music on or off (same as the *Idle music* setting).
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
    - *AI connects before approach ends*: the entity connects to ElevenLabs this long before the cue finishes
      (default 1 s), so the moment follows the cue's length; it speaks a second or two later.
    - **Preview timeline** plays the tape and the cue on the speakers with the saved values, without the AI.
  - *Idle music*: on/off and volume.
  - *Persona*: a specific character, or random each conversation.
  - *Reverb amount / room size*: echo added to the entity's voice before it reaches the speaker. 0% turns it off.
  - *Voice shifting*: see section 3.
  - *Half-duplex*: mutes the mic while the entity talks (plus a short echo tail). Keep this on with an open
    speaker, otherwise the entity hears itself.
  - *Tape wind-down*: sliders for every part of the effect (when it starts dying, how slow it sags, wow and
    flutter rate/depth, where the filter closes to, resonance, distortion drive, dropout and stutter odds).
    Saved on the server like everything else. **Test tape** uses the saved values.
- **Logs** stream live: triggers, what the visitor said (with the turn number), what the entity said,
  system events and errors. They are also written to `logs/entity.log`.

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

`app/persona.py` holds a list of personas: currently Manjulika (who speaks Hindi), Pennywise and Jigsaw. Each
has its own character prompt, voice ID, language and opening lines. The rules they all share are appended to
every prompt: reply format, `[shift]`, boundaries, the escalating reaction to silence, and leaving. Each
conversation sends the chosen persona's prompt, opening line, language and voice to the agent as overrides, so
all of them use the same LLM and the TTS settings configured on the ElevenLabs website. To add a character,
append a `Persona(...)` to the list.

## Customising the entity

Edit `app/persona.py` (characters, opening lines, shared rules, farewell notice), then restart the server or click
**Resync agent**. The LLM defaults to `claude-sonnet-5`. Override it with `ENTITY_LLM` in `.env` using any
model ElevenLabs agents support, for example `gpt-4o-mini` or `claude-haiku-4-5`.

## Run on boot (Raspberry Pi, systemd)

PulseAudio/PipeWire belongs to the logged-in user, so run the app as that user with its runtime directory:

```ini
# /etc/systemd/system/washroom-entity.service
[Unit]
Description=Washroom Bhoot
After=network-online.target sound.target
Wants=network-online.target

[Service]
User=pi
Environment=XDG_RUNTIME_DIR=/run/user/1000
WorkingDirectory=/home/pi/washroom
ExecStart=/home/pi/washroom/.venv/bin/python run.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable it with `sudo systemctl enable --now washroom-entity`, and `sudo loginctl enable-linger pi` so the
user's sound server runs without a login. Use `id -u pi` if the uid isn't 1000.

## Troubleshooting

- **`Invalid sample rate` / `-9997`**: some USB mics can't do 16 kHz natively. Leave the device blank (PulseAudio
  resamples) instead of pinning a raw `hw:` index.
- **No sound / `Couldn't open the speaker`**: `pactl info` must work as the user running the app; check the
  systemd unit's `User` and `XDG_RUNTIME_DIR`.
- **`PulseAudio sink(s) not found`**: the *Outputs* names must match `pactl list short sinks` exactly.
- **The entity answers itself / turns get used up**: turn on half-duplex, point the speaker away from the
  mic, or lower the speaker volume.
- **`ffmpeg not found`**: `sudo apt install ffmpeg`, or drop a `res/Infinite_Dancefloor.wav` next to the mp3.
- **`missing the permission convai_write`**: see section 1.
