import asyncio
import json

from brainutil import FAKE, control_requests, pids, run_brain, says, script, serving, talk, utt
from heardback.brain import brain


def _interrupts(env) -> int:
    return sum(r.get("subtype") == "interrupt" for r in control_requests(env))


def test_stop_interrupts(env):
    out = run_brain(env, [utt("cue:slow 5"), utt("stop")], claude_cmd=FAKE, gap=0.3)
    assert not [m for m in out if m["type"] == "say" and "You said" in m["text"]]
    assert _interrupts(env) == 1
    assert [m["type"] for m in out] == ["done", "done"]   # stop: done only; the interrupted turn: done, no text
    assert "reset" not in (env / "state/heardback/brain.log").read_text()   # our own interrupt is not an error


def test_interrupt_message(env):
    out = run_brain(env, [utt("cue:slow 5"), {"type": "interrupt", "t": 0}], claude_cmd=FAKE, gap=0.3)
    assert out == [{"type": "done"}] and _interrupts(env) == 1


def test_stuck_turn_killed(env):
    out = run_brain(env, [utt("cue:mute 5"), utt("stop"), utt("hi")], claude_cmd=FAKE, gap=0.3,
                    INTERRUPT_GRACE_S=0.3)
    assert says(out) == ["You said: hi."]
    p = pids(env)
    assert len(p) == 2 and p[0] != p[1]
    assert "reset reason=interrupt" in (env / "state/heardback/brain.log").read_text()


def test_same_process_after_interrupt(env):
    out = run_brain(env, [utt("cue:slow 5"), utt("stop"), utt("hi")], claude_cmd=FAKE, gap=0.3)
    assert says(out) == ["You said: hi."]
    assert len(set(pids(env))) == 1


def test_queued_turns_dropped(env):
    out = run_brain(env, [utt("cue:slow 5"), utt("tell me a joke"), utt("stop")], claude_cmd=FAKE, gap=0.2)
    assert says(out) == [] and [m["type"] for m in out] == ["done"] * 3
    assert len(pids(env)) == 1   # the queued joke never reached Claude


def test_interrupt_denies_pending_confirm(env):
    script(env, {"zen close-unused --dry-run": {"rc": 3, "stdout": '{"ok":false,"confirm":"Close 3 tabs?"}'}})
    b = brain.Brain(claude_cmd=FAKE)

    async def go():
        path = brain.sock_path()
        async with serving(b, path):
            task = asyncio.create_task(talk(path, [utt('cue:tool mcp__jv__zen_close-unused {"hours":24}')]))
            async with asyncio.timeout(3):
                while not b.awaiting:
                    await asyncio.sleep(0.01)
            await talk(path, [{"type": "interrupt", "t": 0}], timeout=0.3)
            return await task
    out = asyncio.run(go())
    assert not b.awaiting and any(m["type"] == "confirm" for m in out) and out[-1] == {"type": "done"}
    assert says(out) == []   # the deny reply of the interrupted turn is not spoken


def test_stop_handled_while_fast_path_runs(env):   # M1 review: interrupt no longer waits behind a jv call
    script(env, {"wall next": {"rc": 0, "stdout": "{}", "sleep": 1}})

    async def go():
        path = brain.sock_path()
        async with serving(brain.Brain(claude_cmd=FAKE), path):
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            out = await talk(path, [utt("next wallpaper"), utt("stop")], count=1, timeout=3, gap=0.3)
            return out, loop.time() - t0
    out, took = asyncio.run(go())
    assert out == [{"type": "done"}] and took < 0.8   # stop's done arrives before the slow jv call ends


def test_voice_disconnect_mid_turn(env):
    async def go():
        path = brain.sock_path()
        async with serving(brain.Brain(claude_cmd=FAKE), path):
            await talk(path, [utt("cue:slow 1")], timeout=0.2)
            return await talk(path, [utt("hello")])
    assert says(asyncio.run(go())) == ["You said: hello."]


# --- Task 19: slow turns, 90 s cap, concurrency --------------------------------------------------------------
CAP = "That was taking too long, so I stopped. I can hand it to a Claude session."


def test_working_on_it(env):
    out = run_brain(env, [utt("cue:slow 0.6")], claude_cmd=FAKE, WORKING_S=0.2)
    assert says(out) == ["Working on it.", "You said: cue:slow 0.6."]


def test_no_working_notice_when_quick(env):
    out = run_brain(env, [utt("hello")], claude_cmd=FAKE, WORKING_S=0.2)
    assert says(out) == ["You said: hello."]


def test_wall_cap(env):
    out = run_brain(env, [utt("cue:slow 5")], claude_cmd=FAKE, WALL_CAP_S=0.4)
    assert says(out) == [CAP] and out[-1] == {"type": "done"}
    assert _interrupts(env) == 1


def test_fast_path_during_claude_turn(env):
    out = run_brain(env, [utt("cue:slow 1"), utt("next wallpaper")], claude_cmd=FAKE)
    assert says(out) == ["Done.", "You said: cue:slow 1."]


def test_turn_gap(env):
    run_brain(env, [utt("one"), utt("two")], claude_cmd=FAKE, MIN_TURN_GAP_S=0.5)
    sends = [e["t"] for e in map(json.loads, (env / "claude.log").read_text().splitlines())
             if "in" in e and json.loads(e["in"]).get("type") == "user"]
    assert len(sends) == 2 and sends[1] - sends[0] >= 0.42   # fake's read time; 1st read lags while it starts


# --- M4 review fixes ------------------------------------------------------------------------------------------
def test_queued_utterance_never_answers_a_later_confirm(env):   # I1
    """"okay" is read (and queued behind a slow jv call) before Claude's confirm opens: it is a normal utterance."""
    script(env, {"wall next": {"rc": 0, "stdout": "{}", "sleep": 0.5},
                 "zen close-unused --dry-run": {"rc": 3, "stdout": '{"ok":false,"confirm":"Close 3 tabs?"}'}})
    b = brain.Brain(claude_cmd=FAKE)

    async def go():
        path = brain.sock_path()
        async with serving(b, path):
            msgs = [utt('cue:tool mcp__jv__zen_close-unused {"hours":24}'), utt("next wallpaper"), utt("okay")]
            out = await talk(path, msgs, count=2, timeout=1.5)   # wallpaper's done; the confirm is still open
            return out, bool(b.awaiting)
    out, open_after = asyncio.run(go())
    log = (env / "state/heardback/brain.log").read_text()
    assert open_after and "route=confirm" not in log and "route=claude verb=- model=haiku" in log.split("'okay'")[0]
    assert not [r for _, r in _responses(env) if r.get("behavior") == "allow"]


def _responses(env):
    from brainutil import _inputs
    return [(p, m["response"]["response"]) for p, m in _inputs(env) if m.get("type") == "control_response"
            and "response" in m["response"]]


def test_interrupted_turn_tool_denied_without_asking(env):   # I2
    from brainutil import jv_calls, load_fixture
    from heardback.brain import router
    b = brain.Brain(claude_cmd=FAKE)
    b.router = router.Router(load_fixture())
    sent = []

    class Proc:
        async def respond(self, rid, res):
            sent.append((rid, res))

    turn = brain.Turn(None, "x", sent=True, interrupted=True)
    asyncio.run(b._can_use_tool(Proc(), turn, "r1", {"tool_name": "mcp__jv__zen_close-unused", "input": {}}))
    assert sent == [("r1", {"behavior": "deny", "message": "heardback refused that tool call."})]
    assert jv_calls(env) == [] and not b.awaiting


def test_concurrent_resets_leave_one_process(env):   # I3
    b = brain.Brain(claude_cmd=FAKE)

    async def go():
        async with serving(b, brain.sock_path()):
            await asyncio.gather(b.reset("user"), b.reset("interrupt"), b.reset("idle"))
            return [p for p in b._procs if p.alive], b.claude
    alive, current = asyncio.run(go())
    assert alive == [current]


def test_drop_head_denies_its_confirm(env):   # M4 review minor 2: reset from another connection
    class Conn:   # still connected (a disconnect would deny on its own)
        def is_closing(self):
            return False

        def write(self, data):
            pass

        async def drain(self):
            pass

    async def go():
        b, conn = brain.Brain(), Conn()
        fut = asyncio.get_running_loop().create_future()
        turn = brain.Turn(conn, "x", sent=True)
        b.awaiting[conn] = brain.Awaiting(fut, True, "question", "Really?",
                                             asyncio.get_running_loop().time() + 60, turn)
        b.turns.append(turn)
        await b._drop_head()
        return fut.done() and fut.result() == (False, "") and not b.turns and not b.awaiting
    assert asyncio.run(go())
