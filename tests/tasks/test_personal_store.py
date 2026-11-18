"""Storage safety and pre-removal v4 compatibility, using disposable databases."""
import os
from pathlib import Path
import sqlite3

import pytest

from heardback.tasks import personal
from test_personal import isolated_state


def test_v4_database_keeps_personal_rows_and_ignores_retired_ledger(monkeypatch):
    path = personal.database_path()
    path.parent.mkdir(mode=0o700, parents=True)
    schema = Path(__file__).parents[1] / "fixtures" / "tasks-v4.sql"
    with sqlite3.connect(path) as conn:
        conn.executescript(schema.read_text())
        # Deliberately lacks task_events: retired ledger consistency must not gate reminders.
        conn.execute("INSERT INTO tasks VALUES(?, 'then', 'then', 1, 'old task', NULL, 's', 'a', "
                     "'local', 'unknown', NULL, 'queued', 'not_run')", ("c" * 32,))
        for i, state in enumerate(personal.STATES):
            conn.execute("INSERT INTO reminders VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (f"{i:032x}", f"request-{i}", f"saved {state}", 2_000_000_000 + i,
                          state, i + 1, "created", "updated", 1_999_999_999 if state == "spoken" else None))
        conn.execute("INSERT INTO inbox VALUES(?, 'note-key', 'saved note', 'created')", ("b" * 32,))
        before_schema = conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name").fetchall()
        old_tables = [r[1] for r in before_schema if r[0] == "table" and r[1] not in ("reminders", "inbox")]
        before_ledger = {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in old_tables}
        conn.row_factory = sqlite3.Row
        reminders = [dict(r) for r in conn.execute("SELECT * FROM reminders ORDER BY due_at, reminder_id")]
        inbox = [dict(r) for r in conn.execute("SELECT * FROM inbox")]
    path.chmod(0o600)
    before_bytes = path.read_bytes()
    assert personal.list_reminders() == reminders
    assert personal.list_inbox() == inbox
    assert path.read_bytes() == before_bytes
    personal.verify_store()
    assert personal.list_reminders() == reminders
    assert personal.list_inbox() == inbox
    assert personal.add_reminder(request_key="request-0", text="retry", due_at=1) == reminders[0]
    assert personal.add_inbox(request_key="note-key", text="retry") == inbox[0]
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert conn.execute("PRAGMA application_id").fetchone()[0] == 0x4A565453
        assert conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name").fetchall() == before_schema
        assert {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in old_tables} == before_ledger


def test_fresh_store_has_only_personal_tables_and_private_wal():
    old_umask = os.umask(0)
    try:
        personal.verify_store()
        path = personal.database_path()
        assert path.name == "tasks.sqlite3"
        assert path.parent.stat().st_mode & 0o777 == 0o700
        with personal._open(write=True) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {"reminders", "inbox"}
            assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
            for suffix in ("", "-wal", "-shm"):
                assert path.with_name(path.name + suffix).stat().st_mode & 0o777 == 0o600
    finally:
        os.umask(old_umask)


@pytest.mark.parametrize("damage", ["corrupt", "newer", "older", "application", "schema"])
@pytest.mark.parametrize("write", [False, True])
def test_unsupported_store_is_refused_without_changing_it(damage, write):
    personal.verify_store()
    path = personal.database_path()
    if damage == "corrupt":
        path.write_bytes(b"not sqlite")
    else:
        with sqlite3.connect(path) as conn:
            conn.execute({"newer": "PRAGMA user_version=99", "older": "PRAGMA user_version=3",
                          "application": "PRAGMA application_id=123", "schema": "DROP TABLE inbox"}[damage])
    before = path.read_bytes()
    with pytest.raises(personal.PersonalStoreError):
        with personal._open(write=write):
            pytest.fail("unsupported store accepted")
    assert path.read_bytes() == before


@pytest.mark.parametrize("damage", ["file_mode", "directory_mode", "symlink", "hardlink", "directory_symlink",
                                    "shared_parent", "journal", "wal", "shm"])
@pytest.mark.parametrize("write", [False, True])
def test_unsafe_paths_and_sidecars_are_refused(tmp_path, monkeypatch, damage, write):
    personal.verify_store()
    path = personal.database_path()
    if damage == "file_mode":
        path.chmod(0o644)
    elif damage == "directory_mode":
        path.parent.chmod(0o755)
    elif damage == "shared_parent":
        path.parent.parent.chmod(0o777)
    elif damage == "hardlink":
        os.link(path, tmp_path / "linked.sqlite3")
    elif damage in ("symlink", "directory_symlink"):
        target = path if damage == "symlink" else path.parent
        moved = target.with_name("original")
        target.rename(moved)
        target.symlink_to(moved)
    else:
        path.with_name(path.name + "-" + {"journal": "journal", "wal": "wal", "shm": "shm"}[damage]).symlink_to(path)
    with pytest.raises(personal.PersonalStoreError) as error:
        with personal._open(write=write):
            pytest.fail("unsafe store accepted")
    assert error.value.code == "unsafe"


def test_write_failure_rolls_back():
    personal.add_inbox(request_key="saved", text="keep")
    with pytest.raises(RuntimeError, match="interrupted"):
        with personal._open(write=True) as conn:
            conn.execute("DELETE FROM inbox")
            raise RuntimeError("interrupted")
    assert personal.list_inbox()[0]["text"] == "keep"


def test_polling_does_not_scan_integrity_or_retired_tables(monkeypatch):
    personal.verify_store()
    connect = sqlite3.connect
    statements = []

    def traced(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(personal.sqlite3, "connect", traced)
    personal.add_reminder(request_key="r", text="reminder", due_at=1)
    personal.list_reminders()
    personal.catchup_reminders(now=2)
    personal.add_inbox(request_key="i", text="note")
    personal.list_inbox()
    assert not any("quick_check" in s.lower() or "foreign_key_check" in s.lower() or "task_events" in s.lower()
                   for s in statements)
