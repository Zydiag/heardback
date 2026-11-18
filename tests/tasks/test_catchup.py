"""Catch-up is a read-only projection over disposable local state."""
import json
import time
from contextlib import contextmanager
from datetime import datetime, timezone
import sqlite3

import pytest

from heardback.tasks import personal
from jv import catchup, cli


@pytest.fixture
def invoke(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))

    def run(*argv):
        rc = cli.run([*argv, "--json"])
        out, _ = capsys.readouterr()
        return rc, json.loads(out)
    return run


def test_top_level_verb_registered_and_cli_runs_with_claude_off(invoke):
    rc, data = invoke("catchup")
    assert (rc, data["say"]) == (0, "Nothing new.")
    from heardback.brain.brain import Brain
    assert Brain(claude_cmd=None).claude_cmd is None


@pytest.mark.parametrize("said", ["catch me up", "what did I miss"])
def test_exact_phrases_route_locally(said):
    from heardback.brain import catalogue, router
    from jv.cli import all_verbs, catalogue as cli_catalogue
    cat = catalogue._index(cli_catalogue(all_verbs()))
    route = router.Router(cat).route(said)
    assert route and route.verb == "catchup" and route.args == {}


def test_session_summary_phrase_stays_on_status():
    from heardback.brain import catalogue, router
    from jv.cli import all_verbs, catalogue as cli_catalogue
    cat = catalogue._index(cli_catalogue(all_verbs()))
    route = router.Router(cat).route("summarise my sessions")
    assert route and route.verb == "status.summary"


@pytest.mark.parametrize("failed", ["reminders", "notes", "sessions", "claude"])
def test_each_failed_section_leaves_other_sections_spoken(monkeypatch, failed):
    providers = {name: getattr(catchup, "_" + name) for name in
                 ("reminders", "notes", "sessions", "claude")}
    for name in providers:
        if name == failed:
            monkeypatch.setattr(catchup, "_" + name, lambda: (_ for _ in ()).throw(RuntimeError()))
        else:
            monkeypatch.setattr(catchup, "_" + name, lambda name=name: {
                "reminders": "Reminder section.", "notes": "Notes section.",
                "sessions": "Sessions section.",
                "claude": "Claude section.",
            }[name])
    result = catchup._catchup({})["say"]
    labels = {"reminders": "Reminder", "notes": "Notes", "sessions": "Sessions", "claude": "Claude"}
    expected = " ".join(labels[name] + " section." for name in providers if name != failed)
    label = "Claude" if failed == "claude" else failed
    assert result == expected + f" I couldn't check {label}."


@pytest.mark.parametrize("failed", ["reminders", "notes", "sessions", "claude", "all"])
def test_failed_empty_sections_never_claim_nothing_new(monkeypatch, failed):
    for name in ("reminders", "notes", "sessions", "claude"):
        def section(name=name):
            if failed in (name, "all"):
                raise RuntimeError("synthetic failure")
            return ""
        monkeypatch.setattr(catchup, "_" + name, section)
    names = "reminders, notes, sessions and Claude" if failed == "all" else (
        "Claude" if failed == "claude" else failed)
    assert catchup._catchup({}) == {"say": f"I couldn't check {names}."}


@pytest.mark.parametrize("age,expected", [
    (0, "1 spoken recently: call mum."), (12 * 3600, "1 spoken recently: call mum."),
    (12 * 3600 + 1, ""),
])
def test_reminder_spoken_window_boundaries(invoke, monkeypatch, age, expected):
    now = 2_000_000_000
    monkeypatch.setattr(catchup.time, "time", lambda: now - age)
    row = personal.add_reminder(request_key="boundary", text="call mum", due_at=now - age)
    claim = personal.claim_reminder(row["reminder_id"], expected_revision=1)
    personal.finish_reminder(row["reminder_id"], expected_revision=claim["revision"], result="spoken")
    monkeypatch.setattr(catchup.time, "time", lambda: now)
    assert catchup._reminders() == expected


def test_reminders_overdue_held_unknown_next_and_cap(monkeypatch):
    monkeypatch.setattr(catchup.time, "time", lambda: 200000)
    monkeypatch.setattr(catchup.personal, "catchup_reminders", lambda **kw: {
        "overdue": 1, "spoken_count": 5, "spoken": [{"text": f"done{i}"} for i in range(3)],
        "unknown_count": 2, "unknown": [{"text": "maybe"}, {"text": "maybe2"}],
        "next": {"text": "next", "due_at": 2_000_000_030},
    })
    monkeypatch.setattr("jv.catchup._time", lambda _: "at 6 pm today")
    result = catchup._reminders()
    assert result == "1 overdue or held reminder; 2 reminders may not have played: maybe, maybe2. 5 spoken recently: done0, done1, done2; Next reminder: next, at 6 pm today."


def test_real_reminder_states(invoke, monkeypatch):
    now = 2_000_000_000
    monkeypatch.setattr(personal, "_now", lambda: datetime.fromtimestamp(
        now, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"))
    monkeypatch.setattr(catchup.time, "time", lambda: now)
    overdue = personal.add_reminder(request_key="overdue", text="overdue", due_at=now - 1)
    spoken = personal.add_reminder(request_key="spoken", text="spoken", due_at=now - 10)
    claim = personal.claim_reminder(spoken["reminder_id"], expected_revision=1)
    assert personal.finish_reminder(spoken["reminder_id"], expected_revision=claim["revision"], result="spoken")
    unknown = personal.add_reminder(request_key="unknown", text="uncertain", due_at=now - 20)
    claim = personal.claim_reminder(unknown["reminder_id"], expected_revision=1)
    assert personal.finish_reminder(unknown["reminder_id"], expected_revision=claim["revision"], result="unknown")
    personal.add_reminder(request_key="pending", text="upcoming", due_at=now + 60)
    from jv.reminder import _time
    assert catchup._reminders() == (
        f"1 overdue or held reminder; 1 reminder may not have played: uncertain. "
        f"1 spoken recently: spoken; Next reminder: upcoming, {_time(now + 60)}."
    )
    assert personal.get_reminder(overdue["reminder_id"])["state"] == "pending"


def test_read_all_reminder_rows_and_newest_three(invoke, monkeypatch):
    now, clock = 2_000_000_000, [0]
    monkeypatch.setattr(catchup.time, "time", lambda: clock[0])
    for i in range(120):
        clock[0] = now - 100_000 + i
        row = personal.add_reminder(request_key=f"old-{i}", text=f"old-{i}", due_at=now - 200_000 + i)
        claim = personal.claim_reminder(row["reminder_id"], expected_revision=1)
        personal.finish_reminder(row["reminder_id"], expected_revision=claim["revision"], result="spoken")
    clock[0] = now - 300
    row = personal.add_reminder(request_key="fresh", text="fresh", due_at=now - 200)
    claim = personal.claim_reminder(row["reminder_id"], expected_revision=1)
    personal.finish_reminder(row["reminder_id"], expected_revision=claim["revision"], result="spoken")
    for i in range(60):
        personal.add_reminder(request_key=f"late-{i}", text=f"late-{i}", due_at=now - 60 + i)
        personal.add_reminder(request_key=f"future-{i}", text=f"upcoming-{i}", due_at=now + i + 1)
    clock[0] = now
    from jv.reminder import _time
    assert catchup._reminders() == (
        f"60 overdue or held reminders. 1 spoken recently: fresh; "
        f"Next reminder: upcoming-0, {_time(now + 1)}."
    )


@pytest.mark.parametrize("age,expected", [(0, 1), (24 * 3600, 1), (24 * 3600 + 1, 0)])
def test_note_count_boundaries(invoke, monkeypatch, age, expected):
    now = 2_000_000_000
    created = datetime.fromtimestamp(now - age, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    monkeypatch.setattr(catchup.time, "time", lambda: now)
    monkeypatch.setattr(personal, "_now", lambda: created)
    personal.add_inbox(request_key="boundary", text="UNIQUE-PRIVATE-NOTE")
    result = catchup._notes()
    assert result == ("1 new note." if expected else "")
    assert "UNIQUE-PRIVATE-NOTE" not in result


def test_sessions_add_no_extra_text(monkeypatch):
    monkeypatch.setattr(catchup, "_session_overview", lambda: "One live local Claude Code session. Shell is idle.")
    assert catchup._sessions() == "One live local Claude Code session."


@pytest.mark.parametrize("values,expected", [
    ({"off_until": 2_000_000_000}, "Claude is unavailable until 3:33 pm."),
    ({"haiku_until": 2_000_000_000}, "Claude is using Haiku until 3:33 pm."),
    ({}, ""),
])
def test_claude_usage_states(monkeypatch, values, expected):
    monkeypatch.setattr(catchup.usage, "_read", lambda **kw: values)
    monkeypatch.setattr(catchup.usage, "spoken", lambda _: "3:33 pm")
    monkeypatch.setattr(catchup.time, "time", lambda: 1_900_000_000)
    assert catchup._claude() == expected


@pytest.mark.parametrize("contents", [None, "not-json"])
def test_missing_or_malformed_usage_is_silent(tmp_path, monkeypatch, contents):
    state = tmp_path / "heardback"
    state.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    if contents is not None:
        (state / "usage.json").write_text(contents)
    assert catchup._claude() == ""


def test_read_changes_no_ledger_rows_or_revisions(invoke):
    personal.add_reminder(request_key="before", text="held reminder", due_at=int(time.time()) - 1)
    personal.add_inbox(request_key="note", text="private note")
    before = (personal.list_reminders(), personal.list_inbox())
    before_bytes = personal.database_path().read_bytes()
    rc, data = invoke("catchup")
    after = (personal.list_reminders(), personal.list_inbox())
    assert rc == 0 and data["say"] != "Nothing new."
    assert "private note" not in data["say"]
    assert after == before
    assert personal.database_path().read_bytes() == before_bytes


def test_exit_code_and_full_reply_are_bounded(invoke, monkeypatch):
    now, clock = 2_000_000_000, [2_000_000_000]
    monkeypatch.setattr(catchup.time, "time", lambda: clock[0])
    monkeypatch.setattr(personal, "_now", lambda: datetime.fromtimestamp(clock[0], timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"))
    personal.add_reminder(request_key="overdue", text="overdue", due_at=now - 1)
    for result in ("spoken", "unknown"):
        for i in range(4):
            clock[0] = now - 4 + i
            text = f"{result} {i} call the doctor! Then check the appointment details?"
            row = personal.add_reminder(request_key=f"{result}-{i}", text=text, due_at=now - 10)
            claim = personal.claim_reminder(row["reminder_id"], expected_revision=1)
            personal.finish_reminder(row["reminder_id"], expected_revision=claim["revision"], result=result)
    clock[0] = now
    personal.add_reminder(request_key="next", text="next call the doctor! Then check the appointment details?", due_at=now + 3600)
    personal.add_inbox(request_key="note", text="UNIQUE-PRIVATE-NOTE")
    (personal.database_path().parent / "usage.json").write_text(json.dumps({"off_until": now + 3600}))
    records = [{"session_id": str(i), "project": f"project{i}", "state": "working",
                "updated_at": "2033-05-18T03:33:20.000Z", "tmux_pane": None} for i in range(4)]
    monkeypatch.setattr(catchup.store, "list_sessions", lambda **kw: records)
    rc, data = invoke("catchup")
    expected = (
        "1 overdue or held reminder; 4 reminders may not have played: unknown 3 call the doctor, Then check…, unknown 2 call the doctor, Then check…, unknown 1 call the doctor, Then check…. "
        "4 spoken recently: spoken 3 call the doctor, Then check…, spoken 2 call the doctor, Then check…, spoken 1 call the doctor, Then check…; "
        f"Next reminder: next call the doctor, Then check the…, {catchup._time(now + 3600)}. "
        "1 new note. 4 live local Claude Code sessions. "
        f"Claude is unavailable until {catchup.usage.spoken(now + 3600)}."
    )
    assert (rc, data["say"]) == (0, expected)
    assert len(data["say"].split(". ")) == 5
    assert "UNIQUE-PRIVATE-NOTE" not in data["say"]


@pytest.mark.parametrize("text,expected", [
    ("call mum", "call mum"),
    ("call the doctor about the new appointment details tomorrow", "call the doctor about the new…"),
    ("x" * 41, "…"),
    ("Call mum. Ask how she is! Check in?", "Call mum, Ask how she is, Check in,"),
    ("x" * 40, "x" * 40),
])
def test_reminder_names_are_short_and_do_not_add_sentences(text, expected):
    assert catchup._name(text) == expected
    assert len(catchup._name(text)) <= 40


def test_absent_state_tree_stays_absent(invoke, tmp_path):
    assert not (tmp_path / "state").exists()
    rc, data = invoke("catchup")
    assert (rc, data["say"]) == (0, "Nothing new.")
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "config").exists()


def test_unsafe_ledger_names_failed_sections(invoke):
    personal.add_reminder(request_key="late", text="synthetic overdue", due_at=1)
    path = personal.database_path()
    path.chmod(0o644)
    try:
        rc, data = invoke("catchup")
        assert (rc, data["say"]) == (0, "I couldn't check reminders and notes.")
    finally:
        path.chmod(0o600)


def test_reminder_count_and_names_share_read_snapshot(invoke, monkeypatch):
    now = 2_000_000_000
    monkeypatch.setattr(catchup.time, "time", lambda: now)
    first = personal.add_reminder(request_key="first", text="first", due_at=now - 1)
    claim = personal.claim_reminder(first["reminder_id"], expected_revision=1)
    personal.finish_reminder(first["reminder_id"], expected_revision=claim["revision"], result="spoken")
    second = personal.add_reminder(request_key="second", text="second", due_at=now - 1)
    claim = personal.claim_reminder(second["reminder_id"], expected_revision=1)
    # WAL lets the second connection finish delivery while the reader holds its snapshot.
    with sqlite3.connect(personal.database_path()) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    original_open = personal._open

    class InterleavedConnection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, values=()):
            if sql.startswith("SELECT * FROM reminders WHERE state='spoken'"):
                assert personal.finish_reminder(second["reminder_id"], expected_revision=claim["revision"], result="spoken")
            return self.conn.execute(sql, values)

    @contextmanager
    def interleaved_open(*, write=False):
        with original_open(write=write) as conn:
            yield conn if write else InterleavedConnection(conn)

    with monkeypatch.context() as scoped:
        scoped.setattr(personal, "_open", interleaved_open)
        assert catchup._reminders() == "1 spoken recently: first."
    summary = personal.catchup_reminders(now=now)
    assert summary["spoken_count"] == 2
    assert {row["text"] for row in summary["spoken"]} == {"first", "second"}
