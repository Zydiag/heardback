import asyncio
import time

from brainutil import FAKE, pids, run_brain, says, serving, talk, utt
from heardback.brain.brain import Brain, sock_path


def test_token_reset(env):
    run_brain(env, [utt("cue:cost 0.001 40001"), utt("next")], claude_cmd=FAKE)
    p = pids(env)
    assert len(p) == 2 and p[0] != p[1]
    assert "reset reason=tokens" in (env / "state/heardback/brain.log").read_text()


def test_tokens_at_limit_no_reset(env):
    run_brain(env, [utt("cue:cost 0.001 40000"), utt("next")], claude_cmd=FAKE)
    assert len(set(pids(env))) == 1


def test_cost_reset(env):   # Raise the per-message bound to reach the cumulative one.
    run_brain(env, [utt("cue:cost 0.09 100") for _ in range(3)] + [utt("next")], claude_cmd=FAKE, MSG_COST_RESET=1)
    p = pids(env)
    assert len(set(p[:3])) == 1 and p[2] != p[3]   # 0.27 > 0.25 after the third


def test_message_cost_reset(env):
    out = run_brain(env, [utt("first"), utt("cue:cost 0.06 100"), utt("next")], claude_cmd=FAKE)
    p = pids(env)
    assert p[0] == p[1] and p[1] != p[2] and says(out)[1] == "You said: cue:cost 0.06 100."   # answer spoken before the reset
    assert "cost=0.0600" in (env / "state/heardback/brain.log").read_text()


def test_turn_reset(env):
    run_brain(env, [utt(f"t{i}") for i in range(31)], claude_cmd=FAKE, timeout=15)
    p = pids(env)
    assert len(p) == 31 and p[29] != p[30] and len(set(p[:30])) == 1


def test_idle_reset(env):
    async def go():
        b = Brain(claude_cmd=FAKE, IDLE_RESET_S=0.2)
        async with serving(b, sock_path()):
            await talk(sock_path(), [utt("one")])
            await asyncio.sleep(0.4)
            t0 = time.monotonic()
            out = await talk(sock_path(), [utt("two")])
            return out, time.monotonic() - t0
    out, dt = asyncio.run(go())
    p = pids(env)
    assert len(p) == 2 and p[0] != p[1] and says(out) == ["You said: two."] and dt < 1


def test_no_reset_below_limits(env):
    run_brain(env, [utt(f"plain {i}") for i in range(5)], claude_cmd=FAKE)
    assert len(pids(env)) == 5 and len(set(pids(env))) == 1
