# heardback

**A voice front end for your Linux desktop and Claude Code sessions that gives the model no shell.**

[![tests](https://github.com/Zydiag/heardback/actions/workflows/tests.yml/badge.svg)](https://github.com/Zydiag/heardback/actions/workflows/tests.yml)
[![licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)
![python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)
![platform: Linux](https://img.shields.io/badge/platform-Linux-lightgrey.svg)

![demo](docs/demo.gif)

[Read-back and confirmation](docs/readback.gif)
[Session status](docs/status.gif)

> **You need your own Claude Code login.** heardback runs the unmodified `claude` binary you already have, signed in the normal way. It never reads, copies or stores your token, and it refuses to start if `ANTHROPIC_API_KEY` is set. Every turn it sends to Claude Code counts against your plan's usage limits like any other Claude Code use.

Say the wake word (or press a key), speak, and heardback switches tmux windows, types into a Claude Code session, tells you which sessions are waiting on you, opens apps and tabs, sets reminders, and answers out loud. Speech recognition, speech synthesis and the wake word all run locally.

## Why

- **Your Claude Code sessions, from across the room.** "Anything waiting on me?", "what is window 3 asking?", "tell me when window 2 is done". It reads Claude Code's hooks and speaks up when a session finishes or needs you.
- **The model can't run a command.** The Claude Code process behind the voice (`claude -p`) gets no shell and no file tools, only a fixed list of `jv` verbs. Anything destructive is read back to you and needs a spoken "yes".
- **Most commands never reach a model.** Common phrases ("switch to window 2", "volume up") are matched locally: the median reply starts 0.9 s after you stop talking. Only open-ended requests go to Claude Code.

## Quick start (2 minutes, no microphone)

Not on PyPI yet: install from GitHub. You need Linux, Python 3.12+, [uv](https://docs.astral.sh/uv/), tmux, and Claude Code installed and logged in.

```sh
uv tool install git+https://github.com/Zydiag/heardback   # installs jv, heardbackd, heardback-voice (no voice models yet)
heardback doctor               # checks Claude Code, tmux, PipeWire, models; prints a fix for each miss
jv tmux list                   # your tmux windows, with Claude Code sessions marked
jv list                        # every verb and the phrases that reach it
jv status install-hooks        # optional: lets heardback see Claude Code session state
```

`jv` is the whole action layer and has no dependencies. Try a verb before you trust it: `jv tmux close 3 --dry-run` prints the read-back it would ask for and exits 3.

### Add voice

```sh
uv tool install --reinstall 'heardback[voice] @ git+https://github.com/Zydiag/heardback'      # NVIDIA GPU (CUDA): a large download
uv tool install --reinstall 'heardback[voice-cpu] @ git+https://github.com/Zydiag/heardback'  # no GPU: smaller models, slower replies
heardback-voice setup                    # downloads the pinned models
```

Then install `systemd/heardback.service` as a user unit and start it. With no wake model configured, heardback is push-to-talk: bind a key to `systemctl --user kill -s SIGUSR1 heardback.service`. See [configuration](docs/configuration.md) for tmux, folders, microphones and Bluetooth names.

## How it stays safe

- **Allowlist, not a shell.** The brain runs `claude -p` with `--tools ""` and one MCP server whose tools are `jv` verbs. Each verb declares its effect (read, reversible, destructive, outbound).
- **Read-back and a spoken yes.** `jv` itself decides when a call needs confirming and writes the sentence; the model never words its own read-back. Nothing runs until you say "yes" within 8 seconds.
- **Injection taint.** Once a conversation has read outside text (a web page, a pane, a session's reply), actions that could carry that text into a Claude Code session ask again, and memory changes are refused.
- **Speaker check.** Optional: after you enrol your voice, a wake word from someone else is ignored.

In 17 days of daily use (643 turns), the voice brain asked for 196 actions that needed a check and 45 were refused; 96 read-backs were asked. Details and the threat model: [docs/safety.md](docs/safety.md).

## Requirements and honest limits

- **Linux only.** Needs tmux and PipeWire (`pw-record`, `pw-play`). Built and used daily on one Hyprland desktop; other setups are less tested.
- **Optional modules:** Hyprland (windows, workspaces), Zen browser with the add-on in `extension/` (tabs, search, video), Bluetooth, power profiles. Missing ones report "not available".
- **English only**, for both speech and phrase matching.
- **Claude Code internals.** Session status relies on Claude Code's hook events, transcript files and on-screen dialogs. These are not a stable API, so a Claude Code update can break status until heardback catches up; `heardback doctor` reports the version.
- **The wake word is yours to provide.** No wake model ships. Without one, use push-to-talk.
- **Not a general assistant.** It does what the `jv` verbs can do, plus spoken answers. See [docs/commands.md](docs/commands.md).

## Models and licences

heardback's code is MIT. No model is in this repository: `heardback-voice setup` downloads each one, pinned to a commit.

| Job | Model | Licence |
|---|---|---|
| Speech to text (GPU) | faster-whisper large-v3-turbo | MIT |
| Speech to text (CPU) | faster-distil-whisper-small.en | MIT |
| Text to speech | Kokoro-82M | Apache 2.0 |
| Voice activity | Silero VAD | MIT |
| Speaker check | SpeechBrain ECAPA (spkrec-ecapa-voxceleb) | Apache 2.0 |
| Wake word | openWakeWord (code) | Apache 2.0 |

openWakeWord's stock pre-trained wake models are **CC BY-NC-SA 4.0** (non-commercial). heardback never ships them; if you use one, it is downloaded on your machine at first run and its licence applies to you.

## Docs

- [Architecture](docs/architecture.md): one turn from microphone to speaker
- [Safety](docs/safety.md): threat model, what the gate blocks, numbers
- [Commands](docs/commands.md): every verb and its phrases
- [Configuration](docs/configuration.md): every config key
- [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Code of conduct](CODE_OF_CONDUCT.md)

heardback is not made by or affiliated with Anthropic. Claude and Claude Code are Anthropic's products.

## Licence

[MIT](LICENSE)
