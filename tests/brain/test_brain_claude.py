import asyncio
import json

from brainutil import FAKE, pids, run_brain, says, serving, talk, utt


def test_claude_turn_streams(env):
    out = run_brain(env, [utt("tell me a joke")], claude_cmd=FAKE)
    assert out[-1] == {"type": "done"}
    assert " ".join(m["text"] for m in out if m["type"] == "say") == "You said: tell me a joke."
    assert [m["final"] for m in out if m["type"] == "say"] == [True]   # "You said: tell me a joke." is the leftover


def test_sentences_stream_before_result(env):
    out = run_brain(env, [utt("cue:ask")], claude_cmd=FAKE)
    sayings = [m for m in out if m["type"] == "say"]
    assert sayings[0].pop("turn").endswith("-b1")
    assert sayings == [{"type": "say", "text": "Which one?", "final": True,
                                                         "expect_reply": True}]


def test_foreign_tool_kills_claude(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_TOOLS", "Bash,mcp__jv__wall_next")
    out = run_brain(env, [utt("tell me a joke"), utt("tell me another")], claude_cmd=FAKE)
    assert says(out) == ["Claude is off; simple commands still work."] * 2
    assert [m["type"] for m in out].count("done") == 2


def test_back_to_back_turns_wait_for_result(env):   # R4: one Claude message in flight at a time
    out = run_brain(env, [utt("cue:slow 0.3"), utt("second one")], claude_cmd=FAKE)
    assert says(out) == ["You said: cue:slow 0.3.", "You said: second one."]
    sends = [e["t"] for e in map(json.loads, (env / "claude.log").read_text().splitlines()) if "in" in e]
    assert len(sends) == 2 and sends[1] - sends[0] >= 0.3


def test_fast_path_still_works_with_claude(env):
    out = run_brain(env, [utt("next wallpaper")], claude_cmd=FAKE)
    assert says(out) == ["Done."] and pids(env) == []


def test_stream_log_written(env):
    run_brain(env, [utt("hello")], claude_cmd=FAKE)
    lines = [json.loads(l) for l in (env / "state/heardback/brain-stream.jsonl").read_text().splitlines()]
    assert [e["type"] for e in lines] == ["system", "stream_event", "stream_event", "assistant", "result"]


def test_claude_env_and_cwd(env):
    run_brain(env, [utt("hello")], claude_cmd=FAKE)
    first = json.loads((env / "claude.log").read_text().splitlines()[0])
    assert "--restricted" in first["argv"] and "--bare" not in first["argv"]
    assert (env / "state/heardback/brain-cwd").is_dir()


def test_crash_mid_turn_respawns(env):
    out = run_brain(env, [utt("cue:crash"), utt("hello")], claude_cmd=FAKE, BACKOFF_S=0.01)
    assert "Claude restarted, please say that again." in says(out)
    assert "You said: hello." in says(out)
    assert len(set(pids(env))) == 2


def test_auth_error(env):
    out = run_brain(env, [utt("cue:auth")], claude_cmd=FAKE)
    assert says(out) == ["Claude isn't signed in; simple commands still work."] and out[-1] == {"type": "done"}


def test_max_turns(env):
    out = run_brain(env, [utt("cue:maxturns"), utt("hello")], claude_cmd=FAKE)
    assert says(out) == ["I couldn't finish that.", "You said: hello."]
    assert len(set(pids(env))) == 1   # error_max_turns does not reset


def test_other_error_resets(env):
    out = run_brain(env, [utt("cue:error error_max_budget_usd"), utt("hello")], claude_cmd=FAKE)
    assert says(out) == ["I couldn't finish that.", "You said: hello."]
    p = pids(env)
    assert len(p) == 2 and p[0] != p[1]


def test_result_counts_usage(env):
    run_brain(env, [utt("hello"), utt("again")], claude_cmd=FAKE)
    u = json.loads((env / "state/heardback/usage.json").read_text())
    assert u["turns"] == 2 and u["by_model"] == {"haiku": 2}


def test_backoff_doubles_and_resets(env):
    from heardback.brain.brain import Brain
    from heardback.brain.brain import sock_path
    b = Brain(claude_cmd=FAKE, BACKOFF_S=0.01)

    async def go():
        async with serving(b, sock_path()):
            await talk(sock_path(), [utt("cue:crash"), utt("cue:crash")], count=2)
            await asyncio.sleep(0.1)   # second respawn done
            doubled = b.backoff
            await talk(sock_path(), [utt("hello")])
            return doubled, b.backoff
    assert asyncio.run(go()) == (0.04, 0.01)   # 0.01 → 0.02 → 0.04; a result resets it


def test_subagent_text_not_spoken(env):
    import asyncio as aio
    from heardback.brain.brain import Brain, Turn
    b = Brain(claude_cmd=FAKE)
    sent = []

    async def go():
        async def say(conn, text, final=False, **kw):
            sent.append(text)
        b.say = say
        b.turns.append(Turn(None, "x", sent=True))
        d = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Inner. "}}
        await b._on_event({"type": "stream_event", "event": d, "parent_tool_use_id": "toolu_1"})
        await b._on_event({"type": "stream_event", "event": {**d, "delta": {**d["delta"], "text": "Outer. "}}})
    aio.run(go())
    assert sent == ["Outer."]


def test_live_capture_speaks_once(env):   # Task 11: the real stream shape, replayed through the parser
    import asyncio as aio
    from brainutil import FIX
    from heardback.brain.brain import Brain, Turn
    lines = [json.loads(l) for l in (FIX / "stream_live.ndjson").read_text().splitlines()]
    turn = lines[1:lines.index(next(e for e in lines if e.get("type") == "result")) + 1]
    b = Brain(claude_cmd=FAKE)
    sent = []

    async def go():
        async def say(conn, text, final=False, **kw):
            sent.append((text, final))
        async def nothing(*a, **k):
            pass
        b.say, b.done, b._kick = say, nothing, nothing
        b.turns.append(Turn(None, "say ok", sent=True))
        for e in turn:
            await b._on_event(e)
    aio.run(go())
    assert sent == [("Ok.", True)] and not b.turns and not b.claude_off


def test_unstreamed_result_capped(env):   # M2 review: the no-delta fallback goes through a splitter
    import asyncio as aio
    from heardback.brain.brain import Brain, Turn
    b = Brain(claude_cmd=FAKE)
    sent = []

    async def go():
        async def say(conn, text, final=False, **kw):
            sent.append((text, final))
        async def nothing(*a, **k):
            pass
        b.say, b.done, b._kick = say, nothing, nothing
        b.turns.append(Turn(None, "x", sent=True))
        await b._on_event({"type": "result", "subtype": "success", "is_error": False, "result": "word " * 100})
    aio.run(go())
    assert len(sent) == 3 and all(len(t) <= 200 for t, _ in sent) and [f for _, f in sent] == [False, False, True]


def test_bad_usage_file_keeps_queue_moving(env):   # M2 review: bookkeeping failure must not wedge the FIFO
    from datetime import date
    (env / "state/heardback").mkdir(mode=0o700, parents=True, exist_ok=True)
    (env / "state/heardback/usage.json").write_text(json.dumps({"date": date.today().isoformat(), "turns": None,
                                                             "by_model": []}))
    out = run_brain(env, [utt("one"), utt("two")], claude_cmd=FAKE)
    assert says(out) == ["You said: one.", "You said: two."]
    assert json.loads((env / "state/heardback/usage.json").read_text())["turns"] == 2


def test_bookkeeping_error_still_kicks(env, monkeypatch):
    from heardback.brain import brain as brain_mod
    def boom(*a):
        raise RuntimeError("x")
    monkeypatch.setattr(brain_mod.Brain, "_bound", boom)
    out = run_brain(env, [utt("one"), utt("two")], claude_cmd=FAKE)
    assert [m["type"] for m in out].count("done") == 2 and "You said: two." in says(out)


def test_crash_clears_process_counters(env):   # M2 review: the new process starts its own count
    out = run_brain(env, [utt("t0"), utt("t1"), utt("cue:crash"), utt("alpha"), utt("beta")], claude_cmd=FAKE,
              BACKOFF_S=0.01, TURN_RESET=3)
    p = pids(env)
    assert len(p) == 5 and len(set(p[:3])) == 1 and p[3] == p[4] != p[0], (out, p)


# --- Task 14: jv tools + MCP health -------------------------------------------
def test_no_tools_are_preallowed():
    from brainutil import load_fixture
    from heardback.brain.brain import Brain
    assert Brain._allowed(load_fixture()) == []


def _argv(env) -> list[list[str]]:
    return [e["argv"] for e in map(json.loads, (env / "claude.log").read_text().splitlines()) if "argv" in e]


def test_cmd_has_mcp(env):
    import sys
    from brainutil import load_fixture
    from heardback.brain.brain import Brain
    run_brain(env, [utt("hello")], claude_cmd=FAKE)
    argv = _argv(env)[0]
    assert argv[argv.index("--permission-prompt-tool") + 1] == "stdio"
    assert argv[argv.index("--allowedTools") + 1] == ",".join(Brain()._allowed(load_fixture()))
    mcp = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]["jv"]
    assert mcp["command"] == sys.executable and mcp["args"] == ["-m", "heardback.brain.mcp_jv"]


def test_no_catalogue_runs_without_tools(env, monkeypatch):   # no jv at all: Claude still answers (spec §5)
    monkeypatch.setenv("PATH", "/nonexistent")
    from heardback.brain import catalogue
    monkeypatch.setattr(catalogue, "_fetch", lambda: (_ for _ in ()).throw(OSError("no jv")))
    out = run_brain(env, [utt("hello there friend how are you")], claude_cmd=FAKE)
    assert says(out) == ["You said: hello there friend how are you."]
    assert "--mcp-config" not in _argv(env)[0]


def test_mcp_missing_twice_turns_off(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_TOOLS", "")
    out = run_brain(env, [utt("hi"), utt("hi again")], claude_cmd=FAKE)
    assert says(out).count("Claude's tools failed to load; simple commands still work.") == 1
    assert says(out) == ["Claude's tools failed to load; simple commands still work.",
                         "Claude is off; simple commands still work."]
    assert len(set(pids(env))) == 2   # sent once to each of the two processes


def test_mcp_missing_once_respawns(env, monkeypatch, tmp_path):
    flag = tmp_path / "first"
    flag.write_text("")
    # fake lists no tools only while the flag exists; the wrapper removes it after the first spawn
    wrapper = tmp_path / "claude.sh"
    wrapper.write_text(f'#!/bin/sh\nif [ -e {flag} ]; then rm {flag}; export FAKE_CLAUDE_TOOLS=""; fi\n'
                       f'exec {" ".join(FAKE)} "$@"\n')
    wrapper.chmod(0o755)
    out = run_brain(env, [utt("hi")], claude_cmd=[str(wrapper)])
    assert says(out) == ["You said: hi."] and len(set(pids(env))) == 2


