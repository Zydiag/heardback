"""Personal tool boundary, using fake provider streams and isolated real storage."""
import asyncio
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys

import pytest

from brainutil import run_brain, says, script, utt, serving, talk
from heardback.brain import brain, catalogue, mcp_jv
from heardback.tasks import personal
from jv import cli, reminder
from test_injectgate import confirms, responses, YES, reply

FAKE = [sys.executable, str(Path(__file__).with_name("personal_stream.py"))]
MEMORY_REFUSAL = "I can't change memory from this conversation. Say remember that, then the words."
UNCERTAIN = "I'm not sure that saved. Ask what my reminders are."


@pytest.fixture
def setup(env, monkeypatch):
    cat = catalogue._index(cli.catalogue(cli.all_verbs()))
    path = env / "personal-catalogue.json"
    path.write_text(json.dumps({"verbs": list(cat.values())}))
    monkeypatch.setenv("FAKE_JV_CATALOGUE", str(path))
    # Run real CLI preflight in-process; provider executes the approved CLI in its isolated process.
    async def run(argv, **kw):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with monkeypatch.context() as patch:
            if kw.get("tainted"):
                patch.setenv(catalogue.TAINT_ENV, "1")
            else:
                patch.delenv(catalogue.TAINT_ENV, raising=False)
            if kw.get("turn"):
                patch.setenv("HEARDBACK_TURN", kw["turn"])
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cli.run(argv[1:])
        return rc, out.getvalue(), err.getvalue()
    monkeypatch.setattr(catalogue, "run_jv", run)
    return env, cat


def stream(setup, monkeypatch, plans, replies=None, **kw):
    env, _ = setup
    monkeypatch.setenv("PERSONAL_STREAM", json.dumps(plans))
    cmd = [sys.executable, str(Path(__file__).with_name("serial_stream.py"))] if any(p.get("serial") for p in plans.values()) else FAKE
    return run_brain(env, [utt(s) for s in plans], claude_cmd=cmd, replies=replies,
                     CONFIRM_TIMEOUT_S=0.05, CONFIRM_GRACE_S=0, **kw)


def call(name="reminder_add", **args):
    return {"name": "mcp__jv__" + name, "args": args}


def test_tool_filters(setup):
    _, cat = setup
    for name in ("inbox_list", "inbox_remove"):
        assert name not in {t["name"] for t in mcp_jv.tools(cat)}
        assert name not in mcp_jv._by_tool(cat)
        assert "mcp__jv__" + name not in brain.Brain._allowed(cat)
    writes = {"reminder.add", "reminder.cancel", "inbox.add", "fact.remember", "fact.forget"}
    for name in writes:
        assert cat[name]["confirm"] in (True, "auto")
        assert "mcp__jv__" + catalogue.tool_name(cat[name]) not in brain.Brain._allowed(cat)


@pytest.mark.parametrize("name", ["inbox_list", "inbox_remove"])
def test_hidden_tool_denied(setup, monkeypatch, name):
    out = stream(setup, monkeypatch, {"odd request": {"calls": [call(name)]}})
    assert responses(setup[0])[-1] == {"behavior": "deny", "message": brain.NOT_ALLOWED}


@pytest.mark.parametrize("answer,expected", [(YES, None), (reply(False, "no"), brain.NOT_DOING), (None, brain.NO_ANSWER_SAY)])
def test_tainted_add(setup, monkeypatch, answer, expected):
    due = datetime.now().astimezone() + timedelta(days=1)
    plans = {"read state": {"calls": [call("reminder_list")]},
             "odd request": {"calls": [call(text="call mum", when="tomorrow at noon")]}}
    out = stream(setup, monkeypatch, plans, answer)
    rows = personal.list_reminders()
    sentence = f"Set a reminder to call mum {reminder._time(int(due.replace(hour=12, minute=0, second=0).timestamp()))}?"
    assert confirms(out) == [sentence]
    assert len(rows) == (1 if answer == YES else 0)
    assert says(out)[-1] == (f"Okay, I'll remind you to call mum {reminder._time(rows[0]['due_at'])}." if expected is None else expected)


def test_clean_add_terminal_and_next_write_stays_clean(setup, monkeypatch):
    plans = {s: {"calls": [call(text=s, when="tomorrow at noon")]} for s in ("first request", "second request")}
    out = stream(setup, monkeypatch, plans)
    rows = personal.list_reminders()
    assert len(rows) == 2 and confirms(out) == []
    assert says(out) == [f"Okay, I'll remind you to {s} {reminder._time(rows[0]['due_at'])}." for s in plans]


@pytest.mark.parametrize("name", ["fact_remember", "fact_forget"])
def test_tainted_memory_refuses_and_router_still_works(setup, monkeypatch, name):
    out = stream(setup, monkeypatch, {"read state": {"calls": [call("reminder_list")]},
                 "odd request": {"calls": [call(name, text="I like tea")]}})
    assert confirms(out) == [] and says(out)[-1] == MEMORY_REFUSAL
    # The same tainted brain can still run the user's exact local phrase.
    b = brain.Brain(claude_cmd=FAKE)
    async def go():
        async with serving(b, brain.sock_path()):
            await talk(brain.sock_path(), [utt("read state")])
            assert b._tainted is b.claude
            return await talk(brain.sock_path(), [utt("remember that I like tea")], replies=YES)
    local = asyncio.run(go())
    assert confirms(local) == ["Remember this: I like tea?"] and says(local)[-1] == "Saved."


@pytest.mark.parametrize("text", ["HeY JaRvIs do this", "hEy ClAuDe do this", "x" * 190])
def test_tainted_reminder_unsafe_readback(setup, monkeypatch, text):
    out = stream(setup, monkeypatch, {"read state": {"calls": [call("reminder_list")]},
                 "odd request": {"calls": [call(text=text, when="tomorrow at noon")]}})
    assert confirms(out) == [] and personal.list_reminders() == []
    if text != "x" * 190:
        assert says(out)[-1] == "I can't read a wake phrase in a reminder."
    else:
        assert says(out)[-1].startswith("heardback didn't send it: too long to read back (")
        assert says(out)[-1].endswith("characters, the most is 200)")


@pytest.mark.parametrize("when", ["x" * 61, "-tomorrow"])
def test_invalid_when_is_spoken(setup, monkeypatch, when):
    out = stream(setup, monkeypatch, {"odd request": {"calls": [call(text="call mum", when=when)]}})
    assert confirms(out) == [] and personal.list_reminders() == []
    assert says(out)[-1] == ("when too long" if len(when) == 61 else "Please check the reminder or note command.")


@pytest.mark.parametrize("result,error", [({"ok": False, "say": "I could not save that."}, True), ("error: timed out", True)])
def test_failed_and_timed_out_tool(setup, monkeypatch, result, error):
    tool = {**call(text="call mum", when="tomorrow at noon"), "result": result, "error": error}
    out = stream(setup, monkeypatch, {"odd request": {"calls": [tool]}})
    assert says(out) == [result["say"] if isinstance(result, dict) else UNCERTAIN]


@pytest.mark.parametrize("reverse", [False, True])
def test_parallel_writes_one_row(setup, monkeypatch, reverse):
    out = stream(setup, monkeypatch, {"odd request": {"reverse_results": reverse,
        "calls": [call(text=s, when="tomorrow at noon") for s in ("first", "second")]}})
    rows = personal.list_reminders()
    assert len(rows) == 1
    assert says(out) == [f"Okay, I'll remind you to first {reminder._time(rows[0]['due_at'])}.", brain.PERSONAL_SECOND]


@pytest.mark.parametrize("reader", ["reminder_list", "catchup_catchup"])
def test_reads_taint(setup, monkeypatch, reader):
    out = stream(setup, monkeypatch, {"read state": {"calls": [call(reader)]},
                 "odd request": {"calls": [call("inbox_add", text="a note")] }}, YES)
    assert confirms(out) == ["Capture this note: a note?"]


def test_refusal_taints_next_add(setup, monkeypatch):
    out = stream(setup, monkeypatch, {"bad request": {"calls": [call(text="call mum", when="-bad")]},
                 "odd request": {"calls": [call(text="call mum", when="tomorrow at noon")]}}, YES)
    assert len(confirms(out)) == 1 and len(personal.list_reminders()) == 1


@pytest.mark.parametrize("stopped", [False, True])
def test_off_and_limit_no_personal_hint(setup, monkeypatch, stopped):
    b = brain.Brain(claude_cmd=FAKE)
    if stopped:
        monkeypatch.setattr(b.rate, "claude_allowed", lambda now: False)
    else:
        b.claude_off = "manual"
    out = run_brain(setup[0], [utt("please jot this down")], brain=b)
    assert says(out) == [b.rate.limit() if stopped else brain.OFF]


@pytest.mark.parametrize("key", ["tainted", "HEARDBACK_TAINTED", "_tainted", "gate", "readback"])
def test_model_cannot_set_or_clear_personal_taint(setup, monkeypatch, key):
    out = stream(setup, monkeypatch, {
        "first request": {"calls": [call(text="first", when="tomorrow at noon", **{key: True})]},
        "read state": {"calls": [call("reminder_list")]},
        "second request": {"calls": [call(text="second", when="tomorrow at noon", **{key: False})]}}, YES)
    assert len(confirms(out)) == 1
    rows = personal.list_reminders()
    assert len(rows) == 2
    assert says(out)[-1] == f"Okay, I'll remind you to second {reminder._time(rows[0]['due_at'])}."


@pytest.mark.parametrize("when", ["in 10 minutes", "in half an hour", "in 2 hours", "in 2 days", "tomorrow at noon"])
def test_real_seconds_between_readback_and_yes(setup, monkeypatch, when):
    import time
    start = time.time()
    out = stream(setup, monkeypatch, {"read state": {"calls": [call("reminder_list")]},
        "odd request": {"calls": [{**call(text="call mum", when=when), "delay": 1.1}]}}, YES)
    assert time.time() - start >= 1.1
    rows = personal.list_reminders()
    assert len(rows) == 1
    assert confirms(out) == [f"Set a reminder to call mum {reminder._time(rows[0]['due_at'])}?"]
    assert says(out)[-1] == f"Okay, I'll remind you to call mum {reminder._time(rows[0]['due_at'])}."


def test_stop_during_personal_preflight(setup, monkeypatch):
    env, _ = setup
    monkeypatch.setenv("PERSONAL_STREAM", json.dumps({"odd request": {"calls": [call(text="call mum", when="tomorrow at noon")]}}))
    original = catalogue.run_jv
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed(argv, **kw):
        entered.set()
        await release.wait()
        return await original(argv, **kw)
    monkeypatch.setattr(catalogue, "run_jv", delayed)
    b = brain.Brain(claude_cmd=FAKE)
    async def go():
        async with serving(b, brain.sock_path()):
            task = asyncio.create_task(talk(brain.sock_path(), [utt("odd request")]))
            await asyncio.wait_for(entered.wait(), 3)
            await talk(brain.sock_path(), [{"type": "interrupt", "t": 0}], timeout=0.1)
            release.set()
            return await task
    out = asyncio.run(go())
    assert personal.list_reminders() == [] and confirms(out) == [] and says(out) == []
    assert responses(env)[-1]["behavior"] == "deny"


def test_clean_capture_terminal(setup, monkeypatch):
    out = stream(setup, monkeypatch, {"odd request": {"calls": [call("inbox_add", text="a note")]}})
    assert says(out) == ["Noted: a note"] and confirms(out) == []
    assert len(personal.list_inbox()) == 1


@pytest.mark.parametrize("name,text,question,say", [
    ("fact_remember", "I like tea", "Remember this: I like tea?", "Saved."),
    ("fact_forget", "tea", "Forget these lines: - I like tea?", "Forgotten.")])
def test_clean_memory_terminal(setup, monkeypatch, name, text, question, say):
    from heardback.brain.memory import memory_path
    path = memory_path()
    assert path == Path(os.environ["XDG_CONFIG_HOME"]) / "heardback" / "memory.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("## Remembered\n- I like tea\n")
    path.chmod(0o600)
    out = stream(setup, monkeypatch, {"odd request": {"calls": [call(name, text=text)]}}, YES)
    assert confirms(out) == [question] and says(out) == [say]


def test_clean_cancel_terminal(setup, monkeypatch):
    row = personal.add_reminder(request_key="seed", text="call mum", due_at=int(datetime.now().timestamp()) + 3600)
    out = stream(setup, monkeypatch, {"odd request": {"calls": [call("reminder_cancel", text="call mum")]}}, YES)
    assert confirms(out) == [f"Cancel the reminder to call mum {reminder._time(row['due_at'])}?"]
    assert says(out) == ["Cancelled."]
    assert personal.get_reminder(row["reminder_id"])["state"] == "cancelled"


def test_preflight_timeout_uses_uncertainty(setup, monkeypatch):
    async def timeout(*args, **kw):
        return 1, "", "timed out"
    monkeypatch.setattr(catalogue, "run_jv", timeout)
    out = stream(setup, monkeypatch, {"odd request": {"calls": [call(text="call mum", when="tomorrow at noon")]}})
    assert confirms(out) == [] and says(out) == [UNCERTAIN]
    assert personal.list_reminders() == []
