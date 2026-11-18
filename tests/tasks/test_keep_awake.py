"""Task 9 keep-awake lifecycle with private ledger rows and fake process."""
import asyncio
import json
import logging
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from heardback import coordinator
from heardback.brain.brain import Brain
from heardback.status.announce import Announcer
from heardback.status import away
from heardback.tasks import personal


class Voice:
    def __init__(self, brain):
        self.brain = brain
        self.sent = asyncio.Event()
        self.auto_ack = True

    def is_closing(self):
        return False

    def write(self, data):
        msg = json.loads(data)
        if msg.get("delivery_id") and self.auto_ack:
            asyncio.get_running_loop().create_task(self.brain.handle(
                {"type": "delivery", "id": msg["delivery_id"], "result": "spoken"}, self))
        self.sent.set()

    async def drain(self):
        pass


class Inhibitor:
    def __init__(self, pid, *, exit_on=()):
        self.pid = pid
        self.returncode = None
        self.terminated = False
        self.exit_on = exit_on
        self.signals = []
        self.signal_times = []
        self.exited = asyncio.Event()
        self.wait_started = asyncio.Event()
        self.killed = asyncio.Event()

    def exit(self, code):
        self.returncode = code
        self.exited.set()

    def receive(self, sig):
        if self.returncode is not None:
            raise ProcessLookupError
        self.signals.append(sig)
        self.signal_times.append(time.monotonic())
        self.terminated = True
        if sig == signal.SIGKILL:
            self.killed.set()
        if sig in self.exit_on:
            asyncio.get_running_loop().call_soon(self.exit, -sig)

    async def wait(self):
        self.wait_started.set()
        await self.exited.wait()
        return self.returncode


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch, _safe_env):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert personal.database_path().is_relative_to(Path(os.environ["XDG_STATE_HOME"]))
    async def forbidden(*args, **kwargs):
        pytest.fail("real subprocess forbidden in keep-awake tests")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    return _safe_env


@pytest.fixture
def rig(monkeypatch):
    clock = SimpleNamespace(now=1800000000.0)
    monkeypatch.setattr(coordinator.time, "time", lambda: clock.now)
    monkeypatch.setattr(away, "is_muted", lambda now: False)
    monkeypatch.setattr(coordinator, "volume_now", lambda: (50, False))
    brain = Brain(announcer=Announcer())
    brain.voice = Voice(brain)
    c = coordinator.Coordinator(brain)
    processes, argvs = [], []

    def fake_killpg(pid, sig):
        for proc in processes:
            if proc.pid == pid:
                proc.receive(sig)
                return
        raise ProcessLookupError

    monkeypatch.setattr(coordinator.os, "killpg", fake_killpg)

    async def fake_exec(*argv, **kwargs):
        assert argv[0] == "systemd-inhibit", "real inhibitor must never run in tests"
        proc = Inhibitor(40000 + len(processes), exit_on=(signal.SIGTERM, signal.SIGKILL))
        processes.append(proc)
        argvs.append((argv, kwargs))
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return c, clock, processes, argvs


def add(clock, *, due, key="one"):
    return personal.add_reminder(request_key=key, text="Call mum", due_at=due)


def run(c):
    asyncio.run(c.tick())


@pytest.mark.parametrize("minutes,held", [(59, True), (60, True), (61, False)])
def test_pending_due_window_boundaries(rig, minutes, held):
    c, clock, processes, _ = rig
    add(clock, due=int(clock.now + minutes * 60))
    run(c)
    assert bool(processes) is held


def test_single_inhibitor_survives_ticks_and_second_row(rig):
    c, clock, processes, _ = rig
    add(clock, due=int(clock.now + 1000))
    run(c)
    add(clock, due=int(clock.now + 2000), key="two")
    run(c)
    run(c)
    assert len(processes) == 1 and processes[0].returncode is None


def test_release_after_last_row_fires(rig):
    c, clock, processes, _ = rig
    row = add(clock, due=int(clock.now + 1))
    run(c)
    clock.now += 1
    run(c)
    assert personal.get_reminder(row["reminder_id"])["state"] == "spoken"
    assert len(processes) == 1 and processes[0].terminated


def test_release_after_last_row_fires_and_keep_for_another(rig):
    c, clock, processes, _ = rig
    first = add(clock, due=int(clock.now + 1))
    second = add(clock, due=int(clock.now + 1000), key="two")
    run(c)
    clock.now += 1
    run(c)
    assert personal.get_reminder(first["reminder_id"])["state"] == "spoken"
    assert len(processes) == 1 and processes[0].returncode is None
    personal.cancel_reminder(second["reminder_id"], expected_revision=1)
    run(c)
    assert processes[0].terminated


def test_release_after_cancel(rig):
    c, clock, processes, _ = rig
    row = add(clock, due=int(clock.now + 1000))
    run(c)
    personal.cancel_reminder(row["reminder_id"], expected_revision=1)
    run(c)
    assert len(processes) == 1 and processes[0].terminated


def test_release_on_shutdown_and_run_cancellation_mid_tick(rig, monkeypatch):
    c, clock, processes, _ = rig
    row = add(clock, due=int(clock.now + 1))
    later = add(clock, due=int(clock.now + 1000), key="later")
    run(c)
    clock.now += 1
    c.brain.voice.auto_ack = False
    real_sleep = asyncio.sleep
    monkeypatch.setattr(coordinator.asyncio, "sleep", lambda delay: real_sleep(0))
    async def run_loop():
        task = asyncio.create_task(c.run())
        await asyncio.wait_for(c.brain.voice.sent.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run_loop())
    assert len(processes) == 1 and processes[0].returncode == -signal.SIGTERM
    assert c.inhibitor is None and c.delivery is None
    assert personal.get_reminder(row["reminder_id"])["state"] == "delivering"
    assert personal.get_reminder(later["reminder_id"])["state"] == "pending"


def test_inhibitor_argv_ties_child_to_parent_lifetime(rig):
    c, clock, processes, argvs = rig
    add(clock, due=int(clock.now + 1000))
    run(c)
    argv, kwargs = argvs[0]
    assert argv == ("systemd-inhibit", "--what=sleep", "--mode=block",
                    "--who=heardback", "--why=Pending reminder", "tail",
                    f"--pid={os.getpid()}", "-f", "/dev/null")
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == kwargs["stdout"] == asyncio.subprocess.DEVNULL
    assert "stderr" not in kwargs


def test_dead_inhibitor_restarts_next_tick(rig, caplog):
    c, clock, processes, _ = rig
    add(clock, due=int(clock.now + 1000))
    run(c)
    clock.now += coordinator.RETRY_S + 1
    processes[0].exit(1)
    run(c)
    assert len(processes) == 2 and processes[1].returncode is None
    assert c.inhibitor is processes[1] and c.inhibitor_retry_at == 0
    assert "status 1" in caplog.text


def test_failed_start_logs_allows_delivery_and_retries_at_sixty_seconds(rig, monkeypatch, caplog):
    c, clock, processes, _ = rig
    row = add(clock, due=int(clock.now))
    future = add(clock, due=int(clock.now + 1000), key="future")
    original = asyncio.create_subprocess_exec
    calls = []
    async def fail_once(*argv, **kwargs):
        calls.append(clock.now)
        if len(calls) == 1:
            raise OSError("inhibitor unavailable")
        return await original(*argv, **kwargs)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_once)
    with caplog.at_level(logging.ERROR):
        run(c)
    assert personal.get_reminder(row["reminder_id"])["state"] == "spoken"
    assert len(calls) == 1 and "inhibitor" in caplog.text.lower()
    run(c)
    assert len(calls) == 1
    clock.now += 60
    run(c)
    assert len(calls) == 2 and len(processes) == 1
    assert personal.get_reminder(future["reminder_id"])["state"] == "pending"


def test_muted_row_releases_after_ten_minutes_then_speaks_when_unmuted(rig, monkeypatch):
    c, clock, processes, _ = rig
    row = add(clock, due=int(clock.now))
    monkeypatch.setattr(away, "is_muted", lambda now: True)
    run(c)
    assert len(processes) == 1 and personal.get_reminder(row["reminder_id"])["state"] == "pending"
    clock.now += 600
    run(c)
    assert not processes[0].terminated
    clock.now += 0.01
    run(c)
    assert processes[0].terminated and personal.get_reminder(row["reminder_id"])["state"] == "pending"
    monkeypatch.setattr(away, "is_muted", lambda now: False)
    run(c)
    assert personal.get_reminder(row["reminder_id"])["state"] == "spoken"


@pytest.mark.parametrize("state", ["delivering", "unknown"])
def test_non_pending_rows_do_not_hold_inhibitor(rig, state):
    c, clock, processes, _ = rig
    row = add(clock, due=int(clock.now + 1000))
    if state == "delivering":
        personal.claim_reminder(row["reminder_id"], expected_revision=1)
    else:
        personal.claim_reminder(row["reminder_id"], expected_revision=1)
        personal.finish_reminder(row["reminder_id"], expected_revision=2, result="unknown")
    run(c)
    assert processes == []


def test_inhibitor_start_is_nonblocking(rig, monkeypatch):
    c, clock, _, _ = rig
    row = add(clock, due=int(clock.now + 1000))
    loop_thread = __import__("threading").get_ident()
    threads = []
    list_rows = personal.list_reminders
    def observed(*args, **kwargs):
        threads.append(__import__("threading").get_ident())
        return list_rows(*args, **kwargs)
    monkeypatch.setattr(personal, "list_reminders", observed)
    # The event loop must remain able to schedule while process startup is pending.
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked_exec(*argv, **kwargs):
        assert argv[0] == "systemd-inhibit"
        entered.set()
        await release.wait()
        return Inhibitor(50000)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", blocked_exec)
    async def check():
        tick = asyncio.create_task(c.tick())
        await entered.wait()
        assert not tick.done() and personal.database_path().exists()
        release.set()
        await tick
    asyncio.run(check())
    assert personal.get_reminder(row["reminder_id"])["state"] == "pending"
    assert threads and loop_thread not in threads


@pytest.mark.parametrize("already_held", [False, True])
def test_old_backlog_does_not_hide_future_row(rig, monkeypatch, already_held):
    c, clock, processes, _ = rig
    future = add(clock, due=int(clock.now + 59 * 60), key="future")
    if already_held:
        run(c)
    for n in range(100):
        add(clock, due=int(clock.now - 601 - n), key=f"old-{n}")
    monkeypatch.setattr(away, "is_muted", lambda now: True)
    run(c)
    assert len(processes) == 1 and c.inhibitor is processes[0]
    assert processes[0].returncode is None and processes[0].signals == []
    assert personal.get_reminder(future["reminder_id"])["state"] == "pending"
    assert len(personal.list_reminders(state="pending", limit=100)) == 100


@pytest.mark.parametrize("offset,held", [(-600, False), (-599, True),
                                         (3600, True), (3601, False)])
def test_fractional_time_window_bounds(rig, monkeypatch, offset, held):
    c, clock, processes, _ = rig
    clock.now += .5
    row = add(clock, due=int(clock.now) + offset)
    monkeypatch.setattr(away, "is_muted", lambda now: True)
    run(c)
    assert (c.inhibitor is not None) is held
    assert len(processes) == int(held)
    assert personal.get_reminder(row["reminder_id"])["state"] == "pending"


def test_ignores_term_then_killed_and_later_reminder_delivered(rig, monkeypatch):
    c, clock, processes, _ = rig
    row = add(clock, due=int(clock.now + 100))
    run(c)
    proc = processes[0]
    proc.exit_on = (signal.SIGKILL,)
    personal.cancel_reminder(row["reminder_id"], expected_revision=1)
    assert coordinator.INHIBITOR_GRACE_S == 5
    monkeypatch.setattr(coordinator, "INHIBITOR_GRACE_S", .02)
    run(c)
    assert proc.signals == [signal.SIGTERM, signal.SIGKILL]
    assert proc.signal_times[1] - proc.signal_times[0] >= .02
    assert proc.returncode == -signal.SIGKILL and c.inhibitor is None
    later = add(clock, due=int(clock.now + 1), key="later")
    clock.now += 1
    run(c)
    assert personal.get_reminder(later["reminder_id"])["state"] == "spoken"
    assert personal.get_reminder(row["reminder_id"])["state"] == "cancelled"


def test_ignores_both_signals_drops_reference_and_logs(rig, monkeypatch, caplog):
    c, clock, processes, _ = rig
    row = add(clock, due=int(clock.now + 100))
    run(c)
    proc = processes[0]
    proc.exit_on = ()
    personal.cancel_reminder(row["reminder_id"], expected_revision=1)
    monkeypatch.setattr(coordinator, "INHIBITOR_GRACE_S", .02)
    async def check():
        await asyncio.wait_for(c.tick(), .5)
    asyncio.run(check())
    assert proc.signals == [signal.SIGTERM, signal.SIGKILL]
    assert proc.returncode is None and c.inhibitor is None
    assert "not reaped after SIGKILL" in caplog.text
    assert personal.get_reminder(row["reminder_id"])["state"] == "cancelled"


@pytest.mark.parametrize("during_kill_wait", [False, True])
def test_second_cancel_during_release_kills_and_clears(rig, monkeypatch, during_kill_wait):
    c, clock, processes, _ = rig
    row = add(clock, due=int(clock.now + 100))
    run(c)
    proc = processes[0]
    proc.exit_on = ()
    monkeypatch.setattr(coordinator, "INHIBITOR_GRACE_S", .02 if during_kill_wait else 5)
    sleeping = asyncio.Event()
    async def idle(delay):
        sleeping.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(coordinator.asyncio, "sleep", idle)
    async def check():
        task = asyncio.create_task(c.run())
        await asyncio.wait_for(sleeping.wait(), 1)
        task.cancel()
        await asyncio.wait_for(proc.wait_started.wait(), 1)
        if during_kill_wait:
            await asyncio.wait_for(proc.killed.wait(), 1)
        assert proc.signals == ([signal.SIGTERM, signal.SIGKILL] if during_kill_wait
                                else [signal.SIGTERM])
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
    asyncio.run(check())
    assert proc.signals == [signal.SIGTERM] + [signal.SIGKILL] * (2 if during_kill_wait else 1)
    assert c.inhibitor is None
    assert personal.get_reminder(row["reminder_id"])["state"] == "pending"


def test_cancel_during_idle_sleep_releases(rig, monkeypatch):
    c, clock, processes, _ = rig
    row = add(clock, due=int(clock.now + 100))
    run(c)
    sleeping = asyncio.Event()
    async def idle(delay):
        sleeping.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(coordinator.asyncio, "sleep", idle)
    async def check():
        task = asyncio.create_task(c.run())
        await asyncio.wait_for(sleeping.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
    asyncio.run(check())
    assert processes[0].returncode == -signal.SIGTERM
    assert processes[0].signals == [signal.SIGTERM] and c.inhibitor is None
    assert personal.get_reminder(row["reminder_id"])["state"] == "pending"


def test_immediate_nonzero_exit_backs_off_and_logs(rig, monkeypatch, caplog):
    c, clock, processes, _ = rig
    row = add(clock, due=int(clock.now + 1000))
    original = asyncio.create_subprocess_exec
    async def failed_child(*argv, **kwargs):
        proc = await original(*argv, **kwargs)
        proc.exit(1)
        return proc
    monkeypatch.setattr(asyncio, "create_subprocess_exec", failed_child)
    run(c)
    run(c)
    assert len(processes) == 1 and c.inhibitor is None
    clock.now += 59
    run(c)
    assert len(processes) == 1
    clock.now += 1
    run(c)
    assert len(processes) == 2 and c.inhibitor is None
    assert all(p.returncode == 1 for p in processes)
    assert "status 1" in caplog.text
    assert personal.get_reminder(row["reminder_id"])["state"] == "pending"


@pytest.mark.parametrize("age", [0, 59, 60])
def test_early_exit_observed_next_tick_backs_off(rig, caplog, age):
    c, clock, processes, _ = rig
    row = add(clock, due=int(clock.now + 1000))
    run(c)
    clock.now += age
    processes[0].exit(7)
    run(c)
    assert len(processes) == 1 and c.inhibitor is None
    assert c.inhibitor_retry_at == clock.now + coordinator.RETRY_S
    assert "status 7" in caplog.text
    clock.now += 59
    run(c)
    assert len(processes) == 1
    clock.now += 1
    run(c)
    assert len(processes) == 2 and c.inhibitor is processes[1]
    assert processes[1].returncode is None
    assert personal.get_reminder(row["reminder_id"])["state"] == "pending"


def test_busy_pre_wal_store_releases_stale_lock_then_delivery_resumes(rig, monkeypatch, caplog):
    c, clock, processes, _ = rig
    row = add(clock, due=int(clock.now + 100))
    run(c)
    good_at = c.inhibitor_checked_at
    personal.cancel_reminder(row["reminder_id"], expected_revision=1)
    monkeypatch.setattr(personal, "BUSY_TIMEOUT_MS", 20)
    conn = sqlite3.connect(personal.database_path())
    try:
        # Existing v4 files can still be in rollback mode before their first new write.
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("BEGIN EXCLUSIVE")
        clock.now += coordinator.KEEP_AWAKE_OVERDUE_S
        run(c)
        assert c.inhibitor is processes[0] and processes[0].returncode is None
        assert c.inhibitor_checked_at == good_at
        clock.now += .01
        run(c)
        assert c.inhibitor is None and processes[0].returncode == -signal.SIGTERM
        assert c.inhibitor_checked_at == good_at
    finally:
        conn.rollback()
        conn.close()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    due = add(clock, due=int(clock.now), key="due")
    run(c)
    assert personal.get_reminder(due["reminder_id"])["state"] == "spoken"
    assert personal.get_reminder(row["reminder_id"])["state"] == "cancelled"
    assert c.inhibitor is None and c.inhibitor_checked_at == clock.now


@pytest.mark.parametrize("state", ["delivering", "unknown"])
def test_held_inhibitor_released_when_only_row_changes_state(rig, state):
    c, clock, processes, _ = rig
    row = add(clock, due=int(clock.now + 100))
    run(c)
    personal.claim_reminder(row["reminder_id"], expected_revision=1)
    if state == "unknown":
        personal.finish_reminder(row["reminder_id"], expected_revision=2, result=state)
    run(c)
    assert c.inhibitor is None and processes[0].returncode == -signal.SIGTERM
    assert personal.get_reminder(row["reminder_id"])["state"] == state


def test_one_light_refresh_per_tick(rig, monkeypatch):
    c, clock, _, _ = rig
    add(clock, due=int(clock.now + 100))
    original = personal.list_reminders
    calls = []
    def observed(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(personal, "list_reminders", observed)
    def no_history_scan(*args, **kwargs):
        pytest.fail("polling must not scan ledger history")
    monkeypatch.setattr(personal, "verify_store", no_history_scan)
    run(c)
    assert len(calls) == 2
    assert calls[0] == {"state": "pending", "due_before": int(clock.now), "limit": 1}
    assert calls[1] == {"state": "pending", "due_after": int(clock.now - 600),
                        "due_before": int(clock.now + 3600), "limit": 1}


@pytest.mark.parametrize("value", [True, -1, 1.5, "1"])
def test_due_after_validated(rig, value):
    with pytest.raises(personal.PersonalStoreError) as exc:
        personal.list_reminders(due_after=value)
    assert exc.value.code == "invalid"


def test_global_guard_blocks_inhibitor_and_passes_other_program(isolated):
    async def check():
        for program in ("systemd-inhibit", "/usr/bin/systemd-inhibit"):
            with pytest.raises(OSError, match="forbidden in tests"):
                await isolated(program, "--what=sleep", "true")
        proc = await isolated(sys.executable, "-c", "print('guard passthrough')",
                              stdout=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(proc.communicate(), 5)
        assert proc.returncode == 0 and out == b"guard passthrough\n"
    asyncio.run(check())
