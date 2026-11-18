"""Local personal phrases precede navigation gates and keep recent arguments private."""
import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest

from heardback.brain import brain, router
from jv.cli import all_verbs, catalogue


@pytest.fixture
def cat():
    return {v["name"]: v for v in catalogue(all_verbs())["verbs"] if v["voice"]}


@pytest.mark.parametrize("raw,name,text", [
    ("remind me to read in the background in 5 minutes", "reminder.add", "to read in the background in 5 minutes"),
    ("remind me to read and write in 5 minutes", "reminder.add", "to read and write in 5 minutes"),
    ("capture " + "word " * 25, "inbox.add", "word " * 24 + "word"),
    ("note that in window 3 keep this", "inbox.add", "in window 3 keep this"),
    ("note that the wifi is down", "inbox.add", "wifi is down"),
    ("capture screen protector order", "inbox.add", "screen protector order"),
    ("set a reminder to read at 6", "reminder.add", "to read at 6"),
    ("cancel my reminder read", "reminder.cancel", "read"),
    ("delete reminder read", "reminder.cancel", "read"),
    ("cancel my stretch reminder", "reminder.cancel", "stretch"),
    ("delete my stretch reminder", "reminder.cancel", "stretch"),
    ("cancel the drink water reminder", "reminder.cancel", "drink water"),
    ("delete the stretch reminder.", "reminder.cancel", "stretch"),
])
def test_personal_text(cat, raw, name, text):
    hit = router.Router(cat).route(raw)
    assert hit and hit.verb == name and hit.args["text"] == text


@pytest.mark.parametrize("raw,name", [
    ("capture the screen", "shot"),
    ("list my reminders", "reminder.list"), ("what are my reminders", "reminder.list"),
    ("read my notes", "inbox.list"), ("cancel the reminder", "reminder.cancel"),
    ("delete the reminder", "reminder.cancel"), ("cancel that reminder", "reminder.cancel"),
    ("delete that", "tmux.clear"), ("clear that", "tmux.clear"),
    ("status of my reminders", "status.get"), ("ask claude to remind me to read at 6", "zen.ask"),
    ("catch me up", "catchup"),
])
def test_literal_phrases(cat, raw, name):
    assert router.Router(cat).route(raw).verb == name


@pytest.mark.parametrize("raw", ["remember to call a friend at 6", "don't let me forget to read"])
def test_odd_phrasing_still_hands_off(cat, raw):
    assert router.Router(cat).route(raw) is None


def test_yield_literal_user_action(cat):
    cat["do.stretch"] = {"params": {}, "argv": [], "args": {}, "confirm": False,
                         "phrases": ["remind me to stretch"]}
    assert router.Router(cat).route("remind me to stretch").verb == "do.stretch"
    cat["do.notes"] = {"params": {}, "argv": [], "args": {}, "confirm": False,
                       "phrases": ["read my notes"]}
    assert router.Router(cat).route("read my notes").verb == "do.notes"


def test_personal_args_absent_from_prefix(cat):
    b = brain.Brain()
    b.router = router.Router(cat)
    b.recent.append((time.time(), "inbox.add", {"text": "private example", "request_key": "secret-key"}, True))
    b.unshown = 1
    prefixed = b.prefix("why")
    assert "private example" not in prefixed and "secret-key" not in prefixed
    assert "inbox add" in prefixed and "ok" in prefixed


@pytest.mark.parametrize("off,limited,handoff", [(False, False, True), (True, False, False), (False, True, False)])
def test_no_time_handoff(cat, off, limited, handoff):
    b = brain.Brain(claude_cmd=["fake"])
    b.router = router.Router(cat)
    b.claude_off = "missing" if off else None
    b.rate.claude_allowed = lambda _: not limited
    b.to_claude, b.say, b.done = AsyncMock(), AsyncMock(), AsyncMock()
    asyncio.run(b._speak_jv(router.Route("reminder.add", {"text": "example"}, 1), 1,
                      json.dumps({"handoff": True, "say": "Say when."}), "", object(), "remind me example", "turn"))
    assert b.to_claude.await_count == int(handoff)
    if not handoff:
        assert b.say.await_args.args[1] == "Say when."


def test_personal_argv_keeps_option_like_text_positional(cat):
    from heardback.brain.catalogue import build_argv
    assert build_argv(cat["inbox.add"], {"text": "--yes private example"}, "--json") == [
        "jv", "inbox", "add", "--json", "--", "--yes private example"]


def test_haiku_only_still_hands_off(cat):
    b = brain.Brain(claude_cmd=["fake"])
    b.router = router.Router(cat)
    b.rate.haiku_until = time.time() + 60
    b.to_claude, b.say, b.done = AsyncMock(), AsyncMock(), AsyncMock()
    asyncio.run(b._speak_jv(router.Route("reminder.add", {"text": "example"}, 1), 1,
                 json.dumps({"handoff": True, "say": "Say when."}), "", object(), "remind me example", "turn"))
    b.to_claude.assert_awaited_once()


def test_personal_refusal_does_not_hand_off(cat):
    b = brain.Brain(claude_cmd=["fake"])
    b.router = router.Router(cat)
    b.to_claude, b.say, b.done = AsyncMock(), AsyncMock(), AsyncMock()
    asyncio.run(b._speak_jv(router.Route("reminder.add", {"text": "example"}, 1), 1,
                 json.dumps({"say": "Recurring reminders are not supported."}), "", object(), "example", "turn"))
    b.to_claude.assert_not_awaited()
    assert b.say.await_args.args[1] == "Recurring reminders are not supported."
