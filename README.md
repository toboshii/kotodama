# Kotodama

Kotodama is an AI voiceover for Final Fantasy XI. When NPC dialog appears in your chat log, Kotodama speaks it aloud. This includes quest dialog and cutscenes. Each kind of character gets a different voice.

Kotodama has two parts: a Windower addon and a small Python program, the **sidecar**. The sidecar runs next to the game and does the speaking.

This README has three parts:

- [Getting started](#getting-started): what you need to hear voices in game.
- [Power users](#power-users): all commands, the other voice providers, voice casting, loudness and cost.
- [Developers](#developers): how Kotodama works inside, and how to run the tests.

# Getting started

## What you need

- Final Fantasy XI with Windower 4.
- Python 3.10 or newer.
- On Linux and the Steam Deck, an audio player: `pw-play`, `paplay` or `ffplay`. One of them is probably installed already. Windows needs nothing more.
- An API key for **Inworld**, the default voice provider. Create a key at platform.inworld.ai (API Keys) and copy the **Base64** credential. To use a different provider, see [Voice providers](#voice-providers).

## Install

1. Download `Kotodama-vX.Y.Z.zip` from the **Releases** page of the project on GitHub.
2. Open the zip. It holds one folder, `Kotodama`. Drag that folder into the Windower `addons` folder. The result is `Windower/addons/Kotodama/Kotodama.lua`.

The rest of this guide calls `Windower/addons/Kotodama` the **addon folder**.

**To upgrade,** drag the new `Kotodama` folder over the old one and let it replace the files. The zip does not contain your settings, keys, cache or the voices that Kotodama learned, so the upgrade keeps them. After the upgrade, restart the sidecar and reload the addon (`//lua reload kotodama`).

## What gets sent where

- **Speech.** The sidecar sends the dialog text to your voice provider, which turns it into speech. The provider can be OpenAI, ElevenLabs, MiniMax, Google, Inworld, a model on Replicate, or a TTS server that you choose.
- **Voice cloning.** With a voice-cloning model, the sidecar uploads a reference clip from your voice bank to Replicate with each line.
- **Voice casting.** Casting is optional and off by default. If you turn it on, the sidecar sends the NPC's name, race and zone and up to three sample lines to your casting provider.

**Kotodama replaces your character's name with `{player}` before it stores anything in a shareable file or sends anything for casting.** Speech requests contain the line as it is, so your name is in it if the NPC says it. The provider must have the real text to speak it. The same is true for your own [voice lines](#voice-lines-and-macros): Kotodama puts real names in place of `<me>` and `<t>` before it sends the line.

## Linux and the Steam Deck

1. **Give Kotodama your key.** Put this line in `~/.config/kotodama.env`:
   ```
   INWORLD_API_KEY=...
   ```
   Then make the file private: `chmod 600 ~/.config/kotodama.env`.
2. **Check the setup.** In the addon folder, run:
   ```
   server/kotodama.sh doctor
   ```
   `doctor` checks your key, the voices, audio playback and the network port. Then it tells you what to fix.
3. **Hear a voice without the game:**
   ```
   server/kotodama.sh test --name Ayame --race 2 "Welcome to Norg."
   ```
4. **Start the sidecar** from a normal host terminal:
   ```
   server/kotodama.sh
   ```
   Keep it open while you play. Press **R** to restart it or **Q** to quit. You do not need Enter or Ctrl+C, which helps with the on-screen keyboard of the Steam Deck.

   Do not start the sidecar from a sandboxed terminal, such as the terminal of a Flatpak editor. The sidecar needs your real audio session and the loopback of the host. To start it automatically when you log in, see [Running the sidecar](#running-the-sidecar).

## Windows (experimental)

If something does not work on Windows, run `server\kotodama.cmd doctor` first. Its output is the most useful thing to report.

1. **Install Python 3.10 or newer** from [python.org](https://www.python.org/downloads/). In the installer, keep "py launcher" selected.
2. **Give Kotodama your key.** Create the folder `%APPDATA%\Kotodama`. To find `AppData\Roaming`, paste that path into the address bar of Explorer. In the folder, create a text file named `kotodama.env` with this line:
   ```
   INWORLD_API_KEY=...
   ```
   Make sure that Notepad did not save it as `kotodama.env.txt`.
3. **Check the setup.** Open a Command Prompt in the addon folder and run:
   ```
   server\kotodama.cmd doctor
   ```
   `doctor` also checks that the sidecar can save its cache and library. The sidecar cannot write to a Windower installed under `Program Files`. Install Windower in a different folder, or set `cache.dir` and `library.file` in `server\config.json` to a folder that you own.
4. **Hear a voice without the game:**
   ```
   server\kotodama.cmd test --name Ayame --race 2 "Welcome to Norg."
   ```
5. **Start the sidecar:** double-click `server\kotodama.cmd`. Keep its window open while you play. Press **R** to restart it or **Q** to quit. If you close the window, the sidecar also stops.

## Load the addon

In game, type `//lua load kotodama`. To load the addon automatically, add `lua load kotodama` to your Windower `scripts/init.txt`.

If the sidecar is not running, you get one warning in chat. The game works as usual.

## Everyday commands

All commands start with `//koto` (or `//kotodama`).

| Command | What it does |
|---|---|
| `on` / `off` | Turn voiceover on or off |
| `mode all` / `events` | **all**: every NPC line. **events**: only NPC events (quest dialog and cutscenes), not one-line greetings |
| `volume <0-400>` | Voice loudness in percent. 100 is normal |
| `duck <0-100>` | How much of the game audio stays while a line plays (default 40) |
| `stop`, `replay` | Stop the current line, or play the last one again |
| `deny t` / `allow t` | Silence your target, or undo that |
| `say <text>` / `line <name>` | Speak your own line, or a line that you saved (see [Voice lines and macros](#voice-lines-and-macros)) |
| `status` | Make sure that the game is connected to the sidecar (`link UP`) |

Kotodama saves your settings and loads them again next time. For all commands, see [All in-game commands](#all-in-game-commands).

## If something does not work

- Run `doctor` (step 2 above) and fix what it reports.
- `//koto status` must show `link UP`. If it does not, make sure that the sidecar is running.
- If the voices are too quiet next to the music, increase `//koto volume` (try 200), or decrease the volume of the game.

# Power users

## The key file

The key file is `~/.config/kotodama.env` on Linux and `%APPDATA%\Kotodama\kotodama.env` on Windows. It holds one key per line. Kotodama reads these keys:

```
INWORLD_API_KEY=...
ELEVENLABS_API_KEY=...
MINIMAX_API_KEY=...
GEMINI_API_KEY=...
OPENAI_API_KEY=sk-...
REPLICATE_API_TOKEN=r8_...
```

- The sidecar reads this file itself. This is true when you start it from a terminal and when you start it with the systemd unit.
- If a variable is already set in your shell, the shell value wins over the file.
- When you press R in the sidecar terminal, the sidecar reads the file again. Thus your edits take effect without a restart of the terminal.
- If the addon folder has a `server/kotodama.env`, that file wins over the key file. Use it to keep keys with a portable install or a test setup. To start one, copy `server/kotodama.example.env`. On Linux, `chmod 600` it. Git ignores this file.
- To use a different file, pass `--env-file PATH` or set `KOTODAMA_ENV_FILE`. Each of these wins over the two files.
- The file can also set `KOTODAMA_CONFIG`, to keep your `config.json` outside the addon folder.

## Running the sidecar

**Restart.** Press **R** in the sidecar terminal to restart the sidecar. The restart loads changes to the code and the config. The game stays connected: the addon sees the restart, says "voice server restarted." and sends your settings (volume, provider, ducking) again. On Windows, the sidecar ignores other keys, such as Insert, Page Down and the F-keys.

**Automatic start on Linux.** Install the systemd *user* unit. Then the sidecar runs whenever you are logged in:

```
cp server/kotodama.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now kotodama
```

Under systemd there is no terminal. To restart the sidecar, use `systemctl --user restart kotodama`.

**Automatic start on Windows.** Press Win+R and enter `shell:startup`. Put a shortcut to `kotodama.cmd` in the folder that opens.

**More checks:**

```
server/kotodama.sh doctor --live               # also speak one short line with every provider that has a key
server/kotodama.sh doctor --provider inworld   # ...or with just one
server/kotodama.sh test --name Ayame --race 2 "Welcome to Norg."   # run twice: the second comes from cache, no API call
server/kotodama.sh test --role custom "For the Crystal!"   # a //koto say line, in the custom voice
```

The live test runs all providers at the same time. It shows the time to first audio for each provider, and at the end it ranks them from fastest to slowest. It does not play or cache audio. Each provider costs a fraction of a cent. A cold Replicate model can need a minute.

On Windows, use `server\kotodama.cmd` in place of `server/kotodama.sh` in all commands in this README. You do not need Python from the Microsoft Store. `kotodama.cmd` skips the `python` placeholder of the Store.

## All in-game commands

`//koto` (or `//kotodama`)

| Command | What it does |
|---|---|
| `on` / `off` | Turn voiceover on or off |
| `mode all` / `events` | **all**: every NPC line. **events**: only NPC events (quest dialog and cutscenes, not one-line ambient chatter). See [Choosing what is spoken](#choosing-what-is-spoken) |
| `battle on/off` | Speak battle chatter, such as Trusts (default off) |
| `own on/off` | Speak the lines of your own character (default on) |
| `narrator on/off` | Speak lines that have no speaker name (default off). A character that the game did not introduce yet shows as `???`. This is not narration: Kotodama always speaks these lines. The character gets one voice for the whole scene, under a name such as `??? (243:10191)` (zone and event). If the game has exactly one `???` with a body nearby, the race comes from that actor in the scene. When the game introduces the character, the character keeps that voice. Kotodama knows it is the same character from the same actor or look, or because the scene has only one `???` while the name is said. To change the voice, use `//koto voice last` |
| `interrupt on/off` | On: a new line stops the previous one. Off: lines wait in a queue |
| `prefetch on/off` | When you target or talk to an NPC, open the connection and cast the voice early, so the first line starts sooner (default on). If the game does not report the race of an NPC, Kotodama casts it from its first line |
| `volume <0-400>` | Voice loudness in percent. 100 is normal. If the voice is quieter than the game audio, go higher (see [Loudness and ducking](#loudness-and-ducking)) |
| `duck on/off`, `duck <0-100>` | Make the game audio quieter while a line plays. The number is how much of it stays (default 40) |
| `norm on/off/default` | Make the loudness of different voices more equal (see [Loudness and ducking](#loudness-and-ducking)). `default` uses `audio.normalize.enabled` in `server/config.json` (on unless you change it) |
| `stop`, `replay` | Playback controls |
| `provider <name>` / `provider default` | Change the voice provider at once: `inworld`, `elevenlabs`, `openai`, or a model such as `minimax`, `gemini`, `chatterbox-turbo`, `kokoro`. `default` uses `tts.provider` from `server/config.json` |
| `providers` | List all providers that the sidecar knows, and show if each key is set |
| `voices` / `voices sync` | Show how many voices the active provider can cast from / refresh that list (ElevenLabs account, model voice list, voice bank) |
| `deny <name\|t\|last>` / `allow <...>` / `deny list` | Silence specific NPCs. `t` is your target. `last` is the last NPC that spoke. Names can have `*` wildcards, for example `deny Moogle*` |
| `voice <name\|t\|last>` | Show the voice that an NPC will get, and why |
| `voice <name> key=value ...` | Override that voice. Keys: `gender` `age` `species` `personality` `tone` `accent` `pitch` `pace` `voice` `direction`. Use `_` for spaces: `//koto voice t age=elderly tone=gruff_and_weary` |
| `voice <name> reset` / `reinfer` / `promote` | Clear your overrides / ask the casting model again / save the effective profile to the shared library |
| `say <text>` | Speak your own line in the custom voice. Kotodama fills in placeholders such as `<t>` and `<me>` (see [Voice lines and macros](#voice-lines-and-macros)) |
| `line <name>` / `line list` | Speak a saved line / list the saved lines |
| `line add <name> <text>` / `line del <name>` | Save a line (this replaces a line with the same name) / delete a line |
| `voice @custom [key=value ...\|reset]` | Show or change the voice for `say` and `line`. Until you change it, it is the narrator's voice |
| `status` | Link, mode, event state, cache and estimated cost |
| `test [event\|ambient\|battle] <Name> : <text>` | Send a line through the full pipeline |
| `debug on/off`, `inspect [t]` | Write `data/debug.log` / write the fields of the target to `data/inspect.log` |

Permitted values:

- `gender`: male, female, neutral.
- `age`: child, young, adult, middle-aged, elderly.
- `pitch`: very low, low, medium, high, very high.
- `pace`: slow, measured, normal, brisk, fast.
- `voice`: one of the voices of the active provider.
  - OpenAI: alloy, ash, ballad, coral, echo, fable, nova, onyx, sage, shimmer, verse, marin, cedar.
  - ElevenLabs: any voice name on your account, for example `voice=George`.
  - A model: any name from `server/kotodama.sh voices list --provider <model>`, for example `voice=English_ManWithDeepVoice`.

The addon saves its settings (mode, toggles, volume, deny list) in `data/settings.xml`.

## Voice lines and macros

`//koto say <text>` speaks any line that you type. `//koto line <name>` speaks a line that you saved. Use them for callouts and reminders. Examples are a battle cry when you use a weapon skill, or a cue to cast a buff again.

**In a macro,** FFXI cannot run `//` commands. Use `/console` instead, or `/con` to save space on the macro line:

```
/con koto say Weapon skill on <bt>!
/con koto line resleep
```

A macro line holds only about 60 characters. Thus, save longer lines first and then use them by name:

```
//koto line add resleep Sleep is about to wear off. Re-sleep the add before it wakes up.
//koto line list
//koto line del resleep
```

Names can use letters, digits, `_` and `-`. Kotodama keeps saved lines in `data/voicelines.json`. All your characters share them.

**Placeholders.** FFXI does not fill in `<t>` and the other placeholders in text sent through `/console`, so Kotodama does this. Kotodama fills them in when it speaks the line, not when it saves it. Thus `Go, <t>!` names your target at that moment.

| Placeholder | Becomes |
|---|---|
| `<me>` | Your character's name |
| `<t>` | Your current target |
| `<bt>` | Your battle target |
| `<pet>` | Your pet |
| `<ft>` | Your focus target |
| `<lastst>` | Your last sub-target |

Placeholders are not case-sensitive. If a placeholder has no value (no target for `<t>`, no pet for `<pet>`), Kotodama does not speak the line and says why in chat. It also refuses an unknown placeholder, and `<st>`, which needs you to select a target on screen. Thus you never hear "Go, !".

**Auto-translate.** Kotodama speaks auto-translate phrases, items and key items as their words. `I need {Healing}` says "I need Healing", and `{Bone Mittens}` says "Bone Mittens". Kotodama leaves all other kinds of auto-translate entry out of the spoken line. If a phrase sounds wrong, do these steps:

1. Turn on `//koto debug on`.
2. Speak the line again.
3. Find `custom line` in `data/debug.log`. It shows the raw bytes that the game sent.

**The voice.** Voice lines use the narrator's voice until you change it with `//koto voice @custom`. For example: `//koto voice @custom tone=rousing pace=brisk` or `//koto voice @custom voice=onyx`. Changes to `@custom` apply only to your lines, not to real narration. `//koto voice @custom reset` sets the narrator's voice again. `mode`, `narrator off` and the deny list do not affect your lines. But `//koto off` silences them, and `//koto stop` stops them.

**Cost.** Kotodama caches audio by its text and voice. Thus a line costs one request the first time. After that, it plays from the cache for free. Each different name that a placeholder fills in gets its own cache entry.

## Voice providers

The default voice provider is **Inworld TTS-2** (`inworld`, see [Inworld](#inworld) below). Kotodama can also speak through **ElevenLabs**, **OpenAI** and the models in the sections that follow.

- To change the provider in game, use `//koto provider elevenlabs` or `//koto provider inworld`. Kotodama remembers the choice and uses it again after the sidecar restarts.
- Each provider has its own cached audio. Thus, if you change between providers, nothing is synthesized again.
- To change the default, set `"tts": {"provider": "elevenlabs"}` (or a different provider name) in `server/config.json`.

To add a provider, put its key in [the key file](#the-key-file) and press R in the sidecar terminal.

### OpenAI

**OpenAI** has 13 fixed voices. It accepts acting directions as free text, so Kotodama sends the age, personality and tone of the NPC directly. `//koto status` estimates the cost at the OpenAI list price for the model in `openai.model` (`gpt-4o-mini-tts`, `tts-1` or `tts-1-hd`). Prices for other models go in `openai.prices`.

**The OpenAI speech models shut down on 2027-01-06.** On that day, all models on the OpenAI `/v1/audio/speech` endpoint (`gpt-4o-mini-tts`, `tts-1`, `tts-1-hd`) stop. Their replacement speaks only over the Realtime API. Until then, you can use OpenAI with `//koto provider openai` or `"tts": {"provider": "openai"}` in `server/config.json`. Kotodama gives a warning when you select it. `//koto providers` and `providers` mark it, and `doctor` shows the date. This does not affect a self-hosted server with an OpenAI-style endpoint (see [Self-hosted and other OpenAI-compatible servers](#self-hosted-and-other-openai-compatible-servers)).

### ElevenLabs

**ElevenLabs** voices belong to your account. These are the stock voices plus any voices that you add from their library, design or clone. ElevenLabs takes no free-text directions. Thus Kotodama works like this:

- **Kotodama casts voices automatically.** The sidecar gets all voices that your account can use. It scores each voice against the profile of the NPC: gender, age, and descriptive words from the tone, personality and pitch. For example, a Galka's "deep, gravelly" matches a voice described as deep and gravelly. A Tarutaru's "bright, bubbly" matches bright and youthful voices. From the best matches, the NPC's name selects one. Thus the same NPC always gets the same voice.
- **An NPC keeps its voice after casting,** also if you add voices later. If its profile changes (an override, a new LLM description), Kotodama casts it again. To cast it again yourself, use `//koto voice <npc> reset`.
- **The personality goes into the delivery.** The speed follows the pace of the profile. The stability follows the temperament: lower for excitable characters, higher for calm ones. On the v4 models, Kotodama adds one leading audio tag such as `[excited]` or `[sarcastic]` when the profile needs it. ElevenLabs warns that it sometimes reads a tag aloud. If you hear one, set `"elevenlabs": {"audio_tags": false}`.
- **Model:** the default is `eleven_v4_turbo`, which ElevenLabs recommends for real-time use. To use a different model, set `elevenlabs.model`.

To set up ElevenLabs:

```
server/kotodama.sh voices sync                    # fetch your account's voices
server/kotodama.sh voices cast                    # preview which voice each race/creature gets
server/kotodama.sh voices cast --npc Prishe --race 4
server/kotodama.sh doctor --provider elevenlabs   # confirms the model works (uses a few credits)
server/kotodama.sh test --provider elevenlabs --name Rok --race 8 "Hm."
```

To keep a voice out of casting, add its name or id to `elevenlabs.voices.exclude`. The sidecar refreshes the voice list each day (`elevenlabs.voices.refresh_hours`).

### Inworld

`inworld` (the default provider) and `inworld-flash` use the Realtime TTS-2 models of Inworld. Like MiniMax and Gemini, they **stream** raw audio. They use the OpenAI-compatible endpoint of Inworld, so they share the OpenAI-compatible code of Kotodama.

- **`inworld`** (TTS-2) accepts **steering**. Each line has a plain direction made from the tone, personality and pace of the NPC. An example is "speak in a deep gravelly and rumbling tone with a stoic manner at a slow pace". The age and gender come from the voice.
- **`inworld-flash`** (TTS-2 Flash) is the fastest and cheapest. It ignores steering, so Kotodama sends none.
- **The voices come from the Inworld library.** Inworld labels each voice with gender, age group and language. Thus a Galka gets an older, deeper voice and a Tarutaru gets a young, bright one. Kotodama also includes the voices that you clone or design in the Inworld portal.

To check Inworld:

```
server/kotodama.sh doctor                      # key, how many voices you can cast from
server/kotodama.sh doctor --provider inworld   # time to first audio (one short line)
server/kotodama.sh voices cast --provider inworld
```

- The sidecar refreshes the voice list each day. `//koto voices sync` refreshes it now. Kotodama leaves out library voices in other languages. To add languages, use `"inworld": {"voices": {"languages": ["en", "ja"]}}`.
- `//koto status` estimates the cost at the Inworld pay-as-you-go price per character: $25 per million for TTS-2 and $15 for Flash. Plans cost less. To set your own rate, use `"models": {"inworld": {"price": {"per_1k_chars": 0.02}}}`.
- Kotodama speaks square brackets in game text as parentheses, because Inworld reads `[...]` as a hidden instruction.
- On a regional (Enterprise) plan, set `inworld.base_url` to `https://api.eu.inworld.ai` or `https://api.in.inworld.ai`.

### MiniMax

`minimax` and `minimax-hd` use the API of MiniMax, which **streams**. The audio starts a fraction of a second after the line appears, about the same as OpenAI and ElevenLabs. They use MiniMax Speech 2.8 Turbo and HD. These have 62 labelled English voices and real pitch control (Galka lower, Tarutaru higher).

Put `MINIMAX_API_KEY=...` in the key file and press R in the sidecar terminal. Then use `//koto provider minimax`. To check it first:

```
server/kotodama.sh doctor                      # key, voice count
server/kotodama.sh doctor --provider minimax   # time to first audio (one short line)
server/kotodama.sh voices cast --provider minimax
```

- **Your own voices are included.** Kotodama adds the voices that you cloned or designed on your MiniMax account to casting automatically. It also adds English system voices that are not on the built-in list. It refreshes them each day, or when you use `//koto voices sync`.
- **Emotion:** MiniMax selects one emotion for each line. To set it from the temperament of each NPC, add `"models": {"minimax": {"input": {"voice_setting": {"emotion": "{emotion}"}}}}` to `server/config.json`.
- **Region:** the default host is `api.minimax.io`. For the host of a different region, set `minimax.base_url`. Set `minimax.group_id` only if your account needs one.
- `//koto status` estimates the cost at the MiniMax pay-as-you-go price per billed character: $60 per million for Turbo and $100 for HD. To set your own rate, use `"models": {"minimax": {"price": {"per_1k_chars": 0.05}}}`.
- Replicate also has the same models, as `minimax-replicate` and `minimax-replicate-hd` (`REPLICATE_API_TOKEN`).

### Gemini

`gemini` and `gemini-lite` use the Gemini 3.8 Flash TTS models of Google. Like MiniMax, they **stream**. They are different from the other providers in two ways:

- **The delivery comes from the character of the NPC.** Kotodama sends each line with a short direction made from the tone, personality and pace of the NPC. An example is "Tone: deep, gravelly and rumbling. Character: stoic. Pace: slow." The age, gender and accent come from the voice, as Google recommends.
- **The voice library is large.** Kotodama casts from the full English voice library of Google, not only the 30 classic voices (Kore, Puck, Charon...). Google labels each voice with gender, pitch and accent, so casting has a lot of information to use. Kotodama also includes the voices that you design or clone in Google AI Studio.

Get a key at aistudio.google.com and put `GEMINI_API_KEY=...` in the key file. Press R in the sidecar terminal, then use `//koto provider gemini`. To check it first:

```
server/kotodama.sh doctor                     # key, how many voices you can cast from
server/kotodama.sh doctor --provider gemini   # time to first audio (one short line)
server/kotodama.sh voices cast --provider gemini
```

- `gemini-lite` uses Flash-Lite. It costs less and is a little less expressive.
- The sidecar refreshes the voice list each day. `//koto voices sync` refreshes it now. Kotodama leaves out voices in other languages. To add languages, use `"gemini": {"voices": {"languages": ["en", "ja"]}}`.
- Google bills by text tokens and audio tokens. `//koto status` shows the audio tokens per day and estimates the cost at the Standard-tier list price of Google. To set your own rate, use `"models": {"gemini": {"price": {"per_1m_input_tokens": 0.5, "per_1m_output_tokens": 9.0}}}`.
- In Gemini, tags such as `<laugh>` or `<sigh>` are sound effects. Thus Kotodama never adds them.

### Replicate and other backends

Kotodama can also use text-to-speech models on [Replicate](https://replicate.com), and any server with the OpenAI `/v1/audio/speech` API. A small **adapter** in `server/tts_models.json` describes each model. Each adapter is a provider that you can select in game: `//koto provider chatterbox-turbo`. Kotodama casts voices automatically, the same as with ElevenLabs.

Put `REPLICATE_API_TOKEN=r8_...` in the key file. Then run:

```
server/kotodama.sh providers                    # what's available
server/kotodama.sh doctor                       # token, and each model's inputs still match
server/kotodama.sh voices cast --provider kokoro
server/kotodama.sh test --provider minimax-replicate --name Rok --race 8 "Hm."
```

Models that ship with Kotodama:

| Provider | Model | Good for |
|---|---|---|
| `minimax-replicate` | `minimax/speech-2.8-turbo` | MiniMax through Replicate: 62 labelled English voices, real pitch control. With a MiniMax key, `minimax` (above) is the same model and streams |
| `minimax-replicate-hd` | `minimax/speech-2.8-hd` | The same voices at higher quality, a little slower |
| `chatterbox-turbo` | `resemble-ai/chatterbox-turbo` | Fast open-source model. Preset voices, plus clones from your voice bank |
| `chatterbox` | `resemble-ai/chatterbox` | Expressive open-source model with emotion intensity. Voices come only from your voice bank |
| `kokoro` | `jaaari/kokoro-82m` | Small, cheap open-source model with 26 English voices. It is a community model, so it can be slow to start |
| `qwen3` | `qwen/qwen3-tts` | Accepts acting directions (tone, character, pace) as text. Preset speakers, plus clones from the voice bank |

**Latency.** Replicate returns finished audio files, not a stream. Thus Kotodama splits each line at the end of each sentence. It makes the first sentence alone and starts to play it while it makes the rest in parallel. The first word comes one or two seconds after the line appears, and this changes with the model. Replicate keeps the official models warm (all models above except Kokoro). A community model that did not run recently can need a minute to start. If this occurs, the sidecar tells you in chat. Kotodama caches these lines like the lines of other providers, so a repeated line is instant and free.

**Cost.** Replicate bills your account for each run. `//koto status` shows the characters and compute seconds per model. It also shows a dollar estimate at the Replicate list price. The official models are priced per character, and Kokoro per second of T4 GPU time. To use a different rate, set `price` on the model in `server/config.json`. For example: `"chatterbox": {"price": {"per_1k_chars": 0.02}}` or `"kokoro": {"price": {"per_second": 0.0002}}`. A model that you add gets a dollar estimate only after you give it a `price`.

### The voice bank (for cloning models)

The original Chatterbox has no preset voices. Chatterbox Turbo and Qwen3 can also clone. These models need a short reference clip of the voice. Kotodama keeps these clips in `voices/` (not committed) and casts NPCs to them by their labels:

- **Make clips automatically:** `server/kotodama.sh voices design` makes labelled clips for each race and creature with Qwen3-TTS voice design. By default it makes 3 variants of each, which is about 70 predictions. It asks before it spends money. To make fewer, use `--only galka,taru_m` and `--variants 2`. When you run it again, it makes only the clips that are missing.
- **Add your own clips:** put a WAV of clean speech in `voices/`, 5.5 to 7.5 seconds long. You can add a `.json` file with the same name that holds `{"name", "gender", "age", "description", "reference_text"}`. The transcript helps Qwen3. Without a `.json` file, Kotodama guesses the labels from the file name (`old_man_gruff.wav` = old, male, gruff). Kotodama cuts longer clips to length.
- `server/kotodama.sh voices bank` lists the clips in the bank, and why it skipped any of them. In game, `//koto voices sync` loads the changes.

### Adding another model

You can add any Replicate TTS model without code. Tell the sidecar to read the inputs of the model and write a draft adapter:

```
server/kotodama.sh replicate schema zsxkib/dia
```

Paste the adapter that it shows into `server/config.json` under `"models"`. Change it as necessary, and press R in the sidecar terminal. An adapter connects the input fields of the model to placeholders. Kotodama fills the placeholders from the profile of each NPC:

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

- `{text}` and `{voice}`.
- `{speed}`, from pace.
- `{pitch}`, through the `controls.pitch` table of the adapter.
- `{emotion}`.
- `{exaggeration}` and `{cfg_weight}`, from temperament, through `controls`.
- `{style}`, a short acting direction.
- `{voice_description}`.
- `{seed}`, the same for each NPC every time.
- `{reference_audio}` and `{reference_text}`, from the voice bank.

`input_clone` holds the fields that change when the voice is a bank clip. `null` removes a field. `voices` can list voices, or name a set from `tts_models.json` (`"@kokoro_en"`). It can also include `"schema"` (the voice list of the model) and `"bank"`. To change a model that ships with Kotodama, override only what is different. For example: `"kokoro": {"controls": {"speed": {"normal": 1.1}}}`. `"enabled": false` hides a model.

### Self-hosted and other OpenAI-compatible servers

Some servers stream audio directly. Examples are Kokoro-FastAPI, Chatterbox TTS servers, and hosted services with an OpenAI-style speech endpoint. If you have one, it gives the lowest latency. Kotodama includes an example, which is disabled:

```json
"models": {
  "kokoro-local": {"enabled": true, "base_url": "http://192.168.1.20:8880/v1"}
}
```

Fields:

- `transport: "openai_compatible"`.
- `base_url`, including `/v1`.
- `model` and `voices`.
- `response_format`: `pcm` or `wav`.
- `api_key_env`, optional.
- `extra`, for more request fields. It uses the same placeholders.

If the server returns a rate limit (429), Kotodama tries again one time before any audio plays. If the server says that it does not have a voice (404), Kotodama removes that voice from casting. Kotodama sends lines longer than `max_chars` (default 1000) in parts. For a hosted service, add `"price": {"per_1k_chars": ...}` to get a cost estimate in `//koto status`.

## How voices are chosen

For each field, the source with the highest priority wins:

1. **Your overrides** (`config/overrides.json`, which `//koto voice` writes).
2. **The NPC library:** first the curated entries, then community packs, then the entries that casting learned.
3. **A stable variation for each name** inside the archetype. Thus the same NPC always sounds the same, on every machine.
4. **The archetype.** It comes from the race of the speaker (Hume, Elvaan, Tarutaru, Mithra, Galka, children), the creature type, or keywords in the name (Moogle, Goblin, Yagudo, Quadav...). To change archetypes, edit `server/archetypes.json`.

A key can be specific to a zone (`"Name@<zone id>"`), for NPCs whose character changes between zones. `@player` and `@narrator` set the voice for those roles. For these, edit `config/overrides.json` directly.

Kotodama turns each profile into an acting direction for the TTS model ("Voice: elderly female Hume. Personality: ... Tone: ... Pace: ..."). The profile is part of the cache key. Thus, if a profile changes, you get new audio, and the old lines stay in the cache.

### Optional voice casting (LLM)

By default (`inference.provider: "none"`), voices come from the archetypes and your overrides. You can set `inference.provider` to `anthropic` or `openai` in `server/config.json`. Then Kotodama asks the model one time to describe how an unknown NPC sounds. It asks when the NPC first speaks, when you target it, or when you start an event with it.

- The sidecar validates the answer against a strict schema and saves it to `library/npcs.json`. It never asks again for that NPC.
- The first line waits up to `wait_ms` (2.5 s) for the answer. After that, the line uses the archetype voice, so dialog never waits long.
- Some NPCs have no race from the game. These are story characters with a fixed "precomposed" model. They wait up to `unknown_wait_ms` (8 s) instead. Their only other voice is a genderless one, and casting replaces it from their second line.

To use `anthropic`, install the SDK in a virtual environment:

- Linux: `python3 -m venv server/.venv && server/.venv/bin/pip install anthropic`. If `server/.venv` exists, `kotodama.sh` uses it automatically.
- Windows: `py -3 -m venv server\.venv`, then `server\.venv\Scripts\pip install anthropic`. If `server\.venv` exists, `kotodama.cmd` uses it automatically.

More facts about casting:

- If the SDK or the API key is missing, the sidecar runs with casting off and says why. It says this in game when the addon connects, in `//koto status`, and in `doctor`.
- `max_per_minute` limits how often the sidecar casts. If casting fails for an NPC, the sidecar does not try that NPC again for ten minutes.
- `//koto status` counts the tokens that each casting call uses. It estimates the cost at the list price of the model. `inference.prices` has the current Claude models and `gpt-4o-mini`. For a different model, add an entry, for example `"inference": {"prices": {"my-model": {"per_1m_input_tokens": 1.0, "per_1m_output_tokens": 4.0}}}`. For a model with no entry, status shows tokens only.

### Sharing a library

The library has two files:

- `library/npcs.default.json` holds the voices that ship with Kotodama. Each release replaces it, so do not edit it.
- `library/npcs.json` holds what your install learned or what you saved. Releases do not contain this file, so an upgrade never overwrites it. Its entries win over the shipped entries. The shipped voices of a new release still apply to NPCs that have no entry in your file.

The two files are plain, sorted JSON that is easy to diff. They contain no player names.

```
server/kotodama.sh library export -o my-npcs.json --source llm --source curated
server/kotodama.sh library import friends-npcs.json    # installs read-only into library/packs/
server/kotodama.sh library list
```

Imported packs win over your learned (LLM) entries, but not over your curated entries. Your overrides always win.

## Loudness and ducking

**Loudness.** The TTS models give voices at very different levels. One voice can be 15 to 20 dB quieter than another. But the lines of one voice stay within one or two dB of each other. Thus the sidecar does this:

1. It measures each line in LUFS, the loudness scale of broadcast audio.
2. It gives each *voice* one steady gain. The gain brings a typical line of that voice to `audio.normalize.target_lufs` (default −13).

A voice is its provider, model and voice. Thus lines styled as a whisper or a shout are still the same voice. A whisper stays a whisper, a shout stays a shout, and the rise and fall inside a line does not change. `//koto volume` applies on top of this: 100 is the normalized level, and 200 is twice as loud, up to 400. On loud parts, a peak limiter decreases its gain, so the audio does not clip.

- **The level of a voice** is the median of its last 20 lines that are one second or longer (`voice_window`). Thus a single whisper or shout does not move it. The maximum boost is +15 dB and the maximum cut is −10 dB (`max_boost_db`, `max_cut_db`).
- **A new voice** starts from the typical level of other voices on the same model. If the sidecar has no measurements yet, it uses the fixed `audio.makeup_db` boost of +9 dB. During the first line, the gain moves toward the target, at most 2 dB per second and at most 6 dB from the start value (`first_line_adapt`). From the second line, the gain is steady. This adds no delay.
- **To start a new voice at the right level,** set `lookahead_ms` (for example 500). The sidecar then holds back that much of the first line to measure it. This delays only the first line of a new voice, by exactly that amount.
- **The leveler** can lift lines that become quieter partway through (`leveler.enabled`, off by default). It compares the last 3 seconds of speech with the start of the line, and ignores a drop of 2 dB or less. It lifts at most `leveler.max_db` (6 dB), at 2 dB per second. It never makes anything quieter, and it does not move during pauses. It also lifts a line that becomes quiet on purpose, and that is why it is off. `leveler.providers` limits it to some providers (empty: all providers).
- **The cache keeps the unscaled audio** with the measured level of each line. Thus a change to these settings never invalidates the cache.
- **To turn it off,** use `//koto norm off`, or set `audio.normalize.enabled: false` in `server/config.json`. Then each line gets the fixed `audio.makeup_db` boost. `//koto norm default` goes back to the value in the config file.
- **To see how it works,** use `//koto voice <name>`. It shows the measured level of a voice and its gain, for example `loudness: -24.0 LUFS over 12 line(s) -> +11.0 dB`. With `//koto debug on`, the sidecar log shows how the sidecar selected the gain of each line.

If the voices are still too quiet next to the music, increase `volume` or `audio.normalize.target_lufs` (−10 is loud), or decrease the volume of the game.

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

**Ducking.** While a line plays, the sidecar makes the audio stream of the game quieter, to `duck.level` percent. The game sends music and effects in one stream, so both become quieter. The sidecar lowers the volume quickly at the start of a line and brings it back slowly after the line. Between lines, it keeps the lower level for `hold_ms`, so the music in a cutscene does not go up and down. On Linux, ducking works at the PipeWire/PulseAudio level through `pactl`. Thus it needs no support from the game or Windower.

On Windows (experimental), the sidecar lowers the per-app volume of FFXI instead. This is the same slider as in the Volume Mixer. The sidecar uses the audio API of Windows, so you do not need to install anything. The audio of FFXI belongs to `pol.exe`, and the default `duck.match` includes it. The sidecar finds apps on all output devices, so ducking also works with headphones. `server\kotodama.cmd audio list` shows the audio of each app by process id.

- `duck.match` tells the sidecar which stream is the game. A stream matches if an entry is part of its application name, binary or Flatpak app id. The defaults match `windower`, `playonline`, `ffxi` and `pol.exe`. To see what matches, run `server/kotodama.sh audio list` while the game runs. The list marks the game stream `DUCK`.
- `audio duck-test` lowers the game volume for three seconds and then sets it back. Use it to make sure that ducking works without the addon.
- While the audio is ducked, the sidecar saves the original volume to `cache/duck_state.json`. If something kills the sidecar during a duck, the next start sets the volume back automatically. You can also run `audio restore`.
- Windows remembers the volume of each app. Thus a crash during a duck would keep FFXI quiet, also after a restart of the game. To prevent this, the next start finds FFXI by its app, not only by the session that it ducked, and sets the volume back. If you close the sidecar window, log off or shut down, the sidecar sets the volume back before it stops.
- The sidecar never ducks its own player streams (`pw-play`, `paplay`, `ffplay`, and on Windows the sidecar's own audio).

## Cache and cost

The sidecar stores each synthesized line in `cache/`, with a separate cache for each provider. The default limit is 2 GB. When the cache is full, the sidecar removes the lines that played least recently. If you hear the same line again, it costs nothing.

- If you skip a line before it finishes, the sidecar still downloads the audio into the cache, because you already paid for it (`cache.finish_download_on_stop`).
- `//koto status` shows the counts for today, with an approximate dollar estimate at the list price of each provider. This includes voice casting.
- For ElevenLabs, status also shows characters and estimated credits (`elevenlabs.credits_per_char`). The default is 0.5 for Flash models and 1 for other models. Look at your plan for the real numbers.
- `server/kotodama.sh cache stats` works without the game.
- `cache analyze` measures the loudness of each cached line that has no measurement yet. `--all` measures all lines again.

## Choosing what is spoken

FFXI does not mark which events are cutscenes, so Kotodama does not try to find them. It puts a line in the smaller group only when the game sends a packet that says so. If Kotodama makes a mistake, you hear a line that you did not want. You do not get silence where a line should be.

To control what you hear:

- **`mode events`** removes one-line greetings and town criers. The game announces each of these with its own packet. Kotodama still speaks quest dialog, cutscenes and zone-in scenes.
- **`deny <name>`** silences an NPC everywhere, for example `//koto deny Survival Guide` or `//koto deny *Moogle`. Use `deny t` for your target and `deny last` for the last speaker. `allow` undoes it.
- **`battle`**, **`narrator`** and **`own`** turn battle chatter, unnamed narration and the lines of your own character on or off.
- **Enter** (or a dialog advance with a controller) stops the line that is playing while `interrupt` is on. `interrupt` is on by default. `//koto stop` always stops the line.

`mode cutscenes` does not exist anymore. If your saved settings use it, Kotodama loads `mode all`. Kotodama does not read or write `library/cutscenes.json` anymore, so you can delete that file.

# Developers

## How it works

Kotodama has two parts:

- **The addon** (`Kotodama.lua`, `lib/`) runs inside Windower. It only listens. It captures dialog lines and finds who speaks and if the line is part of an NPC event. Then it decides if Kotodama speaks the line.
- **The sidecar** (`server/`) is a small Python program that runs on your machine, outside the game. It holds your API keys and selects the voice. It calls the text-to-speech service, caches the audio and plays it. The sidecar is separate because Windower cannot stream or stop audio. Also, a network call inside an addon would freeze the game.

The two parts communicate over a local UDP socket on `127.0.0.1:47123`. Nothing outside your own machine can reach it.

## How dialog is classified

`lib/scene.lua` follows NPC events from the packets that the game sends:

- `0x032`, `0x033` and `0x034` start an event.
- `0x052` releases it.
- `0x00B` (zone out) ends everything.

Each line gets one of these kinds:

- **event**: the line arrives while an event is open, or just after the game released one (a late line from that event). Speech that arrives with no event and no NPC-chat packet is also an event. These are scenes that the server runs without an event packet, such as zone-in cutscenes.
- **ambient**: a line right after an NPC-chat packet (`0x036`), such as a shop greeting. Also a system-mode line (chat mode 151) outside an event.
- **battle**: chat mode 142.

Kotodama does not try to tell cutscenes from other events. The game has no flag for this. A filter based on a guess would drop lines and not tell you.

With `//koto debug on`, `data/debug.log` records:

- each decision, with its reason.
- the raw bytes of each event-start packet.
- the status of the player at each event start and on each line.
- a count of the packets that passed during each event or untracked scene.

If you find a line with the wrong kind, look at that log.

`python3 tests/lua/replay_log.py data/debug.log` replays a recorded log through the current rules. It lists each line whose kind changed. Use it to test a rule change against your real sessions.

## Creature models

Non-player creatures and some townsfolk with a fixed model have a model id instead of a race (`race=0`). Map them to a creature or race archetype in `data/creature_models.lua`. For example, Moogles are model 82, and the Whitegate Mithra child is 182. You can also use name keywords and overrides. `//koto inspect t` writes the fields of the target to `data/inspect.log`, including `race` and `models`.

## Tests

- **Sidecar:** `cd server && python3 -m unittest discover -s tests`. On Windows, use `py -3` instead of `python3`.
- **Lua:** the addon runs under Lua 5.1 with a stubbed Windower, through the optional `lupa` package (pinned in `tests/lua/requirements.txt`):
  ```
  python3 -m venv .venv-lua && .venv-lua/bin/pip install -r tests/lua/requirements.txt && .venv-lua/bin/python -m unittest discover -s tests/lua
  ```
  Without `lupa`, these tests are skipped, not failed. Thus make sure that the output shows more than `skipped`.

GitHub Actions (`.github/workflows/tests.yml`) runs the two test suites on each pull request and each push to `main`:

- The sidecar tests run on Linux and Windows, with Python 3.10 and the newest Python.
- The Lua tests run on Linux. If `lupa` cannot load Lua 5.1, the Lua job stops with an error.

## Releases

1. Set the same version in `Kotodama.lua` (`_addon.version`) and `server/kotodama/__init__.py` (`__version__`). Merge that change to `main`.
2. Tag the commit and push the tag:
   ```
   git tag v0.2.0 && git push origin v0.2.0
   ```

Then `.github/workflows/release.yml` does these steps:

1. It runs all the tests.
2. It builds `Kotodama-v0.2.0.zip` with a `.sha256` file.
3. It publishes a GitHub release, with notes made from the merged pull requests.

If the tag is different from one of the two versions, the workflow publishes nothing. If a tag has a hyphen, such as `v0.2.0-rc1`, the workflow publishes a prerelease.

The zip holds one `Kotodama/` folder that `git archive` builds, so it contains only committed files. It leaves out all files marked `export-ignore` in `.gitattributes` (tests, CI files, repository tooling). The build stops if the zip contains files that must not ship, such as a learned library or a key file. To build the same zip locally, run `scripts/build_release.sh`. It writes to `dist/`.

### Updating the shipped library

The sidecar writes only to `library/npcs.json`, which git ignores. To add your learned voices to the voices that Kotodama ships, merge them into the shipped file and commit it:

```
server/kotodama.sh library export --with-defaults -o library/npcs.default.json
```

# AI usage and disclosure

Agentic AI tools help with the design and development of Kotodama. They write drafts of code, tests and documentation, and they help to examine design options.

A human reviews all code before it goes into the project. A human also tests each change, with the test suites above and, where possible, in the game.

# Credits

The handling of dialog markup comes from the Balloon addon (Hando, Yuki, Ghosty; BSD-3-Clause).

# License

Kotodama is released under the MIT License. The code that comes from Balloon keeps its BSD-3-Clause notice. You can find the two licenses in [LICENSE](LICENSE).
