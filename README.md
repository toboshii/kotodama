# Kotodama

AI voiceover for Final Fantasy XI. Kotodama reads NPC quest and cutscene dialog as it appears in your chat log and speaks it aloud, with a different voice for each kind of character.

It has two parts:

- **The addon** (`Kotodama.lua`, `lib/`) runs inside Windower. It only listens: it captures dialog lines, works out who is speaking and whether it's a cutscene, and decides whether to voice the line.
- **The sidecar** (`server/`) is a small Python program that runs on your machine outside the game. It holds your API keys, picks the voice, calls the text-to-speech service, caches the audio, and plays it. It is separate because Windower can't stream or stop audio, and a network call inside an addon would freeze the game.

The two talk over a local UDP socket on `127.0.0.1:47123`. Nothing is exposed beyond your own machine.

## What gets sent where

Dialog text is sent to OpenAI to be turned into speech. If you turn on voice casting (optional, off by default), the NPC's name, race, zone and up to three sample lines are sent to the casting provider you pick. **Your character's name is replaced with `{player}` before anything is stored in a shareable file or sent for casting.** Speech requests do contain the literal line, including your name if the NPC says it, because that's what has to be spoken.

## Setup

You need Python 3.10+ and an audio player (`pw-play`, `paplay`, or `ffplay`), and an OpenAI API key.

1. **Give it a key.** Either put `OPENAI_API_KEY=sk-...` in `~/.config/kotodama.env` (used by the systemd unit below) or export it in your shell.
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
| `prefetch on/off` | Warm the connection and cast voices when you target or start talking to an NPC, so their first line is quicker (default on) |
| `volume <0-400>` | Voice loudness in percent. 100 is normal; go higher if the voice sits below the game audio (see **Loudness and ducking**) |
| `duck on/off`, `duck <0-100>` | Lower the game's audio while a line is spoken; the number is how much of it is kept (default 40) |
| `stop`, `replay` | Playback controls |
| `deny <name\|t\|last>` / `allow <...>` / `deny list` | Silence specific NPCs. `t` is your target, `last` is whoever spoke last. Names accept `*` wildcards, e.g. `deny Moogle*` |
| `voice <name\|t\|last>` | Show the voice an NPC will get and why |
| `voice <name> key=value ...` | Override it. Keys: `gender` `age` `species` `personality` `tone` `accent` `pitch` `pace` `voice` `direction`. Use `_` for spaces: `//koto voice t age=elderly tone=gruff_and_weary` |
| `voice <name> reset` / `reinfer` / `promote` | Clear your overrides / ask the casting model again / save the effective profile to the shared library |
| `status` | Link, mode, event state, cache and estimated cost |
| `test [event\|cutscene\|ambient\|battle] <Name> : <text>` | Push a line through the whole pipeline |
| `debug on/off`, `inspect [t]` | Write `data/debug.log`; dump the target's fields to `data/inspect.log` |

Allowed values: `gender` male/female/neutral; `age` child/young/adult/middle-aged/elderly; `pitch` very low/low/medium/high/very high; `pace` slow/measured/normal/brisk/fast; `voice` one of the TTS provider's voices (OpenAI: alloy, ash, ballad, coral, echo, fable, nova, onyx, sage, shimmer, verse, marin, cedar — check your provider's current list).

Addon settings (mode, toggles, volume, deny list) are saved in `data/settings.xml`.

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

Every synthesized line is stored in `cache/` (default cap 2 GB, least-recently-played evicted). Hearing the same line again costs nothing. If you advance past a line before it finishes, the audio is still downloaded into the cache because it's already paid for (`cache.finish_download_on_stop`). `//koto status` shows today's counts and a rough estimate at list prices; check your provider's pricing for the real figure. `python3 server/kotodama_server.py cache stats` works without the game.

## Limitations and what to check on first run

Cutscene detection is inferred from packet flow (see `lib/scene.lua`); FFXI has no "cutscene" flag. An event you didn't start (a trigger or zone-in scene), one with several speakers, a line spoken by your own character, or auto-advancing lines are treated as cutscenes. In `mode cutscenes`, the first line before one of those signals appears can be skipped. If something is misclassified, `//koto debug on` logs every line with its classification and the reason to `data/debug.log`.

Things worth confirming with real play, because they depend on what the game actually sends:

- `//koto inspect t` on a few NPCs: that `race` and `models` are populated, and that town NPCs report `spawn_type=2` (used for target prefetch). Map non-player creature model ids in `data/creature_models.lua` if you want automatic creature voices; otherwise use name keywords and overrides.
- That controller button presses cut the current line (the addon uses the event-response packet for this, so it should work without keyboard focus).
- That ducking finds the game: `python3 server/kotodama_server.py audio list` should show your game's stream marked `DUCK`.
- That the UDP link comes up (`//koto status` should say `link UP`). The game needs network access to play at all, so a Flatpak that shares the host network should reach the sidecar's loopback socket; this hasn't been verified on a Steam Deck yet, so if the link stays down, check the game's network permission first.

The sidecar's own tests: `cd server && python3 -m unittest discover -s tests`.

## Credits

Dialog markup handling is derived from the Balloon addon (Hando, Yuki, Ghosty; BSD-3-Clause).
