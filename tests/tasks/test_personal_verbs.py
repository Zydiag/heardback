"""Task 4 verbs, against disposable state only."""
import json
from datetime import datetime, timezone

import pytest

from heardback.tasks import personal
from jv import cli, reminder


@pytest.fixture
def call(capsys, monkeypatch):
    monkeypatch.setenv("HEARDBACK_TURN", "test-turn")

    def invoke(*argv):
        rc = cli.run([*argv, "--json"])
        out, err = capsys.readouterr()
        return rc, json.loads(out), err
    return invoke


def test_add_time_assumption_and_retry(call, monkeypatch):
    from jv import reminder
    monkeypatch.setattr(reminder, "_now", lambda: datetime(2026, 11, 6, 10, tzinfo=timezone.utc))
    rc, data, _ = call("reminder", "add", "to phone a friend at 6")
    assert rc == 0 and "6 pm" in data["say"].lower() and "assum" in data["say"].lower()
    assert data["reminder"]["text"] == "phone a friend"
    assert call("reminder", "add", "to phone a friend at 6")[1] == data
    assert len(personal.list_reminders()) == 1


def test_two_reminders_in_one_turn_are_distinct_once(call, monkeypatch):
    monkeypatch.setattr(reminder, "_now", lambda: datetime(2026, 11, 6, 10, tzinfo=timezone.utc))
    first = call("reminder", "add", "to phone a friend at 6")[1]
    assert call("reminder", "add", "to phone a friend at 6")[1] == first
    second = call("reminder", "add", "to call mum at 7")[1]
    assert second["reminder"]["reminder_id"] != first["reminder"]["reminder_id"]
    assert len(personal.list_reminders()) == 2


@pytest.mark.parametrize("text,handoff", [("phone a friend", True), ("phone a friend every day", False)])
def test_no_time_and_recurring(call, text, handoff):
    rc, data, _ = call("reminder", "add", text)
    assert rc == 1 and bool(data.get("handoff")) == handoff and data["say"]
    assert not personal.database_path().exists()


@pytest.mark.parametrize("argv", [
    ["reminder", "add"], ["reminder", "add", "text", "--when", "-x"],
    ["reminder", "add", "text", "--when=-x"],
    ["reminder", "add", "text", "--when", "tomorrow extra words"],
    ["reminder", "list", "extra"], ["inbox", "remove", "invalid"],
    ["inbox", "add"], ["inbox", "add", "x" * 1001],
    ["reminder", "cancel", "--source-revision", "stale", "--yes"],
])
def test_personal_errors_never_exit_two(call, argv):
    rc, data, _ = call(*argv)
    assert rc == 1 and data["say"] and not data["ok"]


def add(key, text="example", due=2000000000):
    return personal.add_reminder(request_key=key, text=text, due_at=due)


def test_cancel_zero_one_many_and_bound_revision(call):
    assert call("reminder", "cancel", "example")[0] == 1
    first = add("one")
    rc, data, _ = call("reminder", "cancel", "example")
    assert rc == 3 and data["source_revision"] and "example" in data["confirm"]
    revision = data["source_revision"]
    assert call("reminder", "cancel", "example", "--yes")[0] == 3
    assert personal.get_reminder(first["reminder_id"])["state"] == "pending"
    add("two", "example two")
    assert call("reminder", "cancel", "example")[0] == 1
    assert call("reminder", "cancel", "example", "--yes", "--source-revision", revision)[0] == 1
    assert all(r["state"] == "pending" for r in personal.list_reminders())
    assert call("reminder", "cancel", "example two")[0] == 3
    revision = call("reminder", "cancel", "example two")[1]["source_revision"]
    assert call("reminder", "cancel", "example two", "--yes", "--source-revision", revision)[0] == 0
    assert len(personal.list_reminders(state="pending")) == 1


def test_cancel_race_writes_nothing(call):
    row = add("one")
    revision = call("reminder", "cancel", "example")[1]["source_revision"]
    assert personal.claim_reminder(row["reminder_id"], expected_revision=1)
    assert call("reminder", "cancel", "example", "--yes", "--source-revision", revision)[0] == 1
    assert personal.get_reminder(row["reminder_id"])["state"] == "delivering"


def test_cancel_that_two_minute_window(call, monkeypatch):
    row = add("one")
    created = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")).timestamp()
    monkeypatch.setattr(personal.time, "time", lambda: created + 119)
    assert call("reminder", "cancel", "--recent")[0] == 3
    monkeypatch.setattr(personal.time, "time", lambda: created + 121)
    assert call("reminder", "cancel", "--recent")[0] == 1
    assert personal.get_reminder(row["reminder_id"])["state"] == "pending"


def test_notes_list_and_typed_remove(call):
    assert call("inbox", "list")[0] == 0
    rc, data, _ = call("inbox", "add", "example note")
    assert rc == 0 and data["say"] == "Noted: example note"
    item = data["item"]
    assert call("inbox", "list")[1]["say"].endswith("example note")
    assert call("inbox", "remove", item["item_id"])[0] == 0
    assert not personal.list_inbox()


def test_readback_is_not_shortened(call):
    add("long", "z" * 200)
    rc, data, _ = call("reminder", "cancel")
    assert rc == 1 and data["say"]
    assert personal.list_reminders()[0]["state"] == "pending"


def test_catalogue_metadata():
    cat = {v["name"]: v for v in cli.catalogue(cli.all_verbs())["verbs"]}
    for name in ("reminder.add", "reminder.list", "reminder.cancel", "inbox.add", "inbox.list"):
        assert cat[name]["personal"]
    assert not cat["inbox.list"]["claude"]
    assert not cat["inbox.remove"]["voice"] and not cat["inbox.remove"]["claude"]
    assert cat["reminder.add"]["confirm"] == cat["inbox.add"]["confirm"] == "auto"


def test_personal_cli_does_not_trace_private_arguments(monkeypatch, capsys):
    import sys
    from jv import trace
    events = []
    monkeypatch.setattr(trace, "emit", lambda *a, **kw: events.append((a, kw)))
    monkeypatch.setattr(sys, "argv", ["jv", "inbox", "add", "private example", "--json"])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 0
    assert "private example" not in repr(events)
    capsys.readouterr()


@pytest.mark.parametrize("when", ["at 6 pm", "tomorrow", "in 10 minutes"])
def test_explicit_whole_when(call, when):
    rc, data, _ = call("reminder", "add", "example action", "--when", when)
    assert rc == 0 and data["reminder"]["text"] == "example action"
    assert " at " in data["say"]


def test_dry_run_writes_nothing(call):
    assert call("reminder", "add", "example in 5 minutes", "--dry-run")[0] == 0
    assert call("inbox", "add", "example", "--dry-run")[0] == 0
    assert not personal.database_path().exists()


def test_counts_next_three_and_cancel_beyond_first_page(call):
    for n in range(101):
        add(str(n), text=f"example {n}", due=2000000000 + n)
    listing = call("reminder", "list")[1]["say"]
    assert "You have 101 reminders." in listing
    assert "example 2" in listing and "example 3" not in listing
    assert call("reminder", "cancel", "example 100")[0] == 3


def test_note_reply_preview_and_newest_three(call, monkeypatch):
    for n in range(4):
        monkeypatch.setenv("HEARDBACK_TURN", f"turn-{n}")
        monkeypatch.setattr(personal, "_now", lambda n=n: f"2026-11-06T10:00:0{n}.000Z")
        assert call("inbox", "add", f"example {n}")[0] == 0
    listing = call("inbox", "list")[1]["say"]
    assert "4 notes" in listing and "example 0" not in listing
    assert listing.index("example 3") < listing.index("example 2") < listing.index("example 1")
    monkeypatch.setenv("HEARDBACK_TURN", "long-note")
    rc, data, _ = call("inbox", "add", "q" * 121)
    assert rc == 0 and data["say"] == "Noted: " + "q" * 120
    assert data["item"]["text"] == "q" * 121


def test_time_only_request_does_not_store(call):
    assert call("reminder", "add", "in 5 minutes")[0] == 1
    assert not personal.database_path().exists()


def test_cancel_revision_mismatch_keeps_single_match(call):
    row = add("one")
    assert call("reminder", "cancel", "example", "--yes", "--source-revision", "wrong")[0] == 1
    assert personal.get_reminder(row["reminder_id"]) == row


def test_unexpected_failure_keeps_private_body_out_of_diagnostics(call, monkeypatch):
    def broken(**kwargs):
        raise RuntimeError(kwargs["text"])
    monkeypatch.setattr(personal, "add_inbox", broken)
    rc, data, err = call("inbox", "add", "private example")
    assert rc == 1 and "private example" not in json.dumps(data) + err


def test_boundary_validation_does_not_create_state(call):
    for domain, text in (("reminder", "x" * 301 + " in 5 minutes"), ("inbox", "é" * 501), ("inbox", "bad\ncontrol")):
        rc, data, _ = call(domain, "add", text)
        assert rc == 1 and data["say"]
    assert not personal.database_path().exists()


@pytest.mark.parametrize("argv", [
    ["reminder", "add", "example", "--when", "tomorrow extra words"],
    ["reminder", "add", "example", "--when=-x"],
    ["reminder", "add", "example every day"],
    ["inbox", "add", "x" * 1001],
])
def test_dry_run_also_refuses_invalid_input(call, argv):
    rc, data, _ = call(*argv, "--dry-run")
    assert rc == 1 and data["say"]
    assert not personal.database_path().exists()


def spoken_time(monkeypatch, now, due):
    monkeypatch.setattr(reminder, "_now", lambda: now.astimezone())
    return reminder._time(int(due.astimezone().timestamp()))


@pytest.mark.parametrize("due, expected", [
    (datetime(2026, 11, 6, 20, 46), "at 8:46 pm"),
    (datetime(2026, 11, 6, 21), "at 9 pm"),
    (datetime(2026, 11, 7, 0), "tomorrow at 12 am"),
    (datetime(2026, 11, 7, 6, 30), "tomorrow at 6:30 am"),
    (datetime(2026, 11, 13, 18), "on Friday, November 13 at 6 pm"),
    (datetime(2027, 1, 1, 12), "on Friday, January 1, 2027 at 12 pm"),
])
def test_spoken_time(monkeypatch, due, expected):
    assert spoken_time(monkeypatch, datetime(2026, 11, 6, 20, 41), due) == expected


def test_add_list_and_cancel_wording(call, monkeypatch):
    monkeypatch.setattr(reminder, "_now", lambda: datetime(2026, 11, 6, 20, 41).astimezone())
    assert call("reminder", "list")[1]["say"] == "You have no reminders."
    assert call("reminder", "add", "to drink water in 5 minutes")[1]["say"] == "Okay, I'll remind you to drink water at 8:46 pm."
    assert call("reminder", "list")[1]["say"] == "You have 1 reminder. Drink water at 8:46 pm."
    monkeypatch.setenv("HEARDBACK_TURN", "test-turn-2")
    call("reminder", "add", "stretch at 9 pm")
    assert call("reminder", "list")[1]["say"] == "You have 2 reminders. Drink water at 8:46 pm, stretch at 9 pm."
    rc, data, _ = call("reminder", "cancel", "stretch")
    assert rc == 3 and data["confirm"] == "Cancel the reminder to stretch at 9 pm?"


def test_no_task_hands_off_to_claude(call):
    rc, data, _ = call("reminder", "add", "for 2 minutes")
    assert rc == 1 and data["handoff"] is True
