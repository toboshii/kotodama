# Kotodama

AI voiceover for Final Fantasy XI. Kotodama reads NPC quest and cutscene dialog as it appears in your chat log and speaks it aloud, with a different voice for each kind of character.

Kotodama is a Windower addon plus a small Python program, the **sidecar**, that runs next to the game and does the speaking.

This README has three parts:

- **[Getting started](#getting-started)**: everything you need to hear voices in game.
- **[Power users](#power-users)**: every command, the other voice providers, voice casting, loudness, and cost.
- **[Developers](#developers)**: how it works inside, and how to run the tests.

# Getting started

## What you need

- Final Fantasy XI with Windower 4.
- Python 3.10 or newer.
- On Linux and the Steam Deck, an audio player: `pw-play`, `paplay`, or `ffplay`. Windows needs nothing extra.
- An API key for **Inworld**, the default voice provider. Create one at platform.inworld.ai (API Keys) and copy the **Base64** credential. To use a different provider, see **[Voice providers](#voice-providers)**.

## What gets sent where

Dialog text is sent to the voice provider you use (OpenAI, ElevenLabs, MiniMax, Google, Inworld, a model on Replicate, or a TTS server you point it at) to be turned into speech. With a voice-cloning model, the reference clip chosen from your voice bank is uploaded to Replicate with each line. If you turn on voice casting (optional, off by default), the NPC's name, race, zone and up to three sample lines are sent to the casting provider you pick. **Your character's name is replaced with `{player}` before anything is stored in a shareable file or sent for casting.** Speech requests do contain the literal line, including your name if the NPC says it, because that's what has to be spoken.

## Linux and the Steam Deck

1. **Give it your key.** Put this line in `~/.config/kotodama.env`:
   ```
   INWORLD_API_KEY=...
   ```
   Then make the file private: `chmod 600 ~/.config/kotodama.env`.
2. **Check the setup.** In the addon folder, run:
   ```
   python3 server/kotodama_server.py doctor
   ```
   It checks your key, the voices, audio playback and the network port, and tells you what to fix.
3. **Hear it without the game:**
   ```
   python3 server/kotodama_server.py test --name Ayame --race 2 "Welcome to Norg."
   ```
4. **Start the sidecar** from a normal host terminal:
   ```
   python3 server/kotodama_server.py
   ```
   Leave it running while you play. Press **R** to restart it or **Q** to quit. No Enter or Ctrl+C needed, which helps with the Steam Deck's on-screen keyboard.

   Don't run it from a sandboxed terminal (e.g. a Flatpak editor's terminal): it needs your real audio session and the host's loopback. To start it automatically when you log in, see **[Running the sidecar](#running-the-sidecar)**.

## Windows (experimental)

Windows support is new. If something doesn't work, `server\kotodama.cmd doctor` is the first thing to run, and its output is the most useful thing to report.

1. **Install Python 3.10 or newer** from [python.org](https://www.python.org/downloads/). Keep "py launcher" ticked in the installer.
2. **Give it your key.** Create the folder `%APPDATA%\Kotodama` (paste that into Explorer's address bar to find `AppData\Roaming`). In it, create a text file named `kotodama.env` with this line:
   ```
   INWORLD_API_KEY=...
   ```
   Check that Notepad didn't save it as `kotodama.env.txt`.
3. **Check the setup.** Open a Command Prompt in the addon folder and run:
   ```
   server\kotodama.cmd doctor
   ```
   `doctor` also checks that the sidecar can save its cache and library. A Windower installed under `Program Files` can't be written to, so install Windower somewhere else, or point `cache.dir` and `library.file` in `server\config.json` at a folder you own.
4. **Hear it without the game:**
   ```
   server\kotodama.cmd test --name Ayame --race 2 "Welcome to Norg."
   ```
5. **Start the sidecar** by double-clicking `server\kotodama.cmd`. Leave its window open while you play. Press **R** to restart it or **Q** to quit. Closing the window also stops it.

## Load the addon

In game, type `//lua load kotodama`. To load it automatically, add `lua load kotodama` to your Windower `scripts/init.txt`.

If the sidecar isn't running you'll get one chat warning, and the game is otherwise unaffected.

## Everyday commands

All commands start with `//koto` (or `//kotodama`).

| Command | What it does |
|---|---|
| `on` / `off` | Turn voiceover on or off |
| `mode all` / `events` / `cutscenes` | **all**: every NPC line; **events**: only quest dialog and cutscenes; **cutscenes**: only cutscenes |
| `volume <0-400>` | Voice loudness in percent. 100 is normal |
| `duck <0-100>` | How much of the game's audio is kept while a line is spoken (default 40) |
| `stop`, `replay` | Stop the current line, or play the last one again |
| `deny t` / `allow t` | Silence your target, or undo that |
| `status` | Check that the game is connected to the sidecar (`link UP`) |

Your settings are saved and come back next time. For every command, see **[All in-game commands](#all-in-game-commands)**.

## If something doesn't work

- Run `doctor` (step 2 above) and fix what it reports.
- `//koto status` should say `link UP`. If it doesn't, check that the sidecar is running.
- If voices are too quiet next to the music, raise `//koto volume` (try 200), or lower the game's own volume.

# Power users

## The key file

`~/.config/kotodama.env` (Linux) or `%APPDATA%\Kotodama\kotodama.env` (Windows) holds one key per line. Kotodama reads these keys:

```
INWORLD_API_KEY=...
ELEVENLABS_API_KEY=...
MINIMAX_API_KEY=...
GEMINI_API_KEY=...
OPENAI_API_KEY=sk-...
REPLICATE_API_TOKEN=r8_...
```

- The sidecar reads this file itself, whether you start it from a terminal or with the systemd unit.
- A variable already set in your shell takes precedence over the file.
- Pressing R in the sidecar terminal re-reads the file, so edits take effect without restarting the terminal.
- To use a different file, pass `--env-file PATH` or set `KOTODAMA_ENV_FILE`.
- The file can also set `KOTODAMA_CONFIG` to keep your `config.json` outside the addon folder.

## Running the sidecar

**Restarting.** Press **R** in the sidecar terminal to restart it. This picks up code and config changes. The game stays connected across a restart: the addon notices, says "voice server restarted." and sends your settings (volume, provider, ducking) again. On Windows, other keys such as Insert, Page Down and the F-keys are ignored.

**Starting automatically on Linux.** Install the systemd *user* unit so the sidecar runs whenever you're logged in:

```
cp server/kotodama.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now kotodama
```

Under systemd there's no terminal, so use `systemctl --user restart kotodama` to restart it.

**Starting automatically on Windows.** Press Win+R, enter `shell:startup`, and put a shortcut to `kotodama.cmd` in the folder that opens.

**More checks:**

```
python3 server/kotodama_server.py doctor --live               # also speak one short line with every provider that has a key
python3 server/kotodama_server.py doctor --provider inworld   # ...or with just one
python3 server/kotodama_server.py test --name Ayame --race 2 "Welcome to Norg."   # run twice: the second comes from cache, no API call
```

The live test runs every provider at once, reports each one's time to first audio, and ends with them ranked fastest first. It plays and caches nothing, and costs a fraction of a cent per provider. A cold Replicate model can take a minute.

On Windows, use `server\kotodama.cmd` in place of `python3 server/kotodama_server.py` in every command in this README. Python from the Microsoft Store isn't needed, and `kotodama.cmd` skips the Store's `python` placeholder.

## All in-game commands

`//koto` (or `//kotodama`)

| Command | What it does |
|---|---|
| `on` / `off` | Turn voiceover on or off |
| `mode all` / `events` / `cutscenes` | **all**: every NPC line; **events**: only NPC events (quest dialog and cutscenes, not one-line ambient chatter); **cutscenes**: only cutscenes |
| `battle on/off` | Voice battle chatter such as Trusts (default off) |
| `own on/off` | Voice your own character's lines (default on) |
| `narrator on/off` | Voice lines with no speaker name (default off). A character the game hasn't introduced yet, shown as `???`, isn't narration: they're always voiced, with one voice for the whole scene, under a name like `??? (243:10191)` (zone and event). Their race comes from their actor in the scene when the game has exactly one `???` with a body nearby. When they're introduced (the same actor or look, or the scene's only `???` while their name is said), they keep that voice. `//koto voice last` adjusts them |
| `interrupt on/off` | A new line cuts off the previous one (on), or lines queue (off) |
| `prefetch on/off` | Warm the connection and cast voices when you target or start talking to an NPC, so their first line is quicker (default on). NPCs whose race the game doesn't report are cast from their first line instead |
| `volume <0-400>` | Voice loudness in percent. 100 is normal; go higher if the voice sits below the game audio (see **[Loudness and ducking](#loudness-and-ducking)**) |
| `duck on/off`, `duck <0-100>` | Lower the game's audio while a line is spoken; the number is how much of it is kept (default 40) |
| `norm on/off/default` | Even out loudness between voices (see **[Loudness and ducking](#loudness-and-ducking)**). `default` follows `audio.normalize.enabled` in `server/config.json` (on unless changed) |
| `stop`, `replay` | Playback controls |
| `provider <name>` / `provider default` | Switch the voice provider live: `inworld`, `elevenlabs`, `openai`, or another model such as `minimax`, `gemini`, `chatterbox-turbo`, `kokoro`. `default` uses `tts.provider` from `server/config.json` |
| `providers` | List every provider the sidecar knows and whether its key is set |
| `voices` / `voices sync` | How many voices the active provider can cast from / refresh that list (ElevenLabs account, model voice list, voice bank) |
| `deny <name\|t\|last>` / `allow <...>` / `deny list` | Silence specific NPCs. `t` is your target, `last` is whoever spoke last. Names accept `*` wildcards, e.g. `deny Moogle*` |
| `voice <name\|t\|last>` | Show the voice an NPC will get and why |
| `voice <name> key=value ...` | Override it. Keys: `gender` `age` `species` `personality` `tone` `accent` `pitch` `pace` `voice` `direction`. Use `_` for spaces: `//koto voice t age=elderly tone=gruff_and_weary` |
| `voice <name> reset` / `reinfer` / `promote` | Clear your overrides / ask the casting model again / save the effective profile to the shared library |
| `cutscene` / `cutscene forget` | Mark the NPC event you're in (or just finished) as a cutscene, remembered for next time, or undo that |
| `status` | Link, mode, event state, cache and estimated cost |
| `test [event\|cutscene\|ambient\|battle] <Name> : <text>` | Push a line through the whole pipeline |
| `debug on/off`, `inspect [t]` | Write `data/debug.log`; dump the target's fields to `data/inspect.log` |

Allowed values: `gender` male/female/neutral; `age` child/young/adult/middle-aged/elderly; `pitch` very low/low/medium/high/very high; `pace` slow/measured/normal/brisk/fast; `voice` one of the active provider's voices: for OpenAI one of alloy, ash, ballad, coral, echo, fable, nova, onyx, sage, shimmer, verse, marin, cedar; for ElevenLabs any voice name on your account, e.g. `voice=George`; for a model, any name from `kotodama_server.py voices list --provider <model>`, e.g. `voice=English_ManWithDeepVoice`.

Addon settings (mode, toggles, volume, deny list) are saved in `data/settings.xml`.

## Voice providers

The default voice provider is **Inworld TTS-2** (`inworld`, see **[Inworld](#inworld)** below). Kotodama can also speak through **ElevenLabs**, **OpenAI**, and the models in the sections that follow. Switch any time in game with `//koto provider elevenlabs` / `//koto provider inworld`; the choice is remembered and restored when the sidecar restarts. Each provider keeps its own cached audio, so switching back and forth never re-synthesizes anything. To change the default, set `"tts": {"provider": "elevenlabs"}` (or any provider name) in `server/config.json`.

To add a provider, put its key in **[the key file](#the-key-file)** and press R in the sidecar terminal.

### OpenAI

**OpenAI** has 13 fixed voices and takes free-text acting directions, so an NPC's age, personality and tone are passed straight through.

**OpenAI's speech models shut down on 2027-01-06.** Every model on OpenAI's `/v1/audio/speech` endpoint (`gpt-4o-mini-tts`, `tts-1`, `tts-1-hd`) stops working that day, and their replacement only speaks over the Realtime API. That's why OpenAI is no longer the default. Until then, `//koto provider openai` or `"tts": {"provider": "openai"}` in `server/config.json` still uses it. Kotodama warns when you choose it, `//koto providers` and `providers` mark it, and `doctor` prints the date. A self-hosted server with an OpenAI-style endpoint (see **[Self-hosted and other OpenAI-compatible servers](#self-hosted-and-other-openai-compatible-servers)**) isn't affected.

### ElevenLabs

**ElevenLabs** voices belong to your account (the stock voices plus any you add from their library, design or clone), and there are no free-text directions. So:

- **Voices are auto-cast.** The sidecar fetches every voice your account can use and scores each against the NPC's profile: gender, age, and descriptive words from its tone, personality and pitch (a Galka's "deep, gravelly" against a voice described as deep and gravelly, a Tarutaru's "bright, bubbly" against bright and youthful voices). Among the best fits, the NPC's name picks one, so the same NPC always gets the same voice. Once cast, an NPC keeps its voice even if you add voices later; changing its profile (an override, a new LLM description) re-casts it. `//koto voice <npc> reset` re-casts by hand.
- **Personality goes into the delivery:** speed follows the profile's pace, stability follows temperament (lower for excitable characters, higher for calm ones), and on the v4 models one leading audio tag such as `[excited]` or `[sarcastic]` is added when the profile calls for it. ElevenLabs warns a tag is occasionally read aloud; if you hear one, set `"elevenlabs": {"audio_tags": false}`.
- **Model:** `eleven_v4_turbo` by default (their recommendation for real-time use). To use a different one, set `elevenlabs.model`.

Setting up ElevenLabs:

```
python3 server/kotodama_server.py voices sync                    # fetch your account's voices
python3 server/kotodama_server.py voices cast                    # preview which voice each race/creature gets
python3 server/kotodama_server.py voices cast --npc Prishe --race 4
python3 server/kotodama_server.py doctor --provider elevenlabs   # confirms the model works (uses a few credits)
python3 server/kotodama_server.py test --provider elevenlabs --name Rok --race 8 "Hm."
```

To keep a voice out of casting, add its name or id to `elevenlabs.voices.exclude`. The voice list refreshes itself daily (`elevenlabs.voices.refresh_hours`).

### Inworld

`inworld`, the default provider, and `inworld-flash` speak through Inworld's Realtime TTS-2 models, which **stream** raw audio like MiniMax and Gemini. They use Inworld's OpenAI-compatible endpoint, so they share Kotodama's OpenAI-compatible code.

- **`inworld`** (TTS-2) takes **steering**: each line carries a plain direction built from the NPC's tone, personality and pace, such as "speak in a deep gravelly and rumbling tone with a stoic manner at a slow pace". Age and gender come from the voice.
- **`inworld-flash`** (TTS-2 Flash) is the fastest and cheapest, but it ignores steering, so none is sent.
- **Voices come from Inworld's library**, labelled with gender, age group and language, so a Galka gets an older, deeper voice and a Tarutaru a young, bright one. Voices you clone or design in the Inworld portal are included automatically.

To check it:

```
python3 server/kotodama_server.py doctor                      # key, how many voices you can cast from
python3 server/kotodama_server.py doctor --provider inworld   # time to first audio (one short line)
python3 server/kotodama_server.py voices cast --provider inworld
```

- The voice list refreshes daily (`//koto voices sync` forces it). Other languages' library voices are left out; widen that with `"inworld": {"voices": {"languages": ["en", "ja"]}}`.
- `//koto status` estimates cost at Inworld's pay-as-you-go price per character ($25 per million for TTS-2, $15 for Flash). Plans are cheaper; set your own rate with `"models": {"inworld": {"price": {"per_1k_chars": 0.02}}}`.
- Square brackets in game text are spoken as parentheses, because Inworld treats `[...]` as a hidden instruction.
- On a regional (Enterprise) plan, set `inworld.base_url` to `https://api.eu.inworld.ai` or `https://api.in.inworld.ai`.

### MiniMax

`minimax` and `minimax-hd` speak through MiniMax's own API, which **streams**: audio starts playing a fraction of a second after the line appears, much like OpenAI and ElevenLabs. They use MiniMax Speech 2.8 Turbo and HD, with 62 labelled English voices and real pitch control (Galka lower, Tarutaru higher).

Put `MINIMAX_API_KEY=...` in the key file, press R in the sidecar terminal, then `//koto provider minimax`. To check it first:

```
python3 server/kotodama_server.py doctor                      # key, voice count
python3 server/kotodama_server.py doctor --provider minimax   # time to first audio (one short line)
python3 server/kotodama_server.py voices cast --provider minimax
```

- **Your own voices are included.** Voices you've cloned or designed on your MiniMax account appear in casting automatically (refreshed daily, or with `//koto voices sync`), as do any English system voices beyond the built-in list.
- **Emotion** is left to MiniMax, which picks one per line. To drive it from each NPC's temperament instead, add `"models": {"minimax": {"input": {"voice_setting": {"emotion": "{emotion}"}}}}` to `server/config.json`.
- **Region:** the default host is `api.minimax.io`. Set `minimax.base_url` for another region's host, and `minimax.group_id` only if your account still needs one.
- The same models are also available through Replicate as `minimax-replicate` and `minimax-replicate-hd` (`REPLICATE_API_TOKEN`). Those were called `minimax` and `minimax-hd` before; lines already cached under the old names still play from the cache.

### Gemini

`gemini` and `gemini-lite` speak through Google's Gemini 3.8 Flash TTS models, which **stream** like MiniMax. Two things set them apart:

- **Delivery comes from the NPC's character.** Each line is sent with a short direction built from the NPC's tone, personality and pace, such as "Tone: deep, gravelly and rumbling. Character: stoic. Pace: slow." Age, gender and accent come from the voice, as Google recommends.
- **A large voice library.** Kotodama casts from Google's whole English voice library, not only the 30 classic voices (Kore, Puck, Charon...). Google labels each voice with gender, pitch and accent, so casting has a lot to work with. Voices you design or clone in Google AI Studio are included automatically.

Get a key at aistudio.google.com, put `GEMINI_API_KEY=...` in the key file, press R in the sidecar terminal, then `//koto provider gemini`. To check it first:

```
python3 server/kotodama_server.py doctor                     # key, how many voices you can cast from
python3 server/kotodama_server.py doctor --provider gemini   # time to first audio (one short line)
python3 server/kotodama_server.py voices cast --provider gemini
```

- `gemini-lite` uses Flash-Lite: cheaper, slightly less expressive.
- The voice list refreshes daily (`//koto voices sync` forces it). Other languages' voices are left out; widen that with `"gemini": {"voices": {"languages": ["en", "ja"]}}`.
- Google bills by audio tokens; `//koto status` shows them per day. Add `"models": {"gemini": {"price": {"per_1m_output_tokens": 0.0}}}` with Google's current price for a dollar estimate.
- Tags like `<laugh>` or `<sigh>` are sound effects in Gemini, so Kotodama never adds them.

### Replicate and other backends

Beyond OpenAI and ElevenLabs, Kotodama can speak through text-to-speech models hosted on [Replicate](https://replicate.com) and through any server that speaks OpenAI's `/v1/audio/speech` API. Each model is described by a small **adapter** (in `server/tts_models.json`), and each adapter is a provider you can switch to in game: `//koto provider chatterbox-turbo`. Voices are auto-cast the same way as with ElevenLabs.

Put `REPLICATE_API_TOKEN=r8_...` in the key file, then:

```
python3 server/kotodama_server.py providers                    # what's available
python3 server/kotodama_server.py doctor                       # token, and each model's inputs still match
python3 server/kotodama_server.py voices cast --provider kokoro
python3 server/kotodama_server.py test --provider minimax-replicate --name Rok --race 8 "Hm."
```

Shipped models:

| Provider | Model | Good for |
|---|---|---|
| `minimax-replicate` | `minimax/speech-2.8-turbo` | MiniMax through Replicate: 62 labelled English voices, real pitch control. With a MiniMax key, `minimax` (above) is the same model and streams |
| `minimax-replicate-hd` | `minimax/speech-2.8-hd` | The same voices at higher quality, a little slower |
| `chatterbox-turbo` | `resemble-ai/chatterbox-turbo` | Fast open-source model; preset voices plus cloning from your voice bank |
| `chatterbox` | `resemble-ai/chatterbox` | Expressive open-source model with emotion intensity; voices come only from your voice bank |
| `kokoro` | `jaaari/kokoro-82m` | Small, cheap open-source model with 26 English voices (a community model, so it can be slow to wake up) |
| `qwen3` | `qwen/qwen3-tts` | Takes acting directions (tone, character, pace) as text; preset speakers plus cloning from the voice bank |

**Latency.** Replicate returns finished audio files, not a stream. So each line is split at sentence boundaries: the first sentence is made on its own and starts playing while the rest is made in parallel. Expect the first word a second or two after the line appears, depending on the model. Official models (everything above except Kokoro) are kept warm by Replicate; a community model that hasn't run lately can take a minute to start, and the sidecar says so in chat. Lines are cached like any other provider's, so repeats are instant and free.

**Cost.** Replicate bills your account per run; check each model's page for its price. `//koto status` shows characters and compute seconds per model. Add `"price": {"per_1k_chars": 0.0}` or `{"per_second": 0.0}` to a model in `server/config.json` to get a dollar estimate too.

### The voice bank (for cloning models)

Original Chatterbox has no preset voices, and Chatterbox Turbo and Qwen3 can also clone. They take a short reference clip of the voice to use. Kotodama keeps these clips in `voices/` (not committed) and casts NPCs to them by their labels:

- **Make some automatically:** `python3 server/kotodama_server.py voices design` generates labelled clips for every race and creature with Qwen3-TTS voice design (3 variants each by default, about 70 predictions; it asks before spending). `--only galka,taru_m` and `--variants 2` narrow it down. Re-running only makes what's missing.
- **Add your own:** drop a 5.5–7.5 second WAV of clean speech in `voices/`, optionally with a same-named `.json` holding `{"name", "gender", "age", "description", "reference_text"}` (the transcript helps Qwen3). Without one, labels are guessed from the file name (`old_man_gruff.wav` = old, male, gruff). Longer clips are trimmed.
- `python3 server/kotodama_server.py voices bank` lists what's there and why anything was skipped. In game, `//koto voices sync` picks up changes.

### Adding another model

Any Replicate TTS model can be added without code. Ask the sidecar to read its inputs and draft an adapter:

```
python3 server/kotodama_server.py replicate schema zsxkib/dia
```

Paste the printed adapter into `server/config.json` under `"models"`, adjust it, and press R in the sidecar terminal. An adapter maps the model's input fields to placeholders filled from each NPC's profile:

```json
"models": {
  "my-model": {
    "model": "owner/name", "official": false,
    "input": {"text": "{text}", "voice": "{voice}", "speed": "{speed}", "seed": "{seed}"},
    "voices": ["schema", "bank"]
  }
}
```

Placeholders:
- `{text}` and `{voice}`;
- `{speed}`, from pace;
- `{pitch}`, through the adapter's `controls.pitch` table;
- `{emotion}`;
- `{exaggeration}` and `{cfg_weight}`, from temperament, through `controls`;
- `{style}`, a short acting direction;
- `{voice_description}`;
- `{seed}`, stable per NPC;
- `{reference_audio}` and `{reference_text}`, from the voice bank.

`input_clone` holds fields that change when the voice is a bank clip (`null` removes a field). `voices` can list voices, name a set from `tts_models.json` (`"@kokoro_en"`), and include `"schema"` (the model's own voice list) and `"bank"`. To change a shipped model, override only what differs, e.g. `"kokoro": {"controls": {"speed": {"normal": 1.1}}}`; `"enabled": false` hides one.

### Self-hosted and other OpenAI-compatible servers

Servers such as Kokoro-FastAPI, Chatterbox TTS servers, or hosted services with an OpenAI-style speech endpoint stream audio directly, so they're the lowest-latency option if you have one. An example is included, disabled:

```json
"models": {
  "kokoro-local": {"enabled": true, "base_url": "http://192.168.1.20:8880/v1"}
}
```

Fields: `transport: "openai_compatible"`, `base_url` (including `/v1`), `model`, `voices`, `response_format` (`pcm` or `wav`), an optional `api_key_env`, and `extra` for any additional request fields (same placeholders). A rate limit (429) is retried once before any audio plays, a voice the server says it doesn't have (404) is dropped from casting, and lines over `max_chars` (default 1000) are sent in parts. For a hosted service, add `"price": {"per_1k_chars": ...}` to get a cost estimate in `//koto status`.

## How voices are chosen

Highest priority wins, field by field:

1. **Your overrides** (`config/overrides.json`, written by `//koto voice`)
2. **The NPC library**: curated entries, then community packs, then entries learned by casting
3. **A stable per-name variation** within the archetype, so the same NPC always sounds the same, on every machine
4. **The archetype**: from the speaker's race (Hume, Elvaan, Tarutaru, Mithra, Galka, children), creature type, or name keywords (Moogle, Goblin, Yagudo, Quadav...). Edit `server/archetypes.json` to change these.

Keys can be zone-specific (`"Name@<zone id>"`) for NPCs who change character between zones, and `@player` / `@narrator` set the voice for those roles. Edit `config/overrides.json` directly for these.

Each profile is turned into acting direction for the TTS model ("Voice: elderly female Hume. Personality: ... Tone: ... Pace: ...") and is part of the cache key, so changing a profile gives fresh audio while old lines stay cached.

### Optional voice casting (LLM)

By default (`inference.provider: "none"`) voices come from archetypes plus your overrides. If you set `inference.provider` to `anthropic` or `openai` in `server/config.json`, the first time an unknown NPC speaks (or you target one, or start an event with them) the model is asked once to describe how they should sound. The answer is validated against a strict schema and saved to `library/npcs.json`; it's never requested again for that NPC. The first line waits up to `wait_ms` (2.5 s) for the answer, then falls back to the archetype voice so dialog is never delayed much. NPCs the game gives no race for (story characters with a fixed "precomposed" model) wait up to `unknown_wait_ms` (8 s) instead: their only stand-in is a genderless voice, and the casting would replace it from their second line on.

- For `anthropic`, install the SDK in a virtual environment.
  - Linux: `python3 -m venv server/.venv && server/.venv/bin/pip install anthropic`, then run the sidecar with that Python.
  - Windows: `py -3 -m venv server\.venv`, then `server\.venv\Scripts\pip install anthropic`. `kotodama.cmd` uses `server\.venv` automatically when it exists.
- Casting is throttled (`max_per_minute`) and a failed NPC is not retried for ten minutes.

### Sharing a library

`library/npcs.json` is plain, sorted, diff-friendly JSON with no player names in it.

```
python3 server/kotodama_server.py library export -o my-npcs.json --source llm --source curated
python3 server/kotodama_server.py library import friends-npcs.json    # installs read-only into library/packs/
python3 server/kotodama_server.py library list
```

Imported packs beat your learned (LLM) entries but not your curated ones, and your overrides always win.

## Loudness and ducking

**Loudness.** Voices come out of the TTS models at very different levels: one voice can sit 15–20 dB below another, while the lines of any one voice stay within a dB or two of each other. So the sidecar measures every line (in LUFS, the broadcast loudness scale) and gives each *voice* one steady gain that brings its typical line to `audio.normalize.target_lufs` (default −13). A voice is its provider, model and voice, so lines styled to be whispered or shouted are still the same voice: a whisper stays a whisper, a shout stays a shout, and the rise and fall within a line is untouched. `//koto volume` scales on top: 100 is the normalized baseline, 200 is twice as loud again, up to 400. A peak limiter lowers its gain on loud passages instead of clipping them.

- **A voice's level** is the median of its last 20 lines that are a second or longer (`voice_window`), so the odd whisper or shout doesn't move it. Boosts stop at +15 dB and cuts at −10 dB (`max_boost_db`, `max_cut_db`).
- **A voice heard for the first time** starts from the typical level of other voices on the same model (or, with nothing measured yet, the fixed `audio.makeup_db` boost of +9 dB). During that first line its gain settles toward the target, at most 2 dB per second and within 6 dB of where it started (`first_line_adapt`). From its second line on the gain is steady. This adds no delay. If you'd rather a new voice's first line start at the right level, `lookahead_ms` (e.g. 500) holds back that much of the first line to measure it first; this delays only first lines of new voices, by exactly that amount.
- **Lines that sag partway through** can be lifted by the leveler (`leveler.enabled`, off by default). It compares the last 3 seconds of speech with the line's opening, ignores a sag of up to 2 dB, lifts at most `leveler.max_db` (6 dB) at 2 dB per second, never turns anything down, and holds still through pauses. It also lifts a line that trails off on purpose, which is why it's off. `leveler.providers` limits it to some providers (empty: all of them).
- **The cache keeps the unscaled audio** with each line's measured level, so changing any of this never invalidates the cache. Lines cached before this feature are measured the first time they play, or all at once with `python3 server/kotodama_server.py cache analyze`.
- **Turning it off:** `//koto norm off`, or `audio.normalize.enabled: false` in `server/config.json`. Every line then gets the fixed `audio.makeup_db` boost as before. `//koto norm default` goes back to what the config file says.
- **Checking it:** `//koto voice <name>` shows a voice's measured level and its gain, e.g. `loudness: -24.0 LUFS over 12 line(s) -> +11.0 dB`. With `//koto debug on` the sidecar log shows how each line's gain was picked.

If voices are still too quiet next to the music, raise `volume` or `audio.normalize.target_lufs` (−10 is loud), or lower the game's own volume.

```json
"audio": {
  "makeup_db": 9.0,
  "normalize": {
    "enabled": true, "target_lufs": -13, "max_boost_db": 15, "max_cut_db": 10,
    "voice_window": 20, "first_line_adapt": true, "lookahead_ms": 0,
    "leveler": {"enabled": false, "max_db": 6, "providers": []}
  }
}
```

**Ducking.** While a line is spoken the sidecar lowers the game's own audio stream (music and effects together, since the game outputs one stream) to `duck.level` percent, quickly at the start of a line and gradually back afterwards. It holds the lowered level for `hold_ms` between lines so a cutscene doesn't pump the music up and down. This works at the PipeWire/PulseAudio level using `pactl`, so it needs no game or Windower support.

On Windows (experimental) it lowers FFXI's per-app volume instead, the same slider as in the Volume Mixer, through Windows' own audio API, so nothing has to be installed. FFXI's audio belongs to `pol.exe`, which the default `duck.match` includes. Apps on every output device are found, so it works with headphones too. `server\kotodama.cmd audio list` shows each app's audio by process id.

- Which stream is "the game" is decided by `duck.match`: any entry appearing in a stream's application name, binary or Flatpak app id. The defaults match `windower`, `playonline`, `ffxi` and `pol.exe`. Run `python3 server/kotodama_server.py audio list` with the game running to see what's matched: the game's stream is marked `DUCK`.
- `audio duck-test` dips the game volume for three seconds and restores it, so you can confirm it works without the addon.
- The original volume is saved to `cache/duck_state.json` while ducked. If the sidecar is killed mid-duck, the next start restores it automatically, or run `audio restore`.
- Windows remembers each app's volume, so a crash mid-duck would leave FFXI quiet even after restarting the game. The next start finds FFXI by its app, not only by the session it ducked, and restores it. Closing the sidecar's window, logging off or shutting down restores it before the sidecar exits.
- Our own player streams (`pw-play`, `paplay`, `ffplay`, and on Windows the sidecar's own audio) are never ducked.

## Cache and cost

Every synthesized line is stored in `cache/` (default cap 2 GB, least-recently-played evicted), per provider. Hearing the same line again costs nothing. If you advance past a line before it finishes, the audio is still downloaded into the cache because it's already paid for (`cache.finish_download_on_stop`). `//koto status` shows today's counts with a rough estimate: dollars at list price for OpenAI, characters and estimated credits for ElevenLabs (`elevenlabs.credits_per_char`, default 0.5 for Flash models and 1 otherwise). Check your plan for the real figures. `python3 server/kotodama_server.py cache stats` works without the game; `cache analyze` measures the loudness of lines cached before loudness normalization existed (`--all` re-measures everything).

## Cutscenes

FFXI doesn't flag cutscenes, so Kotodama works them out from what the game sends. Once an event is recognised as a cutscene, its zone and menu id are saved to `library/cutscenes.json`, and every later viewing is a cutscene from its first line. The file ships with a few confirmed entries, and it's plain sorted JSON you can share.

A cutscene that opens with several lines from the NPC you talked to looks like ordinary dialog until someone else speaks. In `mode cutscenes`, the first viewing of such a scene can therefore lose its opening lines. If you spot a cutscene that Kotodama missed, type `//koto cutscene` during it (or just after) so it's recognised next time. `//koto cutscene forget` undoes that.

# Developers

## How it works

Kotodama has two parts:

- **The addon** (`Kotodama.lua`, `lib/`) runs inside Windower. It only listens: it captures dialog lines, works out who is speaking and whether it's a cutscene, and decides whether to voice the line.
- **The sidecar** (`server/`) is a small Python program that runs on your machine outside the game. It holds your API keys, picks the voice, calls the text-to-speech service, caches the audio, and plays it. It is separate because Windower can't stream or stop audio, and a network call inside an addon would freeze the game.

The two talk over a local UDP socket on `127.0.0.1:47123`. Nothing is exposed beyond your own machine.

## How cutscenes are detected

`lib/scene.lua` infers cutscenes from packet flow and from the dialog itself. Inside an NPC event, it's a cutscene if you didn't start it, a second speaker joins, the speaker isn't the NPC you talked to, your own character speaks, or lines auto-advance. Outside an event:

- A line right after an NPC-chat packet (`0x036`) is a one-liner such as a shop greeting, and stays **ambient** even though it auto-advances.
- A line that straggles in just after an event was released stays with that event.
- Any other speech is a scene the server drove without an event packet, such as a zone-in cutscene, and counts as a **cutscene**.

**The known gap:** a cutscene that opens with several lines from the NPC you talked to looks exactly like ordinary dialog until someone else speaks (the opening of the Fochacha scene is one). To close it, the first time an event is recognised as a cutscene by a solid signal, its zone and menu id are saved to `library/cutscenes.json`, and every later viewing is a cutscene from its first line.

With `//koto debug on`, `data/debug.log` records every decision with its reason, the raw bytes of each event-start packet, the player's status, and a count of the packets that flew by during each event or untracked scene. If you find a scene that's misclassified, that log is what to look at, and the packet counts are the place to look for a real start-of-cutscene signal.

`python3 tests/lua/replay_log.py data/debug.log` replays a recorded log through the current rules and lists every line whose classification changed, which is the way to check a tweak against your real sessions.

## Creature models

Non-player creatures and some fixed-model townsfolk carry a model id instead of a race (`race=0`). Map them to a creature or race archetype in `data/creature_models.lua` (Moogles are model 82, the Whitegate Mithra child is 182), or use name keywords and overrides. `//koto inspect t` dumps the target's fields, including `race` and `models`, to `data/inspect.log`.

## Tests

- **Sidecar:** `cd server && python3 -m unittest discover -s tests` (`py -3` instead of `python3` on Windows).
- **Lua:** the addon runs under Lua 5.1 with a stubbed Windower via the optional `lupa` package:
  ```
  python3 -m venv .venv-lua && .venv-lua/bin/pip install lupa && .venv-lua/bin/python -m unittest discover -s tests/lua
  ```

# Credits

Dialog markup handling is derived from the Balloon addon (Hando, Yuki, Ghosty; BSD-3-Clause).
