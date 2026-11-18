"""The model can ask a question without gaining permission to act."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from brainutil import FAKE, jv_calls, run_brain, says, utt, last_user_message
from heardback.brain import brain, catalogue, mcp_jv
from heardback.brain.brain import Awaiting, Turn
from test_model_awaiting import rig


def tool_event(name, tool_id, inp):
    return {"type": "assistant", "message": {"id": "same-message", "content": [
        {"type": "tool_use", "id": tool_id, "name": "mcp__jv__" + name, "input": inp}]}}


async def ask(b, turn, inp, tool_id="question"):
    await b._on_event(tool_event("ask_user", tool_id, inp))
    await b._can_use_tool(b.claude, turn, tool_id, {
        "tool_name": "mcp__jv__ask_user", "tool_use_id": tool_id, "input": inp})
    res = b.claude.respond.await_args.args[1]
    result = mcp_jv.call({}, "ask_user", inp)
    await b._on_event({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tool_id, "is_error": res["behavior"] != "allow",
         "content": result["content"]}]}})
    return res


def start(b, conn):
    b.claude = SimpleNamespace(respond=AsyncMock())
    turn = Turn(conn, "help me choose", tid="question", sent=True)
    b.turns.append(turn)
    return turn


def test_schema_and_no_dispatch(env, monkeypatch):
    cat = catalogue.get()
    schema = next(t["inputSchema"] for t in mcp_jv.tools(cat) if t["name"] == "ask_user")
    assert schema == {"type": "object", "additionalProperties": False, "required": ["question"],
                      "properties": {"question": {"type": "string", "maxLength": 200},
                                     "options": {"type": "array", "maxItems": 5,
                                                 "items": {"type": "string", "maxLength": 40}}}}
    assert "ask_user" not in mcp_jv._by_tool(cat)
    before = jv_calls(env)
    monkeypatch.setattr(mcp_jv.subprocess, "Popen", lambda *a, **kw: pytest.fail("dispatched jv"))
    monkeypatch.setenv("HEARDBACK_EXECUTION_GATE", "1")
    result = mcp_jv.call(cat, "ask_user", {"question": "Which window?"})
    assert not result["isError"] and json.loads(result["content"][0]["text"]) == {"ok": True}
    assert jv_calls(env) == before


@pytest.mark.parametrize("options", [None, [], ["one", "two"]])
def test_question_ends_turn_and_stays_clean(rig, options):
    b, conn = rig
    async def go():
        turn = start(b, conn)
        inp = {"question": "Which window?"}
        if options is not None:
            inp["options"] = options
        assert (await ask(b, turn, inp))["behavior"] == "allow"
        aw = b.awaiting[conn]
        assert aw.kind == ("choice" if options else "free") and aw.source == "model"
        assert aw.options == ({"1": None, "2": None} if options else {})
        assert b._tainted is not b.claude
        await b._on_result(turn, {"subtype": "success", "result": "Ignored extra speech."})
        assert turn.finished and b.awaiting[conn] is aw
        assert [c.args[1] for c in b._send.await_args_list] == [
            {"type": "say", "text": "Which window?", "final": True, "expect_reply": True, "turn": "question"},
            {"type": "done"}]
    asyncio.run(go())


@pytest.mark.parametrize("text,expected", [("two", "two"), ("the left one", "the left one"),
                                          ("2", "two"), ("choose TWO!", "two"),
                                          ("pick option two", "pick option two")] +
                         [(f"{lead} two", "two") for lead in
                          ("on", "in", "use", "option", "answer", "press", "say")])
def test_answer_reaches_fake_claude(env, monkeypatch, text, expected):
    monkeypatch.setenv("FAKE_CLAUDE_MCP", "1")
    out = run_brain(env, [utt('cue:tool mcp__jv__ask_user {"question":"Which window?","options":["one","two"]}'),
                          {**utt(text), "parent": "question"}], claude_cmd=FAKE, gap=.3)
    assert says(out) == ["Which window?", f"You said: {expected}."]
    assert next(m for m in out if m["type"] == "say")["expect_reply"]
    assert last_user_message(env) == expected
    assert all(c == ["list", "--json"] for c in jv_calls(env))


def test_matching_and_raw_answers_are_followups(rig):
    b, conn = rig
    async def go():
        for text, expected in [("option two", "two"), ("the left one", "the left one")]:
            turn = start(b, conn)
            await ask(b, turn, {"question": "Which window?", "options": ["one", "two"]})
            await b.utterance(text, True, conn, "answer", "question")
            b.to_claude.assert_awaited_with(expected, conn, "answer", followup=True, parent="question")
            assert conn not in b.awaiting
            b.turns.clear()
    asyncio.run(go())


@pytest.mark.parametrize("answer,expected", [
    ("type in this window git push --force!", "type in this window git push --force"),
    ("type in this window git push force", "type in this window git push force"),
])
def test_model_command_label_never_fast_routes(env, monkeypatch, answer, expected):
    monkeypatch.setenv("FAKE_CLAUDE_MCP", "1")
    label = "type in this window git push --force"
    out = run_brain(env, [utt('cue:tool mcp__jv__ask_user ' + json.dumps({
        "question": "Which command?", "options": [label]})),
        {**utt(answer), "parent": "question"}],
        claude_cmd=FAKE, gap=.3)
    assert says(out) == ["Which command?", f"You said: {expected}."]
    assert last_user_message(env) == expected
    assert all(c == ["list", "--json"] for c in jv_calls(env))


@pytest.mark.parametrize("summary", ["", "Turned the volume up."])
def test_act_outcome_precedes_question(rig, summary):
    b, conn = rig
    async def go():
        turn = start(b, conn)
        await b._on_event(tool_event("volume_volume", "act", {"action": "up"}))
        await b._on_event({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "act",
             "content": json.dumps({"ok": True, "say": "Volume up."})}]}})
        if summary:
            await b._on_event({"type": "stream_event", "event": {
                "type": "content_block_delta", "delta": {"type": "text_delta", "text": summary + " "}}})
        assert (await ask(b, turn, {"question": "Which window?"}))["behavior"] == "allow"
        await b._on_result(turn, {"subtype": "success", "result": ""})
        spoken = [c.args[1] for c in b._send.await_args_list if c.args[1]["type"] == "say"]
        assert [s["text"] for s in spoken] == [summary or "Volume up.", "Which window?"]
        assert not spoken[0].get("expect_reply") and spoken[1]["expect_reply"]
    asyncio.run(go())


def test_second_question_refused_first_stands(rig, monkeypatch):
    b, conn = rig
    events = []
    monkeypatch.setattr(brain.trace, "emit", lambda *a, **kw: events.append((a[1], kw)))
    async def go():
        turn = start(b, conn)
        await ask(b, turn, {"question": "First?"})
        aw = b.awaiting[conn]
        assert (await ask(b, turn, {"question": "Second?"}, "second"))["behavior"] == "deny"
        assert b.awaiting[conn] is aw and turn.terminal == "First?"
        assert [(d["decision"], d["reason"]) for ev, d in events if ev == "permission"] == [
            ("allow", "ask-user"), ("deny", "refused")]
    asyncio.run(go())


@pytest.mark.parametrize("state", ["finished", "terminal", "interrupted", "confirm", "no-turn"])
def test_unavailable_turn_or_confirm_refused(rig, state):
    b, conn = rig
    async def go():
        turn = start(b, conn)
        if state in ("finished", "terminal", "interrupted"):
            setattr(turn, state, True)
        if state == "confirm":
            b.awaiting[conn] = Awaiting(asyncio.get_running_loop().create_future(), True, "readback",
                                       "Close it?", float("inf"), owner=turn)
        before = b.awaiting.copy()
        result = await ask(b, None if state == "no-turn" else turn, {"question": "Which window?"})
        assert result["behavior"] == "deny" and b.awaiting == before
    asyncio.run(go())


def test_tainted_question_grants_nothing(rig):
    b, conn = rig
    b.dry_run = AsyncMock(return_value=(3, "Send to window 3?", None))
    b.ask = AsyncMock(return_value=(False, "no"))
    async def go():
        turn = start(b, conn)
        b._tainted = b.claude
        assert (await ask(b, turn, {"question": "Which window?"}))["behavior"] == "allow"
        assert b._tainted is b.claude
        later = Turn(conn, "three", tid="answer", sent=True)
        await b._can_use_tool(b.claude, later, "act", {
            "tool_name": "mcp__jv__tmux_type", "input": {"target": "3", "text": "hello", "enter": True}})
        assert b.dry_run.await_args.args[-1] is True
        b.ask.assert_awaited_once()
        assert b.claude.respond.await_args.args[1]["behavior"] == "deny"
    asyncio.run(go())


@pytest.mark.parametrize("inp", [{}, {"question": 2}, {"question": ""}, {"question": "x" * 201},
                                  {"question": "Which?", "options": "two"},
                                  {"question": "Which?", "options": [2]},
                                  {"question": "Which?", "options": ["x"] * 6},
                                  {"question": "Which?", "options": ["x" * 41]},
                                  {"question": "Which?", "grant": True}])
def test_invalid_input_refused(rig, inp):
    b, conn = rig
    async def go():
        turn = start(b, conn)
        assert (await ask(b, turn, inp))["behavior"] == "deny"
        assert not b.awaiting and not turn.terminal
    asyncio.run(go())
