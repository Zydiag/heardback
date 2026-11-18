## What and why

<!-- One fix or one verb per PR. Link the issue if there is one: Fixes #123 -->

## How I tested it

<!-- The test you added, and `scripts/test-full.sh` passing. For voice changes, what you tried by voice. -->

## Checklist

- [ ] A test that fails without this change
- [ ] `scripts/test-full.sh` passes
- [ ] If it touches the gate, allowlist, read-backs or taint: a test shows the old refusals still refuse ([docs/safety.md](../docs/safety.md))
- [ ] No new dependency for `jv`
- [ ] No personal data in code, tests or fixtures (names, paths, real utterances)
