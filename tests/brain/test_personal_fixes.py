"""Opus review regressions for the personal tool boundary."""
import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

from brainutil import says, utt, serving, talk
from heardback.brain import brain, catalogue, mcp_jv
from heardback.tasks import personal
from jv import cli, reminder, catchup, grants
from test_personal_claude import setup, stream, call, FAKE, UNCERTAIN
from test_injectgate import confirms, YES


@pytest.mark.parametrize("reader", ["reminder_list", "catchup_catchup"])
def test_parallel_read_cannot_hide_admitted_write(setup, monkeypatch, reader):
    out = stream(setup, monkeypatch, {"odd request": {"reverse_results": True,
        "calls": [call(text="pay rent", when="tomorrow at noon"), call(reader)]}}, YES)
    rows = personal.list_reminders()
    assert len(rows) == 1
    assert says(out) == [f"Okay, I'll remind you to pay rent {reminder._time(rows[0]['due_at'])}."]


def test_serial_second_write_is_denied_and_spoken(setup, monkeypatch):
    tool = call(text="pay rent", when="tomorrow at noon")
    out = stream(setup, monkeypatch, {"odd request": {"serial": True, "calls": [tool, tool]}})
    rows = personal.list_reminders()
    assert len(rows) == 1
    assert says(out) == [f"Okay, I'll remind you to pay rent {reminder._time(rows[0]['due_at'])}.", brain.PERSONAL_SECOND]


def test_list_then_cancel_in_one_turn(setup, monkeypatch):
    # Seen live, 2026-11-06: reminder_list ended the turn, so the cancel after it was denied.
    add = call(text="stretch", when="tomorrow at noon")
    out = stream(setup, monkeypatch, {"set one": {"calls": [add]}, "odd request": {"serial": True,
        "calls": [call("reminder_list"), call("reminder_cancel", text="stretch")]}}, YES)
    assert personal.list_reminders(state="pending") == []
    assert says(out)[-1] == "Cancelled." and "That wasn't set. Say it again." not in says(out)


def test_list_alone_is_the_whole_reply(setup, monkeypatch):
    out = stream(setup, monkeypatch, {"odd request": {"serial": True, "calls": [call("reminder_list")], "text": "made up reply"}})
    assert says(out) == ["You have no reminders."]


def test_refused_write_uses_jv_refusal_line_and_second_write_is_denied(setup, monkeypatch):
    out = stream(setup, monkeypatch, {"odd request": {"serial": True, "calls": [
        call(text="stretch", when="after lunch"), call(text="stretch", when="tomorrow at noon")]}}, YES)
    assert personal.list_reminders() == [] and confirms(out) == []
    assert says(out)[0] == "Please give a clock time like 6:30 pm, or a duration like in 20 minutes."


def test_refused_write_with_no_retry_is_spoken(setup, monkeypatch):
    out = stream(setup, monkeypatch, {"odd request": {"serial": True, "calls": [call(text="stretch", when="after lunch")],
                                                      "text": "made up reply"}})
    assert says(out) == ["Please give a clock time like 6:30 pm, or a duration like in 20 minutes."] and personal.list_reminders() == []


def test_second_refusal_ends_the_turn(setup, monkeypatch):
    bad = call(text="stretch", when="after lunch")
    out = stream(setup, monkeypatch, {"odd request": {"serial": True, "calls": [bad, bad, call(text="stretch", when="tomorrow at noon")]}}, YES)
    assert personal.list_reminders() == [] and says(out)[0] == "Please give a clock time like 6:30 pm, or a duration like in 20 minutes."


@pytest.mark.parametrize("reverse", [False, True])
def test_parallel_failed_write_never_claims_success(setup, monkeypatch, reverse):
    out = stream(setup, monkeypatch, {"odd request": {"reverse_results": reverse, "calls": [
        {**call(text="pay rent", when="tomorrow at noon"), "result": {"ok": False, "say": "Nothing changed."}, "error": True},
        call(text="second", when="tomorrow at noon")]}})
    assert says(out) == ["Nothing changed."] and personal.list_reminders() == []


WAKE = ["hey claude", "hey, claude", "hey... jarvis", "hey-jarvis", "hey\u200bclaude", "heyclaude",
        "hey cloud", "HEY\u00a0JARVIS", "okay hey claude now", "hEy\u200cClOuD"]


@pytest.mark.parametrize("text", WAKE)
@pytest.mark.parametrize("name", ["reminder_add", "inbox_add"])
def test_all_wake_forms_refused(setup, monkeypatch, text, name):
    args = {"text": text, **({"when": "tomorrow at noon"} if name == "reminder_add" else {})}
    out = stream(setup, monkeypatch, {"read state": {"calls": [call("reminder_list")]},
        "odd request": {"calls": [call(name, **args)]}})
    assert confirms(out) == []
    assert says(out)[-1] == ("I can't read a wake phrase in a reminder." if name == "reminder_add" else "I can't read a wake phrase in a note.")
    assert personal.list_reminders() == [] and personal.list_inbox() == []


@pytest.mark.parametrize("name,line", [("reminder_add", UNCERTAIN), ("reminder_cancel", UNCERTAIN),
    ("inbox_add", "I'm not sure that saved. Say read my notes."),
    ("fact_remember", "I'm not sure that changed. Please check your memory file."),
    ("fact_forget", "I'm not sure that changed. Please check your memory file."),
    ("reminder_list", "I couldn't read that. Try again."), ("catchup_catchup", "I couldn't read that. Try again.")])
@pytest.mark.parametrize("phase", ["preflight", "result"])
def test_fallback_per_tool(setup, monkeypatch, name, line, phase):
    async def timeout(*args, **kw):
        return (1, "", "timed out") if phase == "preflight" else (0, '{"ok":true}', "")
    monkeypatch.setattr(catalogue, "run_jv", timeout)
    tool = {**call(name), "result": "error: timed out", "error": True}
    out = stream(setup, monkeypatch, {"odd request": {"calls": [tool]}})
    assert says(out) == [line]


def test_prefix_drops_personal_args_at_recording(setup):
    b = brain.Brain()
    async def discard(*args, **kw):
        pass
    b.say = b.done = discard
    asyncio.run(b._speak_jv(brain.router.Route("inbox.add", {"text": "private canary"}, 1), 0,
                           '{"say":"Noted."}', "", None, "capture private canary"))
    assert b.recent[-1][2] == {}
    b.router.cat = {}
    assert "private canary" not in b.prefix("hello")


def test_catchup_is_personal():
    assert catchup.VERBS[0].personal is True
    assert cli.catalogue(catchup.VERBS)["verbs"][0]["personal"] is True


def test_committed_write_heard_after_stop(setup, monkeypatch):
    personal.verify_store()   # Poll committed rows, not the first schema creation transaction.
    monkeypatch.setenv("PERSONAL_STREAM", json.dumps({"odd request": {"calls": [
        {**call(text="call mum", when="tomorrow at noon"), "result_delay": 0.5}]}}))
    b = brain.Brain(claude_cmd=FAKE)
    async def go():
        async with serving(b, brain.sock_path()):
            task = asyncio.create_task(talk(brain.sock_path(), [utt("odd request")]))
            async with asyncio.timeout(3):
                while not personal.list_reminders():
                    await asyncio.sleep(0.01)
            await talk(brain.sock_path(), [{"type": "interrupt", "t": 0}], timeout=0.1)
            return await task
    out = asyncio.run(go())
    rows = personal.list_reminders()
    assert len(rows) == 1
    assert says(out) == [f"Okay, I'll remind you to call mum {reminder._time(rows[0]['due_at'])}."]


def test_real_mcp_request_key_and_error_mapping(setup, monkeypatch):
    env, cat = setup
    executable = env / "jv"
    executable.write_text(f"#!{sys.executable}\nfrom jv.cli import main\nmain()\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(env) + os.pathsep + os.environ["PATH"])
    monkeypatch.delenv("HEARDBACK_EXECUTION_GATE", raising=False)
    monkeypatch.setenv("HEARDBACK_REQUIRE_GRANT", "1")
    args = {"text": "pay rent", "when": "tomorrow at noon"}
    grants.mint("reminder.add", args, "personal-mcp", "")
    result = mcp_jv.call(cat, "reminder_add", args, expected_turn="personal-mcp")
    assert not result["isError"]
    row = json.loads(result["content"][0]["text"])["reminder"]
    assert row["request_key"].startswith("reminder:personal-mcp:")
    grants.mint("reminder.add", args, "personal-mcp", "")
    retry = mcp_jv.call(cat, "reminder_add", args, expected_turn="personal-mcp")
    assert not retry["isError"] and len(personal.list_reminders()) == 1
    bad_args = {**args, "source_revision": "reminder-add:forged"}
    grants.mint("reminder.add", bad_args, "forged", "")
    bad = mcp_jv.call(cat, "reminder_add", bad_args, expected_turn="forged")
    assert bad["isError"] and json.loads(bad["content"][0]["text"])["say"] == "That changed, please say it again."


def test_real_mcp_confirmed_receipt_and_execution_gate(setup, monkeypatch, run_jv):
    env, cat = setup
    executable = env / "jv"
    executable.write_text(f"#!{sys.executable}\nfrom jv.cli import main\nmain()\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(env) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HEARDBACK_EXECUTION_GATE", "1")
    monkeypatch.setenv("HEARDBACK_EXECUTION_OWNER", "receipt-owner")
    monkeypatch.setenv("HEARDBACK_TURN", "receipt-mcp")
    attempt = catalogue.set_current_turn("receipt-mcp", owner="receipt-owner")
    monkeypatch.setenv("HEARDBACK_TAINTED", "1")
    rc, preview, _ = run_jv("reminder", "add", "--when", "in 10 minutes", "--dry-run", "--json", "--", "call mum")
    assert rc == 3
    monkeypatch.delenv("HEARDBACK_TAINTED")
    monkeypatch.setenv("HEARDBACK_REQUIRE_GRANT", "1")
    args = {"text": "call mum", "when": "in 10 minutes", "source_revision": preview["source_revision"]}
    for _ in range(2):
        grants.mint("reminder.add", args, "receipt-mcp", attempt)
        result = mcp_jv.call(cat, "reminder_add", args, expected_turn="receipt-mcp", expected_attempt=attempt)
        assert not result["isError"]
    rows = personal.list_reminders()
    assert len(rows) == 1 and rows[0]["request_key"].startswith("reminder:receipt-mcp:")
    assert preview["confirm"] == f"Set a reminder to call mum {reminder._time(rows[0]['due_at'])}?"
    bad_args = {**args, "source_revision": "reminder-add:forged"}
    grants.mint("reminder.add", bad_args, "receipt-mcp", attempt)
    forged = mcp_jv.call(cat, "reminder_add", bad_args,
                         expected_turn="receipt-mcp", expected_attempt=attempt)
    assert forged["isError"] and json.loads(forged["content"][0]["text"])["say"] == "That changed, please say it again."
    catalogue.block_turn("receipt-mcp", attempt)
    denied = mcp_jv.call(cat, "reminder_add", args, expected_turn="receipt-mcp", expected_attempt=attempt)
    assert denied["isError"] and denied["content"][0]["text"] == "heardback refused: this request has ended; no action was dispatched."


def test_sibling_of_refused_write_is_not_a_retry(setup, monkeypatch):
    # Review M1: two writes in one message; the first refused must not free the second.
    out = stream(setup, monkeypatch, {"odd request": {"serial": True, "same_message": True, "calls": [
        call(text="stretch", when="after lunch"), call(text="call mum", when="tomorrow at noon")]}}, YES)
    assert personal.list_reminders() == [] and says(out)[0] == "Please give a clock time like 6:30 pm, or a duration like in 20 minutes."
