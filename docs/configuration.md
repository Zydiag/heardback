# Configuration

heardback reads two optional files. With neither, it runs with the defaults below.

| File | What | Read by |
|---|---|---|
| `~/.config/heardback/config.toml` | your desktop: tmux, folders, Bluetooth names, microphones, your name | `jv` and the voice daemon |
| `~/.config/heardback/voice.toml` | voice tuning: thresholds, TTS voice, timings | the voice daemon |

`$XDG_CONFIG_HOME` replaces `~/.config` when set. `HEARDBACK_CONFIG=/path/to/config.toml` points at another `config.toml` (handy for tests). Each process reads the files once at start, so restart the service after an edit: `systemctl --user restart heardback`.

A copy of `config.toml` with every key and its default is in [`examples/config.toml`](../examples/config.toml).

## config.toml

An unknown section or key, or a value of the wrong type, is an error: heardback refuses to start and names the bad key, rather than guessing. Text values must be non-empty and contain no control characters.

### `[user]`

| Key | Type | Default | What |
|---|---|---|---|
| `name` | text | none | What the assistant calls you. Omit it and the brain prompt says "the user". |

### `[tmux]`

| Key | Type | Default | What |
|---|---|---|---|
| `session` | text | none | The tmux session heardback drives. Omit it to use the session you are in, or the first session when the daemon runs outside tmux. |
| `project_roots` | list of text | `["~"]` | Folders whose direct children are your projects. "Open Claude in my notes project" looks for a child folder named like `notes` under each root. |
| `claude_dirs` | list of text | `["~"]` | Folders where a voice-opened Claude Code session may start without an extra read-back. Anything outside them is read back first, and a folder outside your home is refused. |
| `claude_home` | text | `"~"` | Where a new Claude Code session starts when you don't name a folder. Falls back to your home if it doesn't exist. |

### `[devices]`

| Key | Type | Default | What |
|---|---|---|---|
| `aliases` | table of text → text | `{}` | Spoken Bluetooth nicknames. Each value is part of an already-paired device's name: `aliases = { headphones = "WH-1000" }` makes "connect my headphones" work. Keys are matched case-insensitively. |

### `[mic]`

| Key | Type | Default | What |
|---|---|---|---|
| `mode` | text | `"default"` | The microphone mode used at start. Must be a key of `[mic.modes]`. `mic` in `voice.toml` overrides it. |

### `[mic.modes]`

| Key | Type | Default | What |
|---|---|---|---|
| any name | list of text | `default = ["@DEFAULT_SOURCE@"]` | Each mode is a list of PipeWire source names or globs, tried in order. Names may use letters, digits, `_` and `-`. `@DEFAULT_SOURCE@` is the system's default input; a monitor source is never picked. |

Example: a headset that falls back to the built-in mic when it disconnects.

```toml
[mic]
mode = "headset"

[mic.modes]
default = ["@DEFAULT_SOURCE@"]
headset = ["bluez_input.*", "alsa_input.*"]
```

`heardback-voice devices` shows which microphone each mode picks right now.

## voice.toml

Flat keys, no sections except `[stt_fixes]`. The ones most people touch:

| Key | Default | What |
|---|---|---|
| `wake_threshold` | `0.5` | Wake-word score needed to start listening. Lower hears you more easily and wakes more by mistake. |
| `tts_voice` | `"bm_george"` | Kokoro voice. The first letter is the accent: `a` American, `b` British. |
| `tts_volume` | `2.5` | Gain on the speech output, more than 0 and up to 5. |
| `end_silence` | `0.7` | Seconds of silence that end your turn. Lower is faster but cuts you off mid-pause. |
| `max_utterance_s` | `60` | Longest single turn, in seconds. |
| `follow_up` | `true` | Listen once more, without the wake word, after Claude asks you a question. |
| `mic` / `mic_node` | from `config.toml` / `"auto"` | Microphone mode, or a PipeWire node name to pin one device. |
| `stt_device` | `"cuda"` with the GPU extra, else `"cpu"` | Where speech-to-text runs. |
| `speaker_check` | `"off"` | `"wake"` checks your voice on the wake word; `"on"` checks every turn. Needs `heardback-voice enroll` then `heardback-voice tune`. |
| `speaker_wake`, `speaker_utt` | `0` | Speaker-match thresholds; `tune` sets them from your enrolment clips. |
| `stt_hotwords` | `[]` | Words speech-to-text should expect, such as names or sites. |
| `[stt_fixes]` | empty | Whole-phrase corrections applied to commands after speech-to-text, for words it keeps mishearing. Never applied to dictation. |
| `duck_media` | `true` | Pause media players while listening and resume after. |
| `notify` | `true` | Desktop notifications for "Listening…" and errors. |
| `automute_when_mic_in_use` | `true` | Ignore the wake word while another app is recording. |
| `log_text` | `false` | Write what you said into the journal. Off by default. |
| `dash_port` | `8787` | Local status page on 127.0.0.1; `0` turns it off. |

The dictation timings (`dictation_*`) and the wake near-miss log floor (`wake_miss_floor`) also live here; their defaults are in `src/heardback/voice/config.py`.

## Your own voice commands

`~/.config/heardback/actions.toml` adds commands of your own. Each becomes `jv do <name>` with phrases for the fast path and a tool for Claude, and goes through the same read-back rules as built-in verbs. `jv do check` validates the file.

## Environment

| Variable | What |
|---|---|
| `HEARDBACK_CONFIG` | Path to `config.toml`. |
| `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `XDG_DATA_HOME` | Where config, logs and models live (defaults `~/.config`, `~/.local/state`, `~/.local/share`, each with a `heardback` folder). |
| `ANTHROPIC_API_KEY` | Must **not** be set: the daemon refuses to start, so it can only ever use your Claude Code login. |
