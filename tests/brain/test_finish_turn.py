"""Outcome speech and confirmation ordering, with fake execution boundaries."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from brainutil import load_fixture
from heardback.brain import brain, catalogue, router
from heardback.brain.brain import Brain, Turn, act, closing


@pytest.mark.parametrize("state,reason", [("ran", None), ("failed", None)] +
                         [("denied", reason) for reason in brain.NOT_DONE])
@pytest.mark.parametrize("text", ["", "I closed it.", "I couldn't close it."])
@pytest.mark.parametrize("say", [None, "Tool's own reply."])
def test_closing_matrix(state, reason, text, say):
    turn = Turn(None, "close it", reply_text=text)
    turn.acts = [dict(id="call", name="tmux_close", state=state, reason=reason, say=say)]
    before = copy.deepcopy(turn.acts)
    lines = closing(turn)
    assert turn.acts == before
    if state == "ran":
        assert lines == [text or say or "Done."]
    elif state == "failed" and text:
        assert lines == [text]
    else:
        expected = [brain.NOT_DONE.get(reason, "I couldn't confirm that worked, so it isn't done.")]
        if text or say:
            expected.append(text or say)
        assert lines == expected


@pytest.mark.parametrize("text", ["", "Which window?", "Done."])
def test_closing_without_calls(text):
    assert closing(Turn(None, "hello", reply_text=text)) == [text or "I'm not sure that worked."]


def test_closing_partial_success_and_empty_ack():
    turn = Turn(None, "two actions", acts=[dict(state="ran", say="First closed."),
                                           dict(state="denied", reason="no")])
    assert closing(turn) == [brain.NOT_DONE["no"], "First closed."]
    turn.acts[0]["say"] = None
    assert closing(turn) == [brain.NOT_DONE["no"]]
    turn.acts = [dict(state="denied", reason="other", say="Done.")]
    turn.reply_text = "Okay."
    assert closing(turn) == [brain.NOT_DONE["other"]]


def test_closing_reports_each_denied_call():
    turn = Turn(None, "three actions", acts=[dict(state="ran"),
                                            dict(state="denied", reason="no"),
                                            dict(state="denied", reason="timeout")])
    assert closing(turn) == [brain.NOT_DONE["no"], brain.NOT_DONE["timeout"]]


def test_closing_retry_omits_stale_failed_say():
    turn = Turn(None, "focus a window", acts=[dict(state="failed", say="No window called foo."),
                                             dict(state="ran", say="Focused bar.")])
    assert closing(turn) == ["I couldn't confirm that worked, so it isn't done.", "Focused bar."]
    turn.reply_text = "Focused bar."
    assert closing(turn) == ["Focused bar."]


@pytest.mark.parametrize("reason", ["tainted", "preflight"])
@pytest.mark.parametrize("question", [False, True])
def test_streamed_summary_finishes_before_denial_reason(env, reason, question):
    async def go():
        b = Brain()
        b._bg = lambda coro: coro.close()
        b._write_stream = lambda turn: None
        b._bound = lambda *args: None
        b._revoke_execution = lambda turn: True
        b._send = AsyncMock()
        first = ("I couldn't start a lo-fi search in Zen, so I resumed Spotify instead, "
                 'and it\'s now playing "Kashish" by Ashish Bhatia.')
        last = "Want a different track?" if question else "Say the word if you want a different track."
        turn = Turn(None, "play lo-fi", tid="reply", sent=True, acts=[
            dict(id="play", name="zen_play", state="denied", reason=reason),
            dict(id="resume", name="media_media", state="ran", say="playing Kashish")])
        b.turns.append(turn)
        for chunk in (first + " ", last):
            await b._on_event({"type": "stream_event", "event": {
                "type": "content_block_delta", "delta": {"type": "text_delta", "text": chunk}}})
        await b._on_result(turn, dict(subtype="success", result=first + " " + last))
        spoken = [c.args[1] for c in b._send.await_args_list if c.args[1]["type"] == "say"]
        reasons = [brain.NOT_DONE[reason]] if reason == "tainted" else []
        assert [s["text"] for s in spoken] == ([first] + reasons + [last] if question else
                                               [first, last] + reasons)
        assert spoken[-1]["final"] and bool(spoken[-1].get("expect_reply")) == question
        assert not any(s.get("expect_reply") for s in spoken[:-1])
    asyncio.run(go())


@pytest.mark.parametrize("reason", ["no", "other", "timeout", "refused"])
def test_finish_denial_omits_read_only_say(env, reason):
    async def go():
        b = Brain()
        b.router = router.Router(load_fixture())
        b._send = AsyncMock()
        turn = Turn(None, "close window", terminal=True,
                    acts=[dict(name="status_list", state="ran", say="Stale session status."),
                          dict(name="tmux_close", state="denied", reason=reason)])
        await b.finish(turn)
        assert [call.args[1] for call in b._send.await_args_list] == [
            {"type": "say", "text": brain.NOT_DONE[reason], "final": True}, {"type": "done"}]
        turn.acts.pop()
        assert closing(turn, read_tools={"status_list"}) == ["Stale session status."]
    asyncio.run(go())


@pytest.mark.parametrize("state", ["ran", "failed"])
def test_streamed_answer_preserves_question(env, state):
    async def go():
        b = Brain()
        b._bg = lambda coro: coro.close()
        b._write_stream = lambda turn: None
        b._bound = lambda *args: None
        b._revoke_execution = lambda turn: True
        b._send = AsyncMock()
        turn = Turn(None, "status", tid="answer", sent=True)
        b.turns.append(turn)
        await b._on_event({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "status", "name": "mcp__jv__status_list"}]}})
        await b._on_event({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "status",
             "content": json.dumps({"ok": state == "ran", "say": "Tool's own reply."})}]}})
        text = ("heardback is waiting on a permission prompt. Want me to open it?" if state == "ran" else
                "I couldn't read the status. Try again?")
        first, question = text.split(". ")
        for chunk in (first + ". ", question):
            await b._on_event({"type": "stream_event", "event": {
                "type": "content_block_delta", "delta": {"type": "text_delta", "text": chunk}}})
        assert [call.args[1]["text"] for call in b._send.await_args_list] == [first + "."]
        await b._on_event({"type": "result", "subtype": "success", "result": text})
        messages = [call.args[1] for call in b._send.await_args_list]
        spoken = [m for m in messages if m["type"] == "say"]
        assert [m["text"] for m in spoken] == [first + ".", question]
        assert not spoken[0].get("expect_reply") and not spoken[0]["final"]
        assert spoken[1]["expect_reply"] and spoken[1]["final"]
        assert [m["type"] for m in messages] == ["say", "say", "done"]
        assert b.awaiting[None].source == "model" and b.awaiting[None].kind == "free"
    asyncio.run(go())


def test_result_error_after_tool_ran(env):
    async def go():
        b = Brain()
        b._bg = lambda coro: coro.close()
        b._write_stream = lambda turn: None
        b._bound = lambda *args: None
        b._revoke_execution = lambda turn: True
        b._send = AsyncMock()
        turn = Turn(None, "read status", sent=True, acts=[dict(state="ran", say="Read the status.")])
        b.turns.append(turn)
        await b._on_event({"type": "result", "subtype": "error_during_execution", "is_error": True})
        messages = [call.args[1] for call in b._send.await_args_list]
        assert [m["type"] for m in messages] == ["say", "done"]
        assert messages[0]["text"] == brain.UNFINISHED
    asyncio.run(go())


def test_closing_specific_failure_is_the_whole_reply():
    turn = Turn(None, "set two reminders", terminal="Nothing changed.",
                acts=[dict(state="failed", say="Nothing changed."),
                      dict(state="denied", reason="refused", say=brain.PERSONAL_SECOND)])
    assert closing(turn) == ["Nothing changed."]


def test_outcomes_are_per_call_and_denied_result_preserves_reason(env):
    async def go():
        b = Brain()
        b._bg = lambda coro: coro.close()
        turn = Turn(None, "close", tid="original", sent=True)
        b.turns.append(turn)
        for tool_id in ("first", "retry"):
            await b._on_event({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": tool_id, "name": "mcp__jv__tmux_close"}]}})
        act(turn, "first", "tmux_close").update(state="denied", reason="other")
        await b._on_event({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "first", "is_error": True, "content": brain.OTHER_ANSWER},
            {"type": "tool_result", "tool_use_id": "retry", "content": '{"ok":false,"say":"Failed."}'}]}})
        assert len(turn.acts) == 2
        assert turn.acts[0]["reason"] == "other" and turn.acts[0]["state"] == "denied"
        assert turn.acts[1]["state"] == "failed" and turn.acts[1]["say"] == "Failed."
    asyncio.run(go())


def sequence(env, monkeypatch, path, answers):
    """Exercise both real gates and finish; only Claude's parsing and jv are fake."""
    messages, outcomes, admitted = [], [], []
    answers = iter(answers)
    b = Brain(CONFIRM_TIMEOUT_S=0.005, CONFIRM_GRACE_S=0)
    proc = SimpleNamespace(respond=AsyncMock(), control=AsyncMock(), alive=True)
    b.claude = proc
    entry = load_fixture()["zen.close-unused"]
    b.router = router.Router({"zen.close-unused": entry})
    b._bg = lambda coro: coro.close()
    b._write_stream = lambda turn: None
    b._bound = lambda *args: None
    b._revoke_execution = lambda turn: True
    b.dry_run = AsyncMock(return_value=(3, "Close tabs?", None))
    monkeypatch.setattr(catalogue, "run_jv", AsyncMock(return_value=(0, '{"ok":true,"say":"Closed."}', "")))

    class Conn:
        def is_closing(self): return False
    conn = Conn()
    b.voice = conn

    async def send(_conn, msg):
        messages.append(msg)
        if msg["type"] != "confirm":
            return
        answer = next(answers)
        if answer == "timeout":
            return
        if answer == "interrupt":
            await b.interrupt()
            return
        await b.handle({"type": "confirm_reply", "confirmation_id": msg["confirmation_id"],
                        "ok": answer == "yes", "text": "" if answer == "unasked" else answer,
                        "speaker_ok": True, "unasked": answer == "unasked"}, conn)
    b._send = send
    original_finish = b.finish

    async def finish(turn, **kwargs):
        outcomes.append(turn)
        await original_finish(turn, **kwargs)
    b.finish = finish

    async def request(which, tid):
        if which == "fast":
            return await b._confirm_fast(router.Route(entry["name"], {}, 1), entry, conn,
                                         "close tabs", "Close tabs?", tid)
        turn = Turn(conn, "close tabs", tid=tid, sent=True)
        b.turns.append(turn)
        await b._on_event({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tid, "name": "mcp__jv__zen_close-unused"}]}})
        await b._can_use_tool(proc, turn, tid, {"tool_name": "mcp__jv__zen_close-unused",
                                               "tool_use_id": tid, "input": {}})
        allowed = proc.respond.await_args.args[1]["behavior"] == "allow"
        if allowed:
            admitted.append(tid)
        await b._on_event({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tid, "is_error": not allowed,
             "content": json.dumps({"ok": True, "say": "Closed."}) if allowed else "Denied."}]}})
        if turn.confirm_reason == "other":
            assert proc.respond.await_args.args[1]["message"] == brain.OTHER_ANSWER
        await b._on_result(turn, {"subtype": "success", "result": ""})

    async def followup(text, connection, tid, *, followup, parent):
        assert text.startswith('heardback asked: "Close tabs?".') and connection is conn
        assert 'Original request: "close tabs".' in text
        assert followup is True and parent == "initial"
        assert outcomes[0].finished
        assert not any(m["type"] == "done" for m in messages)
        await request("claude", "followup")
    b.to_claude = followup
    b.utterance = AsyncMock(side_effect=AssertionError("A correction must skip the router"))
    asyncio.run(request(path, "initial"))
    return messages, outcomes, admitted, catalogue.run_jv


@pytest.mark.parametrize("path", ["fast", "claude"])
@pytest.mark.parametrize("answer", ["yes", "no", "leave it", "timeout", "unasked"])
def test_path_answer_matrix(env, monkeypatch, path, answer):
    messages, turns, admitted, run = sequence(env, monkeypatch, path, [answer])
    assert [m["type"] for m in messages] == ["confirm", "say", "done"]
    state = "ran" if answer == "yes" else "denied"
    assert turns[0].acts[0]["state"] == state
    if answer == "yes":
        assert messages[1]["text"] == "Closed."
        assert bool(admitted) if path == "claude" else run.await_count == 1
    else:
        reason = "no" if answer == "leave it" else answer
        assert messages[1]["text"] == brain.NOT_DONE[reason]
        assert not admitted and not run.await_count


@pytest.mark.parametrize("path", ["fast", "claude"])
@pytest.mark.parametrize("other", ["only window 6", "just the first one", "open calculator", "Yes, close it."])
@pytest.mark.parametrize("followup", ["yes", "no", "timeout", "unasked", "interrupt"])
def test_other_followup_readback_before_done(env, monkeypatch, path, other, followup):
    messages, turns, admitted, run = sequence(env, monkeypatch, path, [other, followup])
    types = [m["type"] for m in messages]
    assert types == (["confirm", "say", "confirm", "done"] if followup == "interrupt" else
                     ["confirm", "say", "confirm", "say", "done"])
    assert messages[1]["text"] == brain.NOT_DONE["other"]
    assert messages[0]["confirmation_id"] != messages[2]["confirmation_id"]
    assert turns[0].acts[0]["state"] == "denied" and turns[0].acts[0]["reason"] == "other"
    assert admitted == (["followup"] if followup == "yes" else [])
    assert not run.await_count


def test_terminal_retry_stays_refused_and_tainted(env):
    async def go():
        b = Brain()
        proc = SimpleNamespace(respond=AsyncMock())
        b._tainted = proc
        b.dry_run = AsyncMock()
        turn = Turn(None, "original", terminal=True, tid="original")
        act(turn, "first", "tmux_close").update(state="denied", reason="other")
        await b._can_use_tool(proc, turn, "retry", {"tool_name": "mcp__jv__tmux_close",
                                                  "tool_use_id": "second", "input": {"target": "6"}})
        assert b._tainted is proc
        b.dry_run.assert_not_awaited()
        assert proc.respond.await_args.args[1]["behavior"] == "deny"
        assert [a["reason"] for a in turn.acts] == ["other", "refused"]
    asyncio.run(go())


def test_readback_other_during_interrupt_has_no_followup(env):
    async def go():
        b, conn = Brain(), object()
        b._send = AsyncMock()
        b._revoke_execution = lambda turn: True
        b.to_claude = AsyncMock()
        turn = Turn(conn, "close window 3", tid="initial", interrupted=True)
        outcome = act(turn, "close", "tmux_close")
        b.awaiting[conn] = brain.Awaiting(asyncio.get_running_loop().create_future(), True, "confirm",
                                        "Close window 3?", float("inf"), owner=turn, outcome=outcome)
        b._answer(conn, False, "no, window 4")
        await b.finish(turn)
        b.to_claude.assert_not_awaited()
        assert [call.args[1] for call in b._send.await_args_list] == [{"type": "done"}]
    asyncio.run(go())
