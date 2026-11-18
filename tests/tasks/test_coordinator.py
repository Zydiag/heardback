"""Task 8: real private ledger rows; no live voice, state or memory."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from heardback import coordinator
from heardback.brain.brain import Brain
from heardback.status.announce import Announcer
from heardback.status import away
from heardback.tasks import personal


class Socket:
    def __init__(self, brain, result="spoken"):
        self.brain, self.result = brain, result
        self.sent = []
        self.closed = False

    def is_closing(self):
        return self.closed

    def write(self, data):
        msg = json.loads(data)
        self.sent.append(msg)
        if msg.get("delivery_id") and self.result:
            asyncio.get_running_loop().create_task(self.brain.handle(
                {"type": "delivery", "id": msg["delivery_id"], "result": self.result}, self))

    async def drain(self):
        pass


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert personal.database_path().is_relative_to(Path(os.environ["XDG_STATE_HOME"]))


@pytest.fixture
def rig(monkeypatch):
    clock = SimpleNamespace(now=1800000000.0)
    monkeypatch.setattr(coordinator.time, "time", lambda: clock.now)
    monkeypatch.setattr(away, "is_muted", lambda now: False)
    monkeypatch.setattr(coordinator, "volume_now", lambda: (50, False))
    b = Brain(announcer=Announcer(), voice_token="test-voice-token")
    b.voice = Socket(b)
    c = coordinator.Coordinator(b)
    return b, c, clock


def add(clock, *, text="Call mum", due=None, key="one"):
    return personal.add_reminder(request_key=key, text=text,
                                 due_at=int(clock.now) if due is None else due)


def state(row):
    return personal.get_reminder(row["reminder_id"])["state"]


def lines(b):
    return [m["text"] for m in b.voice.sent if m["type"] == "say"]


@pytest.mark.parametrize("late", [0, 120, 120.01])
def test_exact_on_time_and_late_boundary(rig, late):
    b, c, clock = rig
    row = add(clock)
    clock.now += late
    asyncio.run(c.tick())
    due = coordinator.datetime.fromtimestamp(row["due_at"]).strftime("%-I %M %p").lower()
    assert lines(b) == (["Reminder. Call mum."] if late <= 120 else
                        [f"Late reminder from {due}. Call mum."])
    assert state(row) == "spoken"


def test_exact_three_pm_late_wording(rig):
    b, c, clock = rig
    due = int(coordinator.datetime(2027, 1, 15, 15).timestamp())
    clock.now = due + 121
    row = add(clock, due=due)
    asyncio.run(c.tick())
    assert lines(b) == ["Late reminder from 3 pm. Call mum."] and state(row) == "spoken"


@pytest.mark.parametrize("gate", ["busy", "confirmation", "model", "mute", "sink", "gone", "closed"])
def test_gates_hold_then_speak_once(rig, monkeypatch, gate):
    b, c, clock = rig
    row = add(clock)
    socket = b.voice
    b.voice_busy = gate == "busy"
    b.awaiting = {socket: object()} if gate in {"confirmation", "model"} else {}
    monkeypatch.setattr(away, "is_muted", lambda now: gate == "mute")
    monkeypatch.setattr(coordinator, "volume_now", lambda: (50, gate == "sink"))
    if gate == "gone":
        b.voice = None
    socket.closed = gate == "closed"
    asyncio.run(c.tick())
    assert state(row) == "pending" and socket.sent == []
    b.voice, b.voice_busy, b.awaiting, socket.closed = socket, False, {}, False
    monkeypatch.setattr(away, "is_muted", lambda now: False)
    monkeypatch.setattr(coordinator, "volume_now", lambda: (50, False))
    asyncio.run(c.tick())
    asyncio.run(c.tick())
    assert lines(b) == ["Reminder. Call mum."] and state(row) == "spoken"


def test_typed_question_does_not_hold_voice_reminder(rig):
    b, c, clock = rig
    row = add(clock)
    b.awaiting[object()] = object()
    asyncio.run(c.tick())
    assert lines(b) == ["Reminder. Call mum."] and state(row) == "spoken"


def test_away_flag_speaks_in_full(rig):
    b, c, clock = rig
    from heardback.status import away
    away.flag_path().touch()
    row = add(clock)
    asyncio.run(c.tick())
    assert lines(b) == ["Reminder. Call mum."] and state(row) == "spoken"


def test_cold_startup_failure_waits_for_voice_once(rig):
    b, c, clock = rig
    socket, b.voice = b.voice, None
    path = personal.database_path()
    path.write_bytes(b"not a database")
    path.chmod(0o600)
    async def run():
        assert not await c.startup()
        assert socket.sent == []
        await b.handle({"type": "hello", "role": "voice", "token": b.voice_token}, socket)
        await b.handle({"type": "hello", "role": "voice", "token": b.voice_token}, socket)
        await asyncio.gather(*list(b._tasks))
    asyncio.run(run())
    assert lines(b) == ["Reminders are off, the storage check failed"]


def test_startup_creates_only_personal_tables(rig):
    b, c, clock = rig
    assert asyncio.run(c.startup())
    with sqlite3.connect(personal.database_path()) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {"reminders", "inbox"}
    assert lines(b) == [] and personal.list_reminders() == []


def test_restart_mid_speech_never_replays(rig):
    b, c, clock = rig
    row = add(clock)
    personal.claim_reminder(row["reminder_id"], expected_revision=1)
    async def run():
        assert await c.startup()
        await c.tick()
        await c.tick()
    asyncio.run(run())
    assert state(row) == "unknown" and lines(b) == []


def test_restart_before_due(rig):
    b, c, clock = rig
    row = add(clock, due=int(clock.now) + 60)
    async def run():
        assert await c.startup()
        await c.tick()
        assert lines(b) == [] and state(row) == "pending"
        clock.now += 60
        await c.tick()
    asyncio.run(run())
    assert lines(b) == ["Reminder. Call mum."] and state(row) == "spoken"


def test_cancelled_while_held(rig):
    b, c, clock = rig
    row = add(clock)
    b.voice_busy = True
    asyncio.run(c.tick())
    personal.cancel_reminder(row["reminder_id"], expected_revision=1)
    b.voice_busy = False
    asyncio.run(c.tick())
    assert state(row) == "cancelled" and lines(b) == []


def test_cancel_between_gate_and_claim(rig, monkeypatch):
    b, c, clock = rig
    row = add(clock)
    def sink():
        personal.cancel_reminder(row["reminder_id"], expected_revision=1)
        return 50, False
    monkeypatch.setattr(coordinator, "volume_now", sink)
    asyncio.run(c.tick())
    assert state(row) == "cancelled" and lines(b) == []


@pytest.mark.parametrize("result", ["refused", "failed"])
def test_safe_retry_and_failed_backoff(rig, result):
    b, c, clock = rig
    row = add(clock)
    b.voice.result = result
    asyncio.run(c.tick())
    assert state(row) == "pending" and lines(b) == ["Reminder. Call mum."]
    b.voice.result = "spoken"
    if result == "failed":
        clock.now += 59.99
        asyncio.run(c.tick())
        assert len(lines(b)) == 1 and state(row) == "pending"
        clock.now += 0.01
    asyncio.run(c.tick())
    asyncio.run(c.tick())
    assert state(row) == "spoken" and lines(b) == ["Reminder. Call mum."] * 2
    assert len({m["delivery_id"] for m in b.voice.sent}) == 2


def test_no_reply_and_late_reply_stays_unknown(rig, monkeypatch):
    b, c, clock = rig
    row = add(clock)
    b.voice.result = None
    monkeypatch.setattr(coordinator, "DELIVERY_TIMEOUT_S", 0.01)
    async def run():
        await c.tick()
        msg, = b.voice.sent
        await b.handle({"type": "delivery", "id": msg["delivery_id"], "result": "spoken"}, b.voice)
        await c.tick()
    asyncio.run(run())
    assert state(row) == "unknown" and lines(b) == ["Reminder. Call mum."]


def test_two_due_rows_oldest_first_serial(rig):
    b, c, clock = rig
    newer = add(clock, key="newer", text="Second")
    older = add(clock, key="older", text="First", due=int(clock.now) - 10)
    asyncio.run(c.tick())
    assert lines(b) == ["Reminder. First."]
    assert state(older) == "spoken" and state(newer) == "pending"
    asyncio.run(c.tick())
    assert lines(b) == ["Reminder. First.", "Reminder. Second."] and state(newer) == "spoken"


def test_old_attempt_ack_does_not_finish_new_attempt(rig, monkeypatch):
    b, c, clock = rig
    row = add(clock)
    b.voice.result = "refused"
    async def run():
        await c.tick()
        old = b.voice.sent[0]["delivery_id"]
        b.voice.result = None
        monkeypatch.setattr(coordinator, "DELIVERY_TIMEOUT_S", 0.02)
        next_tick = asyncio.create_task(c.tick())
        while len(b.voice.sent) < 2:
            await asyncio.sleep(0)
        await b.handle({"type": "delivery", "id": old, "result": "spoken"}, b.voice)
        await next_tick
    asyncio.run(run())
    assert state(row) == "unknown" and lines(b) == ["Reminder. Call mum."] * 2


def test_send_drain_is_inside_delivery_timeout(rig, monkeypatch):
    b, c, clock = rig
    row = add(clock)
    b.voice.result = None
    async def blocked_drain():
        await asyncio.Future()
    monkeypatch.setattr(b.voice, "drain", blocked_drain)
    monkeypatch.setattr(coordinator, "DELIVERY_TIMEOUT_S", 0.01)
    asyncio.run(asyncio.wait_for(c.tick(), 1))
    assert state(row) == "unknown" and lines(b) == ["Reminder. Call mum."]


def test_foreign_socket_ack_cannot_finish(rig, monkeypatch):
    b, c, clock = rig
    row = add(clock)
    b.voice.result = None
    monkeypatch.setattr(coordinator, "DELIVERY_TIMEOUT_S", 0.03)
    async def run():
        tick = asyncio.create_task(c.tick())
        while not b.voice.sent:
            await asyncio.sleep(0)
        msg, = b.voice.sent
        await b.handle({"type": "delivery", "id": msg["delivery_id"], "result": "spoken"}, object())
        await tick
    asyncio.run(run())
    assert state(row) == "unknown" and lines(b) == ["Reminder. Call mum."]


def test_reconnect_clears_busy(rig):
    b, c, clock = rig
    b.voice_busy = True
    row = add(clock)
    async def run():
        await b.handle({"type": "hello", "role": "voice", "token": b.voice_token}, b.voice)
        await c.tick()
    asyncio.run(run())
    assert lines(b) == ["Reminder. Call mum."] and state(row) == "spoken"


def test_ledger_busy_tick_survives(rig):
    b, c, clock = rig
    row = add(clock)
    with sqlite3.connect(personal.database_path()) as conn:
        conn.execute("BEGIN EXCLUSIVE")
        asyncio.run(c.tick())
        conn.rollback()
    assert state(row) == "pending" and lines(b) == []
    asyncio.run(c.tick())
    assert state(row) == "spoken" and lines(b) == ["Reminder. Call mum."]


def test_startup_failure_once_leaves_brain_up(rig, monkeypatch):
    b, c, clock = rig
    path = personal.database_path()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.write_bytes(b"not a database")
    path.chmod(0o600)
    async def cancel_retry(delay):
        assert delay == 60
        raise asyncio.CancelledError
    monkeypatch.setattr(coordinator.asyncio, "sleep", cancel_retry)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(c.run())
    assert lines(b) == ["Reminders are off, the storage check failed"]
    assert b.voice is not None and path.read_bytes() == b"not a database"


def test_blocking_store_and_mute_work_off_loop(rig, monkeypatch):
    b, c, clock = rig
    row = add(clock)
    loop_thread = threading.get_ident()
    threads = []
    # Observe real storage calls rather than replacing their behavior.
    for obj, name in [(personal, "verify_store"),
                      (personal, "recover_delivering"), (personal, "list_reminders"),
                      (personal, "claim_reminder"), (personal, "finish_reminder"),
                      (away, "is_muted"), (coordinator, "volume_now")]:
        fn = getattr(obj, name)
        def observed(*a, _fn=fn, **kw):
            threads.append(threading.get_ident())
            return _fn(*a, **kw)
        monkeypatch.setattr(obj, name, observed)
    async def run():
        assert await c.startup()
        await c.tick()
    asyncio.run(run())
    assert len(threads) >= 7 and loop_thread not in threads
    assert lines(b) == ["Reminder. Call mum."] and state(row) == "spoken"


def test_second_daemon_never_runs_recovery(rig, runtime_dir):
    b, c, clock = rig
    row = add(clock, due=int(clock.now) + 1000)
    interrupted = add(clock, key="interrupted", due=int(clock.now) + 1000)
    personal.claim_reminder(interrupted["reminder_id"], expected_revision=1)
    async def run():
        path = Path(runtime_dir) / "brain.sock"
        first = asyncio.create_task(b.serve(path))
        try:
            while not path.exists() or state(interrupted) != "unknown":
                await asyncio.sleep(0.005)
            personal.claim_reminder(row["reminder_id"], expected_revision=1)
            with pytest.raises(SystemExit, match="already running"):
                await Brain().serve(path)
            assert state(row) == "delivering" and lines(b) == []
        finally:
            first.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await first
    asyncio.run(run())


def test_shutdown_during_delivery_recovers_unknown(rig):
    b, c, clock = rig
    row = add(clock)
    b.voice.result = None
    async def run():
        task = asyncio.create_task(c.tick())
        while not b.voice.sent:
            await asyncio.sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert state(row) == "delivering"
        assert await coordinator.Coordinator(b).startup()
        await b.coordinator.tick()
    asyncio.run(run())
    assert state(row) == "unknown" and lines(b) == ["Reminder. Call mum."]


def test_startup_busy_retries_after_sixty_seconds(rig, monkeypatch):
    b, c, clock = rig
    row = add(clock)
    conn = sqlite3.connect(personal.database_path())
    conn.execute("BEGIN EXCLUSIVE")
    sleeps = []
    async def sleep(delay):
        sleeps.append(delay)
        conn.rollback()
        clock.now += delay
    monkeypatch.setattr(coordinator.asyncio, "sleep", sleep)
    try:
        assert asyncio.run(c.startup())
    finally:
        conn.close()
    assert sleeps == [60] and lines(b) == [] and state(row) == "pending"
    asyncio.run(c.tick())
    assert lines(b) == ["Reminder. Call mum."] and state(row) == "spoken"


def test_resume_gap_settles_ten_seconds_before_delivery(rig, monkeypatch):
    b, c, clock = rig
    row = add(clock, due=int(clock.now) + 60)
    sleeps = []
    async def sleep(delay):
        sleeps.append(delay)
        assert lines(b) == [] and state(row) == "pending"
        if len(sleeps) == 1:
            clock.now += 180
        elif len(sleeps) == 2:
            clock.now += delay
        else:
            raise asyncio.CancelledError
    monkeypatch.setattr(coordinator.asyncio, "sleep", sleep)
    async def run():
        with pytest.raises(asyncio.CancelledError):
            await c.run()
    # The third sleep occurs only after speech; assert that separately.
    async def bounded_sleep(delay):
        if len(sleeps) == 2:
            raise asyncio.CancelledError
        await sleep(delay)
    monkeypatch.setattr(coordinator.asyncio, "sleep", bounded_sleep)
    asyncio.run(run())
    assert sleeps == [5, 10]
    due = coordinator.datetime.fromtimestamp(row["due_at"]).strftime("%-I %M %p").lower()
    assert lines(b) == [f"Late reminder from {due}. Call mum."] and state(row) == "spoken"
