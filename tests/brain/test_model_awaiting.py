"""Model questions share the slot without sharing read-back approval."""
import asyncio
import time
from unittest.mock import AsyncMock, Mock

import pytest

from brainutil import FAKE, load_fixture, pids, run_brain, says, utt
from heardback.brain import brain, catalogue, router
from heardback.brain.brain import Awaiting, Brain, Turn, answer_for


@pytest.fixture
def rig(env, monkeypatch):
    b, conn = Brain(MIN_TURN_GAP_S=0), Mock()
    conn.is_closing.return_value = False
    b.voice = conn
    b.router = router.Router(load_fixture())
    monkeypatch.setattr(catalogue, "get", lambda: b.router.cat)
    b._send = AsyncMock()
    b._bg = lambda coro: coro.close()
    b._bound = lambda *args: None
    b._write_stream = lambda turn: None
    b._revoke_execution = lambda turn: True
    b.to_claude, b.run_route = AsyncMock(), AsyncMock()
    return b, conn


async def question(b, conn):
    turn = Turn(conn, "read the status", tid="question", sent=True)
    b.turns.append(turn)
    await b._on_result(turn, dict(subtype="success", result="Want me to open it?"))
    return b.awaiting[conn]


@pytest.mark.parametrize("text,expected", [("yes", "model"), ("stop", "new"), ("start over", "new")])
def test_answer_for_is_pure(text, expected):
    aw = Awaiting(None, True, "id", "Open it?", 123, kind="free", source="model")
    before = vars(aw).copy()
    assert answer_for(aw, text) == expected and vars(aw) == before
    aw.kind = "confirm"
    assert answer_for(aw, "yes") == "new"


@pytest.mark.parametrize("text,fast", [("yes", False), ("switch tmux window 3", False), ("next wallpaper", True)])
def test_model_answer_routes_and_traces(rig, monkeypatch, text, fast):
    b, conn = rig
    events = []
    monkeypatch.setattr(brain.trace, "emit", lambda *a, **kw: events.append((a[1], kw)))
    async def go():
        aw = await question(b, conn)
        assert aw.kind == "free" and aw.source == "model" and aw.original == "read the status"
        await b.handle({**utt(text), "turn": "answer", "parent": "question"}, conn)
        assert conn not in b.awaiting
        if fast:
            assert b.run_route.await_args.args[0].verb == "wall.next"
            b.to_claude.assert_not_awaited()
        else:
            b.to_claude.assert_awaited_once_with(text, conn, "answer", followup=True, parent="question")
            b.run_route.assert_not_awaited()
    asyncio.run(go())
    assert [(ev, data.get("result", data.get("why"))) for ev, data in events if ev.startswith("awaiting_")] == [
        ("awaiting_set", None), ("awaiting_answer", "model"), ("awaiting_cleared", "answered")]


@pytest.mark.parametrize("expired,parent", [(False, None), (True, "question")])
def test_new_wake_and_expired_answer_route_normally(rig, expired, parent):
    b, conn = rig
    async def go():
        aw = await question(b, conn)
        aw.expires -= 31 if expired else 5
        await b.handle({**utt("next wallpaper"), "parent": parent}, conn)
        assert conn not in b.awaiting and b.run_route.await_count == 1
        b.to_claude.assert_not_awaited()
    asyncio.run(go())


def test_expired_answer_to_model_is_not_a_followup(rig):
    b, conn = rig
    async def go():
        aw = await question(b, conn)
        aw.expires -= 31
        await b.utterance("tell me a joke", True, conn, "late", "question")
        b.to_claude.assert_awaited_once_with("tell me a joke", conn, "late", followup=False, parent="question")
    asyncio.run(go())


def test_quoted_reply_does_not_correct_or_listen(rig):
    b, conn = rig
    text = ('I sent your selected text to window 3 with the question, '
            '"I have saved this note, why didn\'t it appear?", and pressed Enter.')
    async def go():
        for tid in ("first", "second"):
            turn = Turn(conn, "send my selected text to window 3", tid=tid, sent=True)
            b.turns.append(turn)
            await b._on_result(turn, dict(subtype="success", result=text))
            assert conn not in b.awaiting
        spoken = [c.args[1] for c in b._send.await_args_list if c.args[1]["type"] == "say"]
        assert " ".join(s["text"] for s in spoken) == f"{text} {text}"
        assert not any(s.get("expect_reply") for s in spoken)
    asyncio.run(go())


@pytest.mark.parametrize("text", ["stop", "start over"])
def test_builtins_clear_model_slot(rig, text):
    b, conn = rig
    async def go():
        await question(b, conn)
        await b.handle({**utt(text), "parent": "question"}, conn)
        assert not b.awaiting
        b.to_claude.assert_not_awaited()
        b.run_route.assert_not_awaited()
    asyncio.run(go())


def test_timeout_is_silent_and_releases_deferred_reset(rig):
    b, conn = rig
    b.AWAIT_S = .02
    b._bg = asyncio.create_task
    b.reset = AsyncMock()
    async def go():
        await question(b, conn)
        b._followup_reset = "tokens"
        before = b._send.await_count
        await asyncio.sleep(.04)
        assert not b.awaiting and b._send.await_count == before
        b.reset.assert_awaited_once_with("tokens")
    asyncio.run(go())


def test_finish_refreshes_expiry(rig):
    b, conn = rig
    async def go():
        aw = await question(b, conn)
        aw.expires = time.monotonic() + .01
        await b.finish(aw.owner)
        assert aw.expires - time.monotonic() > 29
    asyncio.run(go())


def test_confirm_replaces_model_and_ignores_its_delayed_answers(rig, monkeypatch):
    b, conn = rig
    cleared = []
    def emit(component, event, *args, **kw):
        if event == "awaiting_cleared":
            cleared.append(kw["why"])
    monkeypatch.setattr(brain.trace, "emit", emit)
    async def go():
        model = await question(b, conn)
        task = asyncio.create_task(b.ask(conn, "Close the tab?", True, owner=Turn(conn, "close it", tid="readback")))
        await asyncio.sleep(0)
        confirm = b.awaiting[conn]
        assert confirm.kind == "confirm" and cleared == ["replaced"]
        await b.handle(dict(type="confirm_reply", confirmation_id=model.id, ok=True, text="yes", speaker_ok=True), conn)
        await b.handle({**utt("yes"), "parent": "question"}, conn)
        assert b.awaiting[conn] is confirm and not task.done()
        assert not b._set_awaiting(conn, model)
        await b.handle(dict(type="confirm_reply", confirmation_id=confirm.id, ok=True, text="yes", speaker_ok=True), conn)
        assert await task == (True, "yes")
    asyncio.run(go())


def test_confirm_reply_never_answers_model_slot(rig):
    b, conn = rig
    async def go():
        aw = await question(b, conn)
        await b.handle(dict(type="confirm_reply", confirmation_id=aw.id, ok=True, text="yes", speaker_ok=True), conn)
        assert b.awaiting[conn] is aw
        b.to_claude.assert_not_awaited()
    asyncio.run(go())


def test_socket_model_answer_reaches_claude(env):
    out = run_brain(env, [utt("cue:ask"), {**utt("yes"), "parent": "question"}], claude_cmd=FAKE, gap=.3)
    assert says(out) == ["Which one?", "You said: yes."]
    assert len(set(pids(env))) == 1
