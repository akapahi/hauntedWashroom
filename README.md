# Washroom Bhoot 👻

A sensor-triggered entity haunting a washroom, out to spook whoever walks in. When the ESP32 fires, the entity speaks first,
holds a short, sinister speech-to-speech conversation (ElevenLabs Conversational AI), announces that it's
leaving after N visitor turns (default 3), and goes quiet until the next trigger.

The server has no audio of its own, so it can run anywhere, including a free Render web service. All the sound
(idle music, tape wind-down, approach cue and the entity's voice and mic) comes from the browser. **Every browser
that opens the dashboard with 🎙 voice on gets its own entity and its own ElevenLabs conversation**: one washroom
machine, or several devices at once, each haunted separately. The sensor wakes all of them; the dashboard's own
Simulate button wakes only that device.

```
ESP32 ──POST /api/trigger──▶  FastAPI server (Render)  ──websocket: logs, status──▶  dashboard(s)
                                 │   signed URL + persona                                 │
                                 ▼                                                       ▼
                           ElevenLabs API                    washroom browser: 🎙 voice on, mic + speakers
                                                                 └──websocket──▶ ElevenLabs agent (ASR → LLM → TTS)
```

## 1. ElevenLabs API key permissions

The key (`ELEVENLABS_API_KEY`) needs these permissions (ElevenLabs → Developers → API Keys → edit):

| Permission | Why |
|---|---|
| **ElevenAgents / Conversational AI: Write** | create/update the agent, mint signed URLs for conversations |
| **Voices: Read** | check the configured voice |
| **Voices: Write** *(optional)* | auto-add a Voice Library voice to your account. Otherwise add it yourself: Voice Library → search the voice ID → "Add to my voices" |

Each persona's voice is set in `app/persona.py`. Voices must be in your account or the public Voice Library
(the app adds library voices automatically). A persona whose voice can't be found is disabled and logged.

## 2. Deploy on Render

1. Push this repo to GitHub, then in Render: **New → Blueprint**, pick the repo. [render.yaml](render.yaml)
   describes the service (Python, `python run.py`, health check on `/api/health`).
2. Fill in the environment variables it asks for:
   - `ELEVENLABS_API_KEY` (required)
   - `TRIGGER_TOKEN`: generated for you; copy it into the ESP32 sketch. Without it anyone with the URL can trigger the entity.
   - `DASHBOARD_PASSWORD`: generated for you (or set your own). The dashboard asks for it once (any username).
     Without it anyone with the URL can control the entity and spend your ElevenLabs credits.
3. Open `https://<your-service>.onrender.com`. On first start the app creates an ElevenLabs agent called
   "Washroom Entity" (or finds the one it created before, by name and tag). On later starts it pushes the current
   personas and voices to it.

Things to know about Render:
- **The disk resets on every deploy.** The agent is found again by name, so no duplicates. Dashboard settings
  (`data/settings.json`) go back to defaults, though: either add a persistent disk (see the comment in
  `render.yaml`, paid plans) or re-enter them after a deploy. Set `ELEVENLABS_AGENT_ID` to pin the agent and skip
  the lookup.
- **The free plan sleeps** after 15 minutes without requests, and the first trigger after that takes about a
  minute to be answered, which the ESP32 sketch allows for. Use the *starter* plan on a show night.
- The server only needs `PORT` (Render sets it) and the variables above.

### Running it locally / on a LAN instead

```bash
python3 -m venv .venv && . .venv/bin/activate     # Windows: python -m venv .venv; .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                               # then put your ELEVENLABS_API_KEY in it
python run.py
```

Open `http://<machine-ip>:8000`. Browsers only allow the microphone on `https://` or on `localhost`, so for
the washroom device either run the browser on the same machine as the server (`http://localhost:8000`) or
host on Render.

## 3. The washroom device

On each device that should be haunted (a washroom machine wired to speakers and a mic, or any phone or laptop):

1. Open the dashboard in Chrome. A popup says the experience needs access to your microphone and speakers;
   that one click triggers Chrome's microphone prompt and unlocks sound. The 🎙 button in the header turns
   green: *voice: this device*, and this device now has its own entity. On a dashboard that should only watch
   the logs, click the 🎙 button afterwards to turn voice off there; the choice is remembered per browser.
2. After a reload the voice device asks Chrome for the microphone by itself and shows a *click anywhere to
   enable sound* popup, because browsers block audio until the page has been clicked once. The server ignores
   triggers while no armed voice device is connected (the dashboard tells you so).
3. Keep the tab open and the screen on; the SDK asks for a screen wake lock while a conversation runs.

For an unattended kiosk, start Chrome with `--autoplay-policy=no-user-gesture-required` and allow the
microphone for the site in its settings, so a reboot doesn't need a click. The device needs internet (it talks to
ElevenLabs directly) and loads the ElevenLabs browser SDK from jsdelivr.

**Speakers.** The entity's voice is stereo. With *voice shifting* on, it sits on the left channel and, when it
writes `[shift]`, glides across to the right (and back next time), so put one speaker on each channel in
different spots of the room. **Test shift** plays a hum that travels across them.

## 4. ESP32 trigger

```
POST https://<your-service>.onrender.com/api/trigger?sensor=<optional-name>
Header (only if TRIGGER_TOKEN is set):  X-Trigger-Token: <token>

200 {"accepted": true,  "reason": "started on 2 dashboard(s)", "clients": {"<id>": "started", ...}}
200 {"accepted": false, "reason": "conversation already in progress" | "cooling down (7s left)" | "agent not ready"
                                  | "no dashboard with voice enabled is connected", "clients": {...}}
401 bad token
```

The sensor starts a conversation on every connected dashboard that has voice enabled, each in its own
session. A dashboard that is already in a conversation or cooling down ignores it, so the ESP32 can fire on
every detection. The trigger URL is printed in the logs on startup. A ready-made sketch is in
[esp32/entity_trigger/entity_trigger.ino](esp32/entity_trigger/entity_trigger.ino): set the URL and token and call
`triggerEntity()` from your sensor code (it speaks https to Render).

Quick test without hardware: `curl -X POST -H "X-Trigger-Token: <token>" https://<your-service>.onrender.com/api/trigger`

## 5. Dashboard

- The state pill, the turn bar and the controls are all about *this* device's entity. Each dashboard has its own.
- **Simulate sensor** starts a conversation on this device only (the real sensor starts one on every voice device).
- **Stop / skip cooldown** ends this device's conversation immediately, or skips its cooldown.
- **🎙 voice** (header): whether *this* browser runs an entity, with its mic and speakers. See section 3.
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
    - *AI connects before approach ends*: the voice device asks the server for a signed URL and opens the
      ElevenLabs conversation this long before the approach cue finishes (default 1 s), so the moment follows
      the cue's length; the entity speaks a second or two later. If no voice device picks it up within 20 s,
      the trigger is written off and the entity cools down.
    - **Preview timeline** runs the tape and approach cue with the values in the form (unsaved too)
      without calling the AI; a playhead shows where you are. Real triggers use the saved values.
      Stop, or anything that ends the conversation, cancels the dashboard side too.

    Every dashboard plays its own idle loop, tape wind-down and approach cue when its own entity is
    triggered (mute the ♪ button on the ones that shouldn't).
  - *Persona*: a specific character, or random each conversation.
  - *Reverb amount / room size*: echo added to the entity's voice on the voice device before it reaches the
    speaker. 0% turns it off.
  - *Voice shifting*: see section 3. *How far to the sides* is the stereo width (100% = one speaker at a time).
  - *Half-duplex*: mutes the mic while the entity talks (plus the reverb tail). Keep this on with an open
    speaker, otherwise the entity hears itself.
- **Idle music**: `res/Infinite_Dancefloor.mp3` loops on the dashboard page whenever the state is `idle`.
  A trigger starts the timeline (tape wind-down, approach cue); any other change of state pauses it. The ♪ button in the header turns it
  off (remembered in the browser); browsers block autoplay until you click the page once, and the button
  says so.
- **Tape wind-down** panel: sliders for every part of the effect (duration, when it starts dying, how
  slow it sags, wow and flutter rate/depth, where the filter closes to, resonance, distortion drive,
  dropout and stutter odds). **Test tape** plays the music for 2 s and winds it down without triggering
  the entity. These live in the browser's localStorage, not on the server, so each dashboard device
  keeps its own.
- **Logs** stream live: triggers, what the visitor said (with the turn number), what the entity said,
  system events and errors. By default a dashboard shows its own entity's lines plus server-wide ones; tick
  **All devices** to see every device's conversation, tagged with the first characters of its id. Everything
  is also written to `logs/entity.log`.

## How a conversation ends

1. The entity speaks a random opening line from `app/persona.py`.
2. Each visitor utterance counts as one turn. Noise transcripts like "..." are ignored.
3. After the entity finishes replying to turn N-1, the voice device sends the agent a contextual update saying the
   next turn is the last.
4. On turn N the mic is muted, the entity replies and says it's leaving, and the session closes once the
   speaker has gone quiet.
5. If the visitor stays silent past the timeout, the silence counts as their turn and the entity provokes
   them: taunting the 1st time, agitated the 2nd, angry and offended from the 3rd on. If that was the last
   turn, it says goodbye in that mood instead.

## Personas

`app/persona.py` holds a list of personas: currently Manjulika, Pennywise and Jigsaw. Each has its own
character prompt, voice ID and opening lines. The rules they all share are appended to every prompt: reply
format, `[shift]`, boundaries, the escalating reaction to silence, and leaving. Each conversation sends the
chosen persona's prompt, opening line and voice to the agent as overrides, so all of them use the same LLM and
the TTS settings configured on the ElevenLabs website. To add a character, append a `Persona(...)` to the list.

## Customising the entity

Edit `app/persona.py` (characters, opening lines, shared rules, farewell notice), then redeploy or click
**Resync agent**. The LLM defaults to `claude-sonnet-5`. Override it with `ENTITY_LLM` using any
model ElevenLabs agents support, for example `gpt-4o-mini` or `claude-haiku-4-5`.

## Troubleshooting

- **"no dashboard with voice enabled is connected"**: on the washroom device answer the popup (or click 🎙 voice),
  allow the mic, and click the page once. The header shows *voice: this device* when it's ready.
- **"No dashboard picked up the conversation"**: the voice device lost its connection or wasn't armed in time.
  Check its 🎙 button and that the tab is still open.
- **The entity answers itself / turns get used up**: turn on half-duplex, point the speaker away from the
  mic, or lower the speaker volume.
- **"Couldn't hook into the SDK's audio output"**: a newer SDK build changed how it plays audio; the entity still
  speaks, without reverb or shifting. Pin the version in `static/index.html` back to `1.27.0`.
- **`missing the permission convai_write`**: see section 1.
- **Microphone not available**: the dashboard must be on `https://` (Render) or `localhost`, and the mic allowed for the site.
