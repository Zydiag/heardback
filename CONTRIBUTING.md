# Contributing

Thanks for helping. Bug reports, fixes and new `jv` verbs are all welcome.

## Setup

You need Linux, Python 3.12+, [uv](https://docs.astral.sh/uv/) and tmux.

```sh
git clone https://github.com/Zydiag/heardback && cd heardback
uv sync                       # jv and the brain; enough for most changes
uv sync --extra voice-cpu     # add this only to work on the voice side
```

## Tests

```sh
scripts/test-full.sh          # the suite, isolated from your desktop: scratch config/state dirs, no GPU
scripts/test-full.sh tests/test_grants.py   # one file
```

The test fixtures keep tests off your live tmux server, Hyprland and Claude Code login: they use a private tmux socket and fakes. Tests that load real models are marked `models` and run only with `HEARDBACK_MODELS=1`.

## Pull requests

- **Keep them small:** one fix or one verb per PR. A short PR gets reviewed today; a large one waits.
- **Add a test** that fails without your change.
- **Safety first.** Any change to the gate, the allowlist, read-backs or taint (see [docs/safety.md](docs/safety.md)) needs a test that shows the old behaviour is still refused. A new verb taints by default; only add it to `CLEAN_TOOLS` if its output contains no outside text.
- **No new dependencies** for `jv`: it stays standard library only.
- If you change a verb's phrases or description, run `jv list` and check the output reads well.

## Review

A bot (Claude Code) reviews every PR and may label issues or ask for missing details. It comments only; it never merges. A maintainer reads every PR and merges it. If you disagree with the bot, say so in the thread: the maintainer decides.

## Reporting bugs

Open an issue with the bug template and paste the output of `heardback doctor`. Please don't paste your transcripts, trace files or what you said to the assistant; describe it instead.

Security problems go through [SECURITY.md](SECURITY.md), not issues.

By contributing you agree your work is released under the [MIT licence](LICENSE) and that you'll follow the [code of conduct](CODE_OF_CONDUCT.md).
