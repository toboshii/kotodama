# Kotodama

AI voiceover for Final Fantasy XI. Kotodama reads NPC quest and cutscene dialog as it appears in your chat log and speaks it aloud, with a different voice for each kind of character.

It has two parts:

- **The addon** (`Kotodama.lua`, `lib/`) runs inside Windower. It only listens: it captures dialog lines, works out who is speaking and whether it's a cutscene, and decides whether to voice the line.
- **The sidecar** (`server/`) is a small Python program that runs on your machine outside the game. It holds your API keys, picks the voice, calls the text-to-speech service, caches the audio, and plays it. It is separate because Windower can't stream or stop audio, and a network call inside an addon would freeze the game.

The two talk over a local UDP socket on `127.0.0.1:47123`. Nothing is exposed beyond your own machine.

## What gets sent where

Dialog text is sent to the voice provider you use (OpenAI or ElevenLabs) to be turned into speech. If you turn on voice casting (optional, off by default), the NPC's name, race, zone and up to three sample lines are sent to the casting provider you pick. **Your character's name is replaced with `{player}` before anything is stored in a shareable file or sent for casting.** Speech requests do contain the literal line, including your name if the NPC says it, because that's what has to be spoken.

## Setup

You need Python 3.10+, an audio player (`pw-play`, `paplay`, or `ffplay`), and an API key for at least one voice provider: OpenAI, ElevenLabs, or both (see **Voice providers**).

1. **Give it a key.** Put `OPENAI_API_KEY=sk-...` and/or `ELEVENLABS_API_KEY=...` in `~/.config/kotodama.env` (used by the systemd unit below), or export them in your shell.
2. **Check the setup:**
   ```
   python3 server/kotodama_server.py doctor
   ```
3. **Hear it without the game:**
   ```
   python3 server/kotodama_server.py test --name Ayame --race 2 "Welcome to Norg."
   python3 server/kotodama_server.py test --name Ayame --race 2 "Welcome to Norg."   # second run: from cache, no API call
   ```
4. **Run the sidecar.** Start it from a normal host terminal, or install the systemd *user* unit so it runs whenever you're logged in:
   ```
   cp server/kotodama.service ~/.config/systemd/user/
   systemctl --user daemon-reload && systemctl --user enable --now kotodama
   ```
   Don't run it from a sandboxed terminal (e.g. a Flatpak editor's terminal): it needs your real audio session and the host's loopback.
5. **Load the addon** in game: `//lua load kotodama`. To load it automatically, add `lua load kotodama` to your Windower `scripts/init.txt`. If the sidecar isn't running you'll get one chat warning and the game is otherwise unaffected.

## In-game commands

`//koto` (or `//kotodama`)

| Command | What it does |
|---|---|
| `on` / `off` | Turn voiceover on or off |
| `mode all` / `events` / `cutscenes` | **all**: every NPC line; **events**: only NPC events (quest dialog and cutscenes, not one-line ambient chatter); **cutscenes**: only cutscenes |
| `battle on/off` | Voice battle chatter such as Trusts (default off) |
| `own on/off` | Voice your own character's lines (default on) |
| `narrator on/off` | Voice lines with no speaker name (default off) |
| `interrupt on/off` | A new line cuts off the previous one (on), or lines queue (off) |
| `prefetch on/off` | Warm the connection and cast voices when you target or start talking to an NPC, so their first line is quicker (default on). NPCs whose race the game doesn't report are cast from their first line instead |
| `volume <0-400>` | Voice loudness in percent. 100 is normal; go higher if the voice sits below the game audio (see **Loudness and ducking**) |
| `duck on/off`, `duck <0-100>` | Lower the game's audio while a line is spoken; the number is how much of it is kept (default 40) |
| `stop`, `replay` | Playback controls |
| `provider openai` / `elevenlabs` / `default` | Switch the voice provider live; `default` uses `tts.provider` from `server/config.json` |
| `voices` / `voices sync` | ElevenLabs: how many voices are known / refresh the list from your account |
| `deny <name\|t\|last>` / `allow <...>` / `deny list` | Silence specific NPCs. `t` is your target, `last` is whoever spoke last. Names accept `*` wildcards, e.g. `deny Moogle*` |
| `voice <name\|t\|last>` | Show the voice an NPC will get and why |
| `voice <name> key=value ...` | Override it. Keys: `gender` `age` `species` `personality` `tone` `accent` `pitch` `pace` `voice` `direction`. Use `_` for spaces: `//koto voice t age=elderly tone=gruff_and_weary` |
| `voice <name> reset` / `reinfer` / `promote` | Clear your overrides / ask the casting model again / save the effective profile to the shared library |
| `cutscene` / `cutscene forget` | Mark the NPC event you're in (or just finished) as a cutscene, remembered for next time, or undo that |
| `status` | Link, mode, event state, cache and estimated cost |
| `test [event\|cutscene\|ambient\|battle] <Name> : <text>` | Push a line through the whole pipeline |
| `debug on/off`, `inspect [t]` | Write `data/debug.log`; dump the target's fields to `data/inspect.log` |

Allowed values: `gender` male/female/neutral; `age` child/young/adult/middle-aged/elderly; `pitch` very low/low/medium/high/very high; `pace` slow/measured/normal/brisk/fast; `voice` one of the active provider's voices: for OpenAI one of alloy, ash, ballad, coral, echo, fable, nova, onyx, sage, shimmer, verse, marin, cedar; for ElevenLabs any voice name on your account (`kotodama_server.py voices list`), e.g. `voice=George`.

Addon settings (mode, toggles, volume, deny list) are saved in `data/settings.xml`.

## Voice providers

Kotodama can speak through **OpenAI** (`gpt-4o-mini-tts`, the default) or **ElevenLabs**. Switch any time in game with `//koto provider elevenlabs` / `//koto provider openai`; the choice is remembered and restored when the sidecar restarts. Each provider keeps its own cached audio, so switching back and forth never re-synthesizes anything. To make ElevenLabs the default, set `"tts": {"provider": "elevenlabs"}` in `server/config.json`.

**OpenAI** has 13 fixed voices and takes free-text acting directions, so an NPC's age, personality and tone are passed straight through.

**ElevenLabs** voices belong to your account (the stock voices plus any you add from their library, design or clone), and there are no free-text directions. So:

- **Voices are auto-cast.** The sidecar fetches every voice your account can use and scores each against the NPC's profile: gender, age, and descriptive words from its tone, personality and pitch (a Galka's "deep, gravelly" against a voice described as deep and gravelly, a Tarutaru's "bright, bubbly" against bright and youthful voices). Among the best fits, the NPC's name picks one, so the same NPC always gets the same voice. Once cast, an NPC keeps its voice even if you add voices later; changing its profile (an override, a new LLM description) re-casts it. `//koto voice <npc> reset` re-casts by hand.
- **Personality goes into the delivery:** speed follows the profile's pace, stability follows temperament (lower for excitable characters, higher for calm ones), and on the v4 models one leading audio tag such as `[excited]` or `[sarcastic]` is added when the profile calls for it. ElevenLabs warns a tag is occasionally read aloud; if you hear one, set `"elevenlabs": {"audio_tags": false}`.
- **Model:** `eleven_v4_turbo` by default (their recommendation for real-time use). If ElevenLabs rejects it on the streaming endpoint, the sidecar switches to `eleven_flash_v2_5` for the session and says so in chat. Both are configurable (`elevenlabs.model`, `elevenlabs.fallback_model`).

Setting up ElevenLabs:

```
python3 server/kotodama_server.py voices sync      # fetch your account's voices
python3 server/kotodama_server.py voices cast      # preview which voice each race/creature gets
python3 server/kotodama_server.py voices cast --npc Prishe --race 4
python3 server/kotodama_server.py doctor --live    # confirms the model works (uses a few credits)
python3 server/kotodama_server.py test --provider elevenlabs --name Rok --race 8 "Hm."
```

To keep a voice out of casting, add its name or id to `elevenlabs.voices.exclude`. The voice list refreshes itself daily (`elevenlabs.voices.refresh_hours`).

## How voices are chosen

Highest priority wins, field by field:

1. **Your overrides** (`config/overrides.json`, written by `//koto voice`)
2. **The NPC library**: curated entries, then community packs, then entries learned by casting
3. **A stable per-name variation** within the archetype, so the same NPC always sounds the same, on every machine
4. **The archetype**: from the speaker's race (Hume, Elvaan, Tarutaru, Mithra, Galka, children), creature type, or name keywords (Moogle, Goblin, Yagudo, Quadav...). Edit `server/archetypes.json` to change these.

Keys can be zone-specific (`"Name@<zone id>"`) for NPCs who change character between zones, and `@player` / `@narrator` set the voice for those roles. Edit `config/overrides.json` directly for these.

Each profile is turned into acting direction for the TTS model ("Voice: elderly female Hume. Personality: ... Tone: ... Pace: ...") and is part of the cache key, so changing a profile gives fresh audio while old lines stay cached.

### Optional voice casting (LLM)

By default (`inference.provider: "none"`) voices come from archetypes plus your overrides. If you set `inference.provider` to `anthropic` or `openai` in `server/config.json`, the first time an unknown NPC speaks (or you target one, or start an event with them) the model is asked once to describe how they should sound. The answer is validated against a strict schema and saved to `library/npcs.json`; it's never requested again for that NPC. The first line waits up to `wait_ms` for the answer, then falls back to the archetype voice so dialog is never delayed much.

- For `anthropic`, install the SDK in a venv: `python3 -m venv server/.venv && server/.venv/bin/pip install anthropic`, then run the sidecar with that Python.
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

**Loudness.** The TTS model's output isn't mastered to game loudness, so the sidecar adds a fixed boost (`audio.makeup_db`, default +9 dB) before playback, and `//koto volume` scales on top of that: 100 is the boosted baseline, 200 is twice as loud again, up to 400. A peak limiter lowers its gain on loud passages instead of clipping them, so large boosts get louder without turning crunchy. The boost is applied at playback, so changing it never invalidates the cache. If it's still too quiet next to the music, raise `volume`, raise `audio.makeup_db`, or lower the game's own volume.

**Ducking.** While a line is spoken the sidecar lowers the game's own audio stream (music and effects together, since the game outputs one stream) to `duck.level` percent, quickly at the start of a line and gradually back afterwards. It holds the lowered level for `hold_ms` between lines so a cutscene doesn't pump the music up and down. This works at the PipeWire/PulseAudio level using `pactl`, so it needs no game or Windower support.

- Which stream is "the game" is decided by `duck.match`: any entry appearing in a stream's application name, binary or Flatpak app id. The defaults match `windower`, `playonline`, `ffxi` and `pol.exe`. Run `python3 server/kotodama_server.py audio list` with the game running to see what's matched.
- `audio duck-test` dips the game volume for three seconds and restores it, so you can confirm it works without the addon.
- The original volume is saved to `cache/duck_state.json` while ducked. If the sidecar is killed mid-duck, the next start restores it automatically, or run `audio restore`.
- Our own player streams (`pw-play`, `paplay`, `ffplay`) are never ducked.

## Cache and cost

Every synthesized line is stored in `cache/` (default cap 2 GB, least-recently-played evicted), per provider. Hearing the same line again costs nothing. If you advance past a line before it finishes, the audio is still downloaded into the cache because it's already paid for (`cache.finish_download_on_stop`). `//koto status` shows today's counts with a rough estimate: dollars at list price for OpenAI, characters and estimated credits for ElevenLabs (`elevenlabs.credits_per_char`, default 0.5 for Flash models and 1 otherwise). Check your plan for the real figures. `python3 server/kotodama_server.py cache stats` works without the game.

## How cutscenes are detected

FFXI doesn't flag cutscenes, so `lib/scene.lua` infers them from packet flow and from the dialog itself. Inside an NPC event, it's a cutscene if you didn't start it, a second speaker joins, the speaker isn't the NPC you talked to, your own character speaks, or lines auto-advance. Outside an event:

- A line right after an NPC-chat packet (`0x036`) is a one-liner such as a shop greeting, and stays **ambient** even though it auto-advances.
- A line that straggles in just after an event was released stays with that event.
- Any other speech is a scene the server drove without an event packet, such as a zone-in cutscene, and counts as a **cutscene**.

**The known gap:** a cutscene that opens with several lines from the NPC you talked to looks exactly like ordinary dialog until someone else speaks (the opening of the Fochacha scene is one). To close it, the first time an event is recognised as a cutscene by a solid signal, its zone and menu id are saved to `library/cutscenes.json`, and every later viewing is a cutscene from its first line. The file ships with a few confirmed entries, is plain sorted JSON you can share, and `//koto cutscene` adds an event by hand when you spot one the heuristics missed. In `mode cutscenes`, the first viewing of an unseen scene of that shape can therefore lose its opening lines.

With `//koto debug on`, `data/debug.log` records every decision with its reason, the raw bytes of each event-start packet, the player's status, and a count of the packets that flew by during each event or untracked scene. If you find a scene that's misclassified, that log is what to look at, and the packet counts are the place to look for a real start-of-cutscene signal.

## Limitations and what to check on first run

Cutscene detection is heuristic (see above). `python3 tests/lua/replay_log.py data/debug.log` replays a recorded log through the current rules and lists every line whose classification changed, which is the way to check a tweak against your real sessions.

Things worth confirming with real play, because they depend on what the game actually sends:

- `//koto inspect t` on a few NPCs: that `race` and `models` are populated. Town NPCs report `spawn_type=2` (used for target prefetch). Non-player creatures and some fixed-model townsfolk carry a model id instead of a race (`race=0`); map them to a creature or race archetype in `data/creature_models.lua` (Moogles are model 82, the Whitegate Mithra child is 182) or use name keywords and overrides.
- That controller button presses cut the current line (the addon uses the event-response packet for this, so it should work without keyboard focus).
- That ducking finds the game: `python3 server/kotodama_server.py audio list` should show your game's stream marked `DUCK`.
- That the UDP link comes up (`//koto status` should say `link UP`). The game needs network access to play at all, so a Flatpak that shares the host network should reach the sidecar's loopback socket; this hasn't been verified on a Steam Deck yet, so if the link stays down, check the game's network permission first.

Tests: `cd server && python3 -m unittest discover -s tests` for the sidecar. The Lua side runs under Lua 5.1 with a stubbed Windower via the optional `lupa` package: `python3 -m venv .venv-lua && .venv-lua/bin/pip install lupa && .venv-lua/bin/python -m unittest discover -s tests/lua`.

## Credits

Dialog markup handling is derived from the Balloon addon (Hando, Yuki, Ghosty; BSD-3-Clause).
