"""Isolated regressions for review fixes; never access desktop services."""
import asyncio
from datetime import datetime, timezone
import logging
import signal
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from heardback import coordinator
from heardback.tasks import personal
from test_coordinator import rig, isolated, add, state, lines


@pytest.mark.parametrize('result', ['spoken', 'refused', 'failed'])
def test_busy_finish_retries_outcome_without_replaying(rig, monkeypatch, result):
    b, c, clock = rig
    b.voice.result = result
    row = add(clock)
    original = personal.finish_reminder
    locked = True
    def finish(*args, **kwargs):
        if locked:
            raise personal._bad('busy', 'locked', 3)
        return original(*args, **kwargs)
    monkeypatch.setattr(personal, 'finish_reminder', finish)
    async def run():
        await c.tick()
        await c.tick()
        assert state(row) == 'delivering'
        assert len(lines(b)) == 1
    asyncio.run(run())
    locked = False
    asyncio.run(c.tick())
    assert state(row) == ('spoken' if result == 'spoken' else 'pending')
    assert c.outcome is None and len(lines(b)) == 1
    if result == 'spoken':
        asyncio.run(c.tick())
        assert len(lines(b)) == 1


def test_restart_with_retained_outcome_becomes_unknown(rig, monkeypatch):
    b, c, clock = rig
    row = add(clock)
    def busy(*a, **kw):
        raise personal._bad('busy', 'locked', 3)
    monkeypatch.setattr(personal, 'finish_reminder', busy)
    asyncio.run(c.tick())
    fresh = coordinator.Coordinator(b)
    assert asyncio.run(fresh.startup())
    assert state(row) == 'unknown'
    asyncio.run(fresh.tick())
    assert len(lines(b)) == 1


def test_startup_fails_twice_then_delivers(rig, monkeypatch):
    b, c, clock = rig
    row = add(clock, due=int(clock.now) + 30)
    original = personal.verify_store
    attempts = 0
    def verify():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise personal._bad('storage', 'bad')
        return original()
    monkeypatch.setattr(personal, 'verify_store', verify)
    waits = []
    async def sleep(delay):
        waits.append(delay)
        clock.now += delay
        if len(waits) == 4:
            raise asyncio.CancelledError
    monkeypatch.setattr(coordinator.asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(c.run())
    assert attempts == 3 and waits[:2] == [60, 60]
    assert state(row) == 'spoken'
    assert lines(b).count('Reminders are off, the storage check failed') == 1
    assert sum('Call mum' in text for text in lines(b)) == 1


def test_cancel_during_startup_retry(rig, monkeypatch):
    b, c, clock = rig
    def fail():
        raise personal._bad('storage', 'bad')
    monkeypatch.setattr(personal, 'verify_store', fail)
    async def run():
        waiting = asyncio.Event()
        original_sleep = asyncio.sleep
        async def sleep(delay):
            assert delay == 60
            waiting.set()
            await original_sleep(60)
        monkeypatch.setattr(coordinator.asyncio, 'sleep', sleep)
        task = asyncio.create_task(c.run())
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())


def test_release_permission_error_does_not_escape(rig, monkeypatch):
    _, c, _ = rig
    async def wait():
        return 0
    c.inhibitor = SimpleNamespace(pid=-999, wait=wait)
    def denied(*a):
        raise PermissionError('denied')
    monkeypatch.setattr(coordinator.os, 'killpg', denied)
    asyncio.run(c._release_inhibitor())
    assert c.inhibitor is None


def test_volume_outages_log_and_notify_once(rig, monkeypatch, caplog):
    b, c, clock = rig
    row = add(clock)
    failing = True
    def volume():
        if failing:
            raise RuntimeError('no sink')
        return 50, False
    monkeypatch.setattr(coordinator, 'volume_now', volume)
    caplog.set_level(logging.INFO, logger=coordinator.__name__)
    for _ in range(3):
        asyncio.run(c.tick())
    assert state(row) == 'pending'
    assert sum('volume check failed' in r.message for r in caplog.records) == 1
    assert len(lines(b)) == 1
    failing = False
    asyncio.run(c.tick())
    assert state(row) == 'spoken'
    assert sum('volume check recovered' in r.message for r in caplog.records) == 1
    add(clock, key='two')
    failing = True
    asyncio.run(c.tick())
    assert sum('volume check failed' in t for t in lines(b)) == 2


@pytest.mark.parametrize('hours,expected', [(11, 1), (13, 0)])
def test_unknown_catchup_window(rig, monkeypatch, hours, expected):
    b, c, clock = rig
    row = add(clock)
    stamp = datetime.fromtimestamp(clock.now - hours * 3600, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    monkeypatch.setattr(personal, '_now', lambda: stamp)
    personal.claim_reminder(row['reminder_id'], expected_revision=1)
    personal.recover_delivering()
    summary = personal.catchup_reminders(now=int(clock.now))
    assert summary['unknown_count'] == len(summary['unknown']) == expected
    assert state(row) == 'unknown'
    if not expected:
        from jv import catchup
        monkeypatch.setattr(catchup.time, 'time', lambda: clock.now)
        monkeypatch.setattr(catchup, "_session_overview", lambda: "No live local Claude Code sessions running.")
        assert catchup._catchup(None)["say"] == "Nothing new."


def test_tick_nonbusy_read_sets_notice(rig, monkeypatch):
    _, c, _ = rig
    def fail(**kw):
        raise personal._bad('storage', 'bad')
    monkeypatch.setattr(personal, 'list_reminders', fail)
    asyncio.run(c.tick())
    assert c.failure_notice


@pytest.mark.parametrize('reader', ['reminders', 'catchup', 'tick'])
def test_real_hot_journal_recovers_without_migration(rig, monkeypatch, tmp_path, reader):
    b, c, clock = rig
    row = add(clock, due=int(clock.now) + 1000)
    path = personal.database_path()
    with sqlite3.connect(path) as conn:
        conn.execute('PRAGMA journal_mode=DELETE')
    # Same spilled-page/SIGKILL method as opus11, with an uncommitted data update.
    script = '''import os, signal, sqlite3, sys
conn=sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("PRAGMA cache_size=1")
conn.execute("PRAGMA cache_spill=ON")
conn.execute("BEGIN IMMEDIATE")
conn.execute("CREATE TABLE crash_scratch (data BLOB)")
for i in range(64): conn.execute("INSERT INTO crash_scratch VALUES (zeroblob(4096))")
os.kill(os.getpid(), signal.SIGKILL)
'''
    child = subprocess.run([sys.executable, '-c', script, str(path)], capture_output=True)
    assert child.returncode == -signal.SIGKILL, child.stderr
    assert path.with_name(path.name + '-journal').exists()
    with pytest.raises(sqlite3.OperationalError) as caught:
        conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        try:
            conn.execute('SELECT count(*) FROM sqlite_master').fetchone()
        finally:
            conn.close()
    assert caught.value.sqlite_errorcode == sqlite3.SQLITE_READONLY_ROLLBACK
    if reader == 'reminders':
        from jv.cli import run
        assert run(['reminder', 'list']) == 0
    elif reader == 'catchup':
        from jv.cli import run
        assert run(['catchup']) == 0
    else:
        asyncio.run(c.tick())
        assert not c.failure_notice
    with sqlite3.connect(path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 4
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='crash_scratch'").fetchone() is None
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert personal.get_reminder(row['reminder_id']) == row


def test_refresh_inhibitor_guards_else_errors(rig, monkeypatch):
    _, c, clock = rig
    add(clock)
    class BadProcess:
        @property
        def returncode(self):
            raise OSError('process status unavailable')
    c.inhibitor = BadProcess()
    asyncio.run(c._refresh_inhibitor(clock.now))
    c.inhibitor = None


def test_release_wait_oserror_does_not_escape(rig, monkeypatch):
    _, c, _ = rig
    async def wait():
        raise OSError('wait failed')
    c.inhibitor = SimpleNamespace(pid=-999, wait=wait)
    monkeypatch.setattr(coordinator.os, 'killpg', lambda *args: None)
    asyncio.run(c._release_inhibitor())
    assert c.inhibitor is None
