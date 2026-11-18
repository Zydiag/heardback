# Security

heardback lets a voice and a language model act on your desktop, so a way around its gate is a real bug. Thank you for looking.

## Reporting

Please report privately through GitHub: this repository's **Security** tab, then **Report a vulnerability**. Don't open a public issue for it.

Useful in a report: what you said or what text the model saw, what ran, and what you expected the gate to do. A `jv … --dry-run` command that reproduces it is ideal. Leave out anything personal from your own logs.

We aim to reply within a week. Fixes are released as soon as they're ready, and you're credited in the release notes unless you'd rather not be.

## In scope

- Anything that makes a `jv` verb run without the read-back and spoken yes it should need.
- Text from a web page, a tmux pane, a Claude Code session's reply or any other outside source that gets an action through without the taint check.
- A way for the voice brain's Claude to run a shell command, read or write files, or call a tool outside the `jv` allowlist.
- Reusing, forging or replaying an approval grant.
- heardback reading, logging or sending your Claude Code credentials.

## Out of scope

- Attacks that need a shell on the machine already (they can run `jv` directly).
- What a Claude Code session you drive by voice does under its own permission mode.
- Speaker-check false accepts: the check is a convenience, documented as weak on short words. See [docs/safety.md](docs/safety.md).

## Supported versions

Only the latest release gets fixes.
