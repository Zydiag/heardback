"""Personal storage acceptance, using disposable ledgers only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import sqlite3
import threading

import pytest

from heardback.tasks import personal


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def reminder(key="request", due=2000000000, text="example"):
    return personal.add_reminder(request_key=key, text=text, due_at=due)


def move(fn, row):
    return fn(row["reminder_id"], expected_revision=row["revision"])


@pytest.mark.parametrize("kind", ["reminder", "inbox"])
def test_retry_returns_first_row_even_with_different_data(kind):
    if kind == "reminder":
        first = reminder()
        assert reminder(due=2000000001, text="changed") == first
        assert personal.list_reminders() == [first]
    else:
        first = personal.add_inbox(request_key="request", text="example")
        assert personal.add_inbox(request_key="request", text="changed") == first
        assert personal.list_inbox() == [first]


@pytest.mark.parametrize("kind", ["reminder", "inbox"])
def test_concurrent_duplicate_adds_return_one_row(kind):
    personal.verify_store()
    barrier = threading.Barrier(2)

    def add():
        barrier.wait(timeout=10)
        return reminder() if kind == "reminder" else personal.add_inbox(request_key="request", text="example")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(add) for _ in range(2)]
        rows = [future.result(timeout=10) for future in futures]
    assert rows[0] == rows[1]
    assert len(personal.list_reminders() if kind == "reminder" else personal.list_inbox()) == 1


@pytest.mark.parametrize("first,second,state", [
    ("claim_reminder", "cancel_reminder", "delivering"),
    ("cancel_reminder", "claim_reminder", "cancelled"),
])
def test_claim_cancel_two_connections(first, second, state):
    row = reminder()
    with ThreadPoolExecutor(max_workers=1) as pool:
        winner = pool.submit(move, getattr(personal, first), row).result(timeout=10)
    assert winner["state"] == state
    assert winner["revision"] == 2
    assert move(getattr(personal, second), row) is None
    assert personal.get_reminder(row["reminder_id"]) == winner


def test_simultaneous_claim_cancel_has_one_winner():
    row = reminder()
    barrier = threading.Barrier(2)

    def race(fn):
        barrier.wait(timeout=10)
        return move(fn, row)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(race, fn) for fn in (personal.claim_reminder, personal.cancel_reminder)]
        rows = [future.result(timeout=10) for future in results]
    assert sum(row is not None for row in rows) == 1


def test_delivery_retry_and_stale_acknowledgement():
    row = reminder()
    claimed = move(personal.claim_reminder, row)
    pending = personal.finish_reminder(row["reminder_id"], expected_revision=2, result="pending")
    assert pending["state"] == "pending" and pending["revision"] == 3
    assert pending["spoken_at"] is None
    claimed_again = move(personal.claim_reminder, pending)
    assert personal.finish_reminder(row["reminder_id"], expected_revision=claimed["revision"], result="spoken") is None
    spoken = personal.finish_reminder(row["reminder_id"], expected_revision=claimed_again["revision"], result="spoken")
    assert spoken["state"] == "spoken" and isinstance(spoken["spoken_at"], int)
    assert move(personal.claim_reminder, spoken) is None
    assert move(personal.cancel_reminder, spoken) is None


def test_due_order_and_stable_equal_times():
    later = reminder("later", 30)
    first = reminder("first", 10)
    second = reminder("second", 10)
    held = reminder("held", 5)
    move(personal.cancel_reminder, held)
    rows = personal.list_reminders(state="pending", due_before=10)
    assert {row["reminder_id"] for row in rows} == {first["reminder_id"], second["reminder_id"]}
    assert rows == personal.list_reminders(state="pending", due_before=10)
    assert personal.list_reminders(state="pending")[-1] == later
    assert len(personal.list_reminders(limit=1)) == 1


@pytest.mark.parametrize("kind,limit", [("reminder", 300), ("inbox", 1000)])
@pytest.mark.parametrize("multibyte", [False, True])
def test_byte_limits(kind, limit, multibyte):
    text = "é" * (limit // 2) if multibyte else "x" * limit
    add = reminder if kind == "reminder" else lambda **kw: personal.add_inbox(request_key="request", **kw)
    assert add(text=text)["text"] == text
    with pytest.raises(personal.PersonalStoreError) as error:
        add(text=text + "x")
    assert error.value.code == "invalid"


@pytest.mark.parametrize("text", ["", "   ", "\x00", "\ud800", None])
@pytest.mark.parametrize("kind", ["reminder", "inbox"])
def test_invalid_text(kind, text):
    with pytest.raises(personal.PersonalStoreError) as error:
        if kind == "reminder":
            reminder(text=text)
        else:
            personal.add_inbox(request_key="request", text=text)
    assert error.value.code == "invalid"
    assert not personal.database_path().exists()


@pytest.mark.parametrize("due", [True, 1.5, "10", None, -1, 2**63])
def test_invalid_due_at(due):
    with pytest.raises(personal.PersonalStoreError) as error:
        reminder(due=due)
    assert error.value.code == "invalid"


def test_busy_is_distinct(monkeypatch):
    row = reminder()
    monkeypatch.setattr(personal, "BUSY_TIMEOUT_MS", 1)
    with sqlite3.connect(personal.database_path()) as other:
        other.execute("BEGIN IMMEDIATE")
        with pytest.raises(personal.PersonalStoreError) as error:
            move(personal.claim_reminder, row)
        assert error.value.code == "busy" and error.value.exit_code == 3


def test_startup_recovery_and_timeout():
    pending = reminder("pending")
    active = move(personal.claim_reminder, reminder("active"))
    timed = move(personal.claim_reminder, reminder("timeout"))
    unknown = personal.finish_reminder(timed["reminder_id"], expected_revision=2, result="unknown")
    assert unknown["state"] == "unknown"
    assert personal.recover_delivering() == 1
    recovered = personal.get_reminder(active["reminder_id"])
    assert recovered["state"] == "unknown" and recovered["revision"] == 3
    assert recovered["spoken_at"] is None
    assert move(personal.claim_reminder, recovered) is None
    assert personal.recover_delivering() == 0
    assert personal.get_reminder(pending["reminder_id"]) == pending


def test_newest_pending_within_two_minutes(monkeypatch):
    now = 2000000000

    def at(offset, key):
        stamp = datetime.fromtimestamp(now + offset, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        monkeypatch.setattr(personal, "_now", lambda: stamp)
        return reminder(key)

    at(-121, "old")
    assert personal.newest_pending(now=now) is None
    boundary = at(-120, "boundary")
    assert personal.newest_pending(now=now) == boundary
    newest = at(-1, "newest")
    at(1, "future")
    assert personal.newest_pending(now=now) == newest
    move(personal.cancel_reminder, newest)
    assert personal.newest_pending(now=now) == boundary


def test_newest_pending_in_current_fractional_second(monkeypatch):
    monkeypatch.setattr(personal, "_now", lambda: "2033-05-18T03:33:20.500Z")
    monkeypatch.setattr(personal.time, "time", lambda: 2000000000.75)
    row = reminder()
    assert personal.newest_pending() == row


def test_inbox_order_remove_and_restart(monkeypatch):
    monkeypatch.setattr(personal, "_now", lambda: "2030-01-01T00:00:00.000Z")
    first = personal.add_inbox(request_key="first", text="example one")
    monkeypatch.setattr(personal, "_now", lambda: "2030-01-01T00:00:01.000Z")
    second = personal.add_inbox(request_key="second", text="example two")
    assert personal.list_inbox() == [second, first]
    assert personal.list_inbox(limit=1) == [second]
    assert personal.remove_inbox(first["item_id"]) == first
    assert personal.remove_inbox(first["item_id"]) is None
    assert personal.list_inbox() == [second]


def test_absent_reads_do_not_create_storage():
    assert personal.list_reminders() == []
    assert personal.list_inbox() == []
    assert personal.get_reminder("a" * 32) is None
    assert personal.newest_pending() is None
    assert not personal.database_path().exists()


def test_light_paths_skip_history_scans(monkeypatch):
    reminder()
    monkeypatch.setattr(personal, "verify_store", lambda: pytest.fail("integrity scan"))
    row = reminder("second")
    assert personal.list_reminders()
    assert personal.get_reminder(row["reminder_id"])
    assert personal.newest_pending()
    move(personal.claim_reminder, row)
    personal.recover_delivering()
    item = personal.add_inbox(request_key="note", text="example")
    assert personal.list_inbox()
    personal.remove_inbox(item["item_id"])


@pytest.mark.parametrize("damage,code", [("mode", "unsafe"), ("schema", "schema")])
def test_light_reads_retain_safety_checks(damage, code):
    reminder()
    if damage == "mode":
        personal.database_path().chmod(0o644)
    else:
        with sqlite3.connect(personal.database_path()) as conn:
            conn.execute("DROP TABLE inbox")
    with pytest.raises(personal.PersonalStoreError) as error:
        personal.list_reminders()
    assert error.value.code == code


def test_bad_mutation_inputs_do_not_change_row():
    row = reminder()
    for revision in (True, 0, "1"):
        with pytest.raises(personal.PersonalStoreError):
            personal.cancel_reminder(row["reminder_id"], expected_revision=revision)
    with pytest.raises(personal.PersonalStoreError):
        personal.finish_reminder(row["reminder_id"], expected_revision=1, result="cancelled")
    with pytest.raises(personal.PersonalStoreError):
        personal.cancel_reminder("invalid", expected_revision=1)
    assert personal.get_reminder(row["reminder_id"]) == row
