"""Exercise optional paste through the real dictation ledger with fake desktop I/O."""
import asyncio
import threading
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from time import monotonic
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

np = pytest.importorskip("numpy")

from heardback.voice import desktop_dictation as core
from heardback.voice import desktop_input as boundary
from heardback.voice.chunker import Chunk

PIN = boundary.Pin("0x123", "kitty", 42)
TEXT = "several words beyond the tiny slice limit"


class DesktopGuards(pytest.MonkeyPatch):
    def __init__(self):
        super().__init__()
        self.executors = []
        self.setattr(boundary.subprocess, "Popen", ProcessHarness.forbidden)
        self.setattr(boundary.subprocess, "run", ProcessHarness.forbidden)
        self.setattr(boundary.hypr, "hyprctl", ProcessHarness.forbidden)

    def restore(self):
        assert not any(t.is_alive() for executor in self.executors for t in executor._threads), \
            "desktop workers still running; I/O guards retained"
        self.undo()


@pytest.fixture(autouse=True)
def desktop_guards(monkeypatch):
    guards = DesktopGuards()
    monkeypatch._paste_guards = guards
    yield guards
    guards.restore()


def join_executors(executors, timeout=0.5):
    deadline = monotonic() + timeout
    for executor in executors:
        executor.shutdown(wait=False, cancel_futures=True)
    for executor in executors:
        for thread in executor._threads:
            thread.join(max(0, deadline - monotonic()))
    assert not any(t.is_alive() for executor in executors for t in executor._threads), \
        "desktop workers still running; I/O guards retained"


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ProcessHarness:
    def __init__(self, monkeypatch, *, failure=None, clock=None, costs=None, callback=None):
        self.failure, self.clock = failure, clock or Clock()
        self.costs = costs or {}
        self.callback = callback
        self.events, self.processes, self.commands = [], [], []
        self.started_at = None
        guards = monkeypatch._paste_guards
        guards.setattr(boundary.shutil, "which", self.which)
        guards.setattr(boundary.subprocess, "Popen", self.popen)

    @staticmethod
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected process or compositor call")

    @staticmethod
    def which(name):
        if name == "wl-copy":
            return "/usr/bin/wl-copy"
        raise AssertionError(f"unexpected executable lookup: {name}")

    def popen(self, argv, **kwargs):
        assert argv in (
            ["/usr/bin/wl-copy", "--sensitive"],
            ["wtype", "-d", "20", "-M", "ctrl", "v", "-m", "ctrl"],
            ["wtype", "-m", "ctrl"],
            ["wtype", *(v for c in boundary._RELEASE_SYMBOLS for v in ("-p", c))],
        )
        assert kwargs.get("stdout") is not None and kwargs.get("stderr") is not None
        role = ("recovery" if "-p" in argv else
                "copy" if "wl-copy" in argv[0] else ("release" if "-M" not in argv else "chord"))
        if role == "release" and self.failure == "release_start":
            raise OSError("fake release startup failure")
        self.commands.append(argv)
        if role == "copy":
            self.started_at = self.clock()
        self.events.append(("start", role))
        process = FakeProcess(self, role)
        self.processes.append(process)
        return process


class FakeProcess:
    def __init__(self, harness, role):
        self.harness, self.role, self.returncode = harness, role, 0
        self.killed = self.reaped = self.timed_out = self.completed = False

    def communicate(self, input=None, timeout=None):
        h, role = self.harness, self.role
        h.events.append(("communicate", role, input, timeout))
        assert timeout is not None and timeout >= 0
        if role == "copy":
            assert isinstance(input, bytes)
            if h.failure == "copy":
                self.returncode = 1
                self.completed = True
                h.events.append(("complete", role))
                return
        else:
            assert input is None
        if role == "release" and h.failure == "release":
            self.returncode = 1
            self.completed = True
            h.events.append(("complete", role))
            return
        if role == "chord" and h.callback:
            h.callback()
        if role == "chord" and h.failure == "chord":
            self.returncode = 1
            self.completed = True
            h.events.append(("complete", role))
            return
        if role == "recovery" and h.failure == "key_release":
            self.returncode = 1
        cost = h.costs.get(role, 0.01)
        if cost > timeout:
            h.clock.advance(timeout)
            self.timed_out = True
            h.events.append(("timeout", role))
            raise boundary.subprocess.TimeoutExpired(role, timeout)
        h.clock.advance(cost)
        if role == "chord" and h.failure in {"timeout", "key_release"}:
            # Consume the allotted chord budget and raise the real timeout type.
            h.clock.advance(max(0, timeout - cost))
            self.timed_out = True
            h.events.append(("timeout", role))
            raise boundary.subprocess.TimeoutExpired(role, timeout)
        self.completed = True
        h.events.append(("complete", role))

    def kill(self):
        self.killed = True
        self.harness.events.append(("kill", self.role))

    def wait(self, timeout=None):
        cost = self.harness.costs.get(self.role + "_reap", 0)
        assert timeout is not None and timeout >= 0
        self.harness.clock.advance(min(cost, timeout))
        if cost > timeout:
            raise boundary.subprocess.TimeoutExpired(self.role, timeout)
        self.reaped = True
        self.harness.events.append(("reap", self.role))
        return self.returncode


class Stt:
    def __init__(self, text):
        self.text, self.calls = text, 0
    def transcribe_dictation(self, audio):
        self.calls += 1
        return self.text


class Chunker:
    def __init__(self):
        self.chunks = []
    def feed(self, _frame):
        result, self.chunks = self.chunks, []
        return result
    def flush(self):
        return None


class Injector:
    sanitize = staticmethod(boundary.sanitize)
    join = staticmethod(boundary.join)
    slices = staticmethod(boundary.slices)

    def __init__(self, pin, harness):
        self.pin, self.harness = pin, harness
        self.calls, self.sent, self.watch_starts = [], [], 0
        self.expires_at = None
        self.focused = True
        self.on_paste = None

    def check(self, pin, layers):
        assert pin == self.pin and layers == ("test-layer",)
        self.calls.append("check")
        if not self.focused:
            raise boundary.InputRefused("focus_changed")

    def send(self, text, delay, timeout):
        self.calls.append(("send", text))
        self.sent.append(text)
        return True

    def paste(self, text, delay, timeout):
        self.calls.append(("paste", text))
        return boundary.paste(text, delay, timeout)

    def arm_lock_timer(self, seconds):
        assert seconds == 10.0
        self.expires_at = core.monotonic() + seconds
        return True

    def lock_held(self):
        return self.expires_at is not None and core.monotonic() < self.expires_at

    def lock_enter(self):
        self.calls.append("lock_enter")
        return True
    def lock_leave(self): return True
    def disarm_lock_timer(self): return True

    def FocusWatch(self, pin, on_lost, ignore_layers):
        assert pin == self.pin and ignore_layers == ("test-layer",)
        outer = self
        class Watch:
            def start(self): outer.watch_starts += 1
            def stop(self): pass
            def lose(self, reason="focus_changed"): on_lost(reason)
        self.watch = Watch()
        return self.watch


async def until(predicate, timeout=0.75):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.002)


def chunk():
    return Chunk(0, np.ones(6400, np.int16), "silence", 0.4, 0.4)


def states(d):
    return [piece.state for entry in d._session.ledger for piece in entry.slices]


@asynccontextmanager
async def session(monkeypatch, *, text=TEXT, cls="kitty", paste_classes=("kitty",),
                  slice_chars=4, harness=None):
    harness = harness or ProcessHarness(monkeypatch)
    pin = boundary.Pin(PIN.address, cls, PIN.pid)
    injector = Injector(pin, harness)
    settings = SimpleNamespace(dictation_slice_chars=slice_chars, dictation_type_delay_ms=20,
                               dictation_post_shortcut_ms=0, dictation_max_session_s=1800,
                               dictation_ignore_layers=("test-layer",),
                               dictation_paste_classes=paste_classes)
    gpu, io = ThreadPoolExecutor(2), ThreadPoolExecutor(1)
    monkeypatch._paste_guards.executors.extend((gpu, io))
    stt, events = Stt(text), []
    d = core.DesktopDictation(stt, gpu, lambda samples: float(bool(np.any(samples))), injector, io,
                              settings=settings, event=lambda **event: events.append(event))
    c = Chunker()
    monkeypatch.setattr(core, "Chunker", lambda *a, **kw: c)
    try:
        sid = d.start(pin)["session_id"]
        await until(lambda: d._session.ready or d.phase == "paused")
        yield d, stt, injector, c, events, sid, harness
    finally:
        d.shutdown()
        try:
            await until(lambda: d._worker is None or d._worker.done(), timeout=0.5)
        finally:
            join_executors((gpu, io))


def enqueue(d, c):
    c.chunks = [chunk()]
    assert d.feed(np.ones(1280, np.int16))


async def settle(d):
    await until(lambda: d.phase in ("paused", *core.TERMINAL))
    await until(lambda: d._worker is None or d._worker.done())


async def resume_and_stop(d, sid, injector):
    before = injector.watch_starts
    d.resume(sid)
    await until(lambda: injector.watch_starts > before and d.phase == "listening")
    d.toggle_stop()
    await until(lambda: d.phase == "stopped")


async def test_paste_off_by_default(monkeypatch):
    async with session(monkeypatch, paste_classes=()) as (d, _, inj, c, _, _, harness):
        enqueue(d, c)
        d.toggle_stop()
        await until(lambda: d.phase == "stopped")
        assert not any(e[1] == "copy" for e in harness.events if e[0] == "start")
        assert len(inj.sent) == len(boundary.slices(TEXT, 4))
        assert all(call[0] != "paste" for call in inj.calls if isinstance(call, tuple))
        assert harness.commands == []


async def test_paste_only_for_listed_class_exact_match(monkeypatch):
    for cls, configured, should_paste in (("kitty", ("kitty",), True),
                                           ("Kitty", ("kitty",), False),
                                           ("kitty-extra", ("kitty",), False)):
        async with session(monkeypatch, cls=cls, paste_classes=configured) as (d, _, inj, c, *_):
            enqueue(d, c)
            d.toggle_stop()
            await until(lambda: d.phase == "stopped")
            assert any(call[0] == "paste" for call in inj.calls if isinstance(call, tuple)) is should_paste


async def test_paste_text_on_stdin_not_argv(monkeypatch):
    harness = ProcessHarness(monkeypatch)
    async with session(monkeypatch, harness=harness) as (d, _, inj, c, *_):
        enqueue(d, c)
        d.toggle_stop()
        await until(lambda: d.phase == "stopped")
        assert TEXT not in repr([argv for argv in harness.events if argv[0] == "start"])
        assert TEXT not in repr(harness.commands)
        copy = next(event for event in harness.events if event[:2] == ("communicate", "copy"))
        assert copy[2] == TEXT.encode()


async def test_paste_uses_sensitive_flag(monkeypatch):
    harness = ProcessHarness(monkeypatch)
    async with session(monkeypatch, harness=harness) as (d, _, _, c, *_):
        enqueue(d, c)
        d.toggle_stop()
        await until(lambda: d.phase == "stopped")
        assert ["/usr/bin/wl-copy", "--sensitive"] in harness.commands


@pytest.mark.parametrize("failure", ["copy", "chord", "timeout"])
async def test_paste_failure_is_uncertain_never_retried(monkeypatch, failure):
    harness = ProcessHarness(monkeypatch, failure=failure)
    async with session(monkeypatch, harness=harness) as (d, _, inj, c, events, sid, _):
        enqueue(d, c)
        await until(lambda: d.phase == "paused")
        await until(lambda: d._worker is None or d._worker.done())
        assert states(d) == ["uncertain"]
        assert sum(call[0] == "paste" for call in inj.calls if isinstance(call, tuple)) == 1
        assert TEXT not in repr(events)
        before = inj.watch_starts
        d.resume(sid)
        await until(lambda: inj.watch_starts > before and d.phase == "listening")
        d.toggle_stop()
        await until(lambda: d.phase == "stopped")
        assert sum(call[0] == "paste" for call in inj.calls if isinstance(call, tuple)) == 1


async def test_one_paste_per_chunk_not_per_slice(monkeypatch):
    async with session(monkeypatch, slice_chars=4) as (d, _, inj, c, *_):
        enqueue(d, c)
        await until(lambda: any(isinstance(call, tuple) and call[0] in ("paste", "send")
                                for call in inj.calls))
        attempts = [call for call in inj.calls if isinstance(call, tuple) and call[0] == "paste"]
        assert attempts == [("paste", TEXT)]
        d.toggle_stop()
        await until(lambda: d.phase == "stopped")


async def test_check_runs_before_and_after_paste(monkeypatch):
    harness = ProcessHarness(monkeypatch)
    async with session(monkeypatch, harness=harness) as (d, _, inj, c, *_):
        enqueue(d, c)
        d.toggle_stop()
        await until(lambda: d.phase == "stopped")
        events = inj.calls
        paste_at = events.index(("paste", TEXT))
        assert events[paste_at - 1] == "check"
        assert events[paste_at - 2] == "lock_enter"
        assert events[paste_at - 3] == "check"
        assert events[paste_at + 1] == "check"


def test_chord_held_modifier_released(monkeypatch):
    """The true TimeoutExpired path kills/reaps the chord and finishes release."""
    # Kept as a direct fake-process boundary test; session-level timeout and
    # uncertainty/no-retry behavior is covered above.
    harness = ProcessHarness(monkeypatch, failure="timeout")
    assert not boundary.paste(TEXT, 20, timeout_s=5.0)
    chord = next(p for p in harness.processes if p.role == "chord")
    release = next(p for p in harness.processes if p.role == "release")
    assert chord.timed_out and chord.killed and chord.reaped
    assert release.completed


@pytest.mark.parametrize("failure, expected", [(None, True), ("chord", False)])
def test_completed_chord_also_releases_ctrl(monkeypatch, failure, expected):
    harness = ProcessHarness(monkeypatch, failure=failure)
    assert boundary.paste(TEXT, 20) is expected
    assert next(p for p in harness.processes if p.role == "chord").completed
    assert next(p for p in harness.processes if p.role == "release").completed


async def test_check_and_watch_restore_during_paste_pause(monkeypatch):
    harness = ProcessHarness(monkeypatch)
    async with session(monkeypatch, harness=harness) as (d, _, inj, c, _, sid, _):
        def pause():
            d.focus_lost("focus_changed")
        harness.callback = pause
        enqueue(d, c)
        await until(lambda: d.phase == "paused")
        await until(lambda: d._worker is None or d._worker.done())
        assert states(d) == ["uncertain"]
        assert sum(call[0] == "paste" for call in inj.calls if isinstance(call, tuple)) == 1
        harness.callback = None
        await resume_and_stop(d, sid, inj)
        assert sum(call[0] == "paste" for call in inj.calls if isinstance(call, tuple)) == 1


@pytest.mark.parametrize("reap_cost", [0, 0.2])
def test_slow_release_and_reap_preserve_release_allowance(monkeypatch, reap_cost):
    clock = Clock()
    monkeypatch.setattr(boundary, "monotonic", clock)
    harness = ProcessHarness(monkeypatch, failure="timeout", clock=clock,
                             costs={"release": 0.8, "chord_reap": reap_cost})
    assert boundary.paste(TEXT, 20, timeout_s=5.0) is False
    chord = next(p for p in harness.processes if p.role == "chord")
    release = next(p for p in harness.processes if p.role == "release")
    assert chord.killed and chord.reaped
    assert release.completed and not release.killed
    release_budget = next(e[3] for e in harness.events if e[:2] == ("communicate", "release"))
    assert release_budget == pytest.approx(1.0)
    assert harness.events.index(("reap", "chord")) < harness.events.index(("start", "release"))
    assert clock() - harness.started_at <= 5.0


async def test_delayed_running_work_keeps_guards_until_joined(monkeypatch, desktop_guards):
    entered, unblock = threading.Event(), threading.Event()
    harness = ProcessHarness(monkeypatch)
    try:
        with pytest.raises(AssertionError, match="workers still running"):
            async with session(monkeypatch, harness=harness) as (d, _, inj, c, *_):
                paste = inj.paste
                def delayed(*args):
                    entered.set()
                    assert unblock.wait(3)
                    return paste(*args)
                inj.paste = delayed
                enqueue(d, c)
                await until(entered.is_set)
        with pytest.raises(AssertionError, match="guards retained"):
            desktop_guards.restore()
        assert boundary.subprocess.Popen == harness.popen
        assert boundary.subprocess.run == harness.forbidden
        assert boundary.hypr.hyprctl == harness.forbidden
    finally:
        unblock.set()
        join_executors(desktop_guards.executors)
        await until(lambda: d._worker is None or d._worker.done())
    assert any(p.role == "copy" for p in harness.processes)
    assert not any(t.is_alive() for executor in desktop_guards.executors for t in executor._threads)


async def test_paste_shared_deadline_stays_inside_reserved_lock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(boundary, "monotonic", clock)
    monkeypatch.setattr(core, "monotonic", clock)
    harness = ProcessHarness(monkeypatch, failure="timeout", clock=clock,
                             costs={"copy": 0.1, "chord": 8.0, "release": 0.05})
    async with session(monkeypatch, harness=harness) as (d, _, inj, c, *_):
        enqueue(d, c)
        await until(lambda: d.phase == "paused")
        chord_budget = next(e[3] for e in harness.events if e[:2] == ("communicate", "chord"))
        release_budget = next(e[3] for e in harness.events if e[:2] == ("communicate", "release"))
        assert chord_budget < 5.0 and release_budget > 0
        assert clock() - harness.started_at <= 5.0
        assert clock() <= inj.expires_at
        assert states(d) == ["uncertain"]


@pytest.mark.parametrize("failure, costs", [
    (None, {"release": 2, "release_reap": 2}),
    ("release", {}),
    ("release_start", {}),
    (None, {"chord": 8, "chord_reap": 8, "release": 0.8}),
])
async def test_incomplete_ctrl_cleanup_pauses_warns_and_never_retries(monkeypatch, failure, costs):
    clock = Clock()
    monkeypatch.setattr(boundary, "monotonic", clock)
    monkeypatch.setattr(core, "monotonic", clock)
    harness = ProcessHarness(monkeypatch, failure=failure, costs=costs, clock=clock)
    async with session(monkeypatch, harness=harness) as (d, _, inj, c, events, sid, _):
        notices = record_notices(d, events)
        enqueue(d, c)
        await settle(d)
        assert d.phase == "paused" and d.metadata()["reason"] == "ctrl_release_failed"
        assert states(d) == ["uncertain"]
        assert clock() - harness.started_at <= 5.0
        assert clock() < inj.expires_at
        if costs.get("chord_reap", 0) > 0.25:
            release = next(p for p in harness.processes if p.role == "release")
            budget = next(e[3] for e in harness.events if e[:2] == ("communicate", "release"))
            assert budget == pytest.approx(1.0)
            assert release.completed and not release.killed
        assert any("Ctrl may be held" in text and sticky for text, sticky in notices)
        assert TEXT not in repr(events) and TEXT not in repr(notices)
        d.suspend("ptt")
        assert d.metadata()["reason"] == "ctrl_release_failed"
        await resume_and_stop(d, sid, inj)
        assert sum(call[0] == "paste" for call in inj.calls if isinstance(call, tuple)) == 1


def record_notices(d, events, *, leave=lambda: None):
    """Exercise the real notice formatter, with synchronous fake cues and trace."""
    from heardback.voice.loop import Voice

    notices = []
    voice = Voice.__new__(Voice)
    voice.desktop, voice._desktop_notified, voice._desktop_discarded = d, None, 0
    voice.cues = SimpleNamespace(play=lambda _: None,
                                 notify=lambda text, sticky=False: notices.append((text, sticky)))
    voice._tr = lambda *args, **kwargs: None
    voice._io_call = lambda fn, *args: fn(*args)
    voice._dictation_leave = leave
    def event(**fields):
        events.append(fields)
        voice._desktop_event_on_loop(fields)
    d.event = event
    return notices


@pytest.mark.parametrize("route", ["paused", "stop", "cancel", "suspend"])
async def test_failed_ctrl_release_warns_once_and_cleans_up(monkeypatch, route):
    harness = ProcessHarness(monkeypatch, failure="release")
    async with session(monkeypatch, harness=harness) as (d, _, inj, c, events, sid, _):
        leave = Mock()
        notices = record_notices(d, events, leave=leave)
        harness.callback = {"paused": None, "stop": d.toggle_stop, "cancel": d.cancel,
                            "suspend": lambda: d.suspend("ptt")}[route]
        enqueue(d, c)
        await settle(d)
        assert next(p for p in harness.processes if p.role == "chord").completed
        assert next(p for p in harness.processes if p.role == "release").returncode == 1
        phase = {"paused": "paused", "stop": "stopped", "cancel": "cancelled",
                 "suspend": "paused"}[route]
        assert d.phase == phase
        assert d.metadata()["reason"] == ("user_cancel" if route == "cancel" else "ctrl_release_failed")
        assert leave.call_count == (1 if phase in core.TERMINAL else 0)
        warnings = [(text, sticky) for text, sticky in notices if "Ctrl may be held" in text]
        assert len(warnings) == 1 and warnings[0][1]
        assert notices[-1] == warnings[0]
        assert sum(call[0] == "paste" for call in inj.calls if isinstance(call, tuple)) == 1
        if route == "cancel":
            assert d.metadata()["held_chars"] == 0 and not d.dashboard()["held"]
            assert all(not p.text for e in d._session.ledger for p in e.slices)
            assert not any(e.get("phase") == "paused" for e in events)
        else:
            assert states(d) == ["uncertain"]
        if phase == "paused":
            d.suspend("ptt")
            assert d.metadata()["reason"] == "ctrl_release_failed"
            await resume_and_stop(d, sid, inj)
        terminal_notice = ("Dictation cancelled; typed text stays" if route == "cancel" else
                           f"Dictation stopped; {len(TEXT)} characters held")
        assert notices.count((terminal_notice, False)) == 1
        leave.assert_called_once_with()
        assert sum("Ctrl may be held" in text for text, _ in notices) == 1
        assert TEXT not in repr(events) and TEXT not in repr(notices)
        with pytest.raises(core.DictationError, match="not_paused"):
            d.resume(sid)
        d.toggle_stop()
        assert d.phase == ("cancelled" if route == "cancel" else "stopped")
        leave.assert_called_once_with()
        assert sum(call[0] == "paste" for call in inj.calls if isinstance(call, tuple)) == 1


@pytest.mark.parametrize("failure", [None, "timeout"])
async def test_key_recovery_after_paste_kill_only(monkeypatch, failure):
    harness = ProcessHarness(monkeypatch, failure=failure)
    async with session(monkeypatch, harness=harness) as (d, _, inj, c, *_):
        enqueue(d, c)
        d.toggle_stop()
        await settle(d)
        recoveries = [e for e in harness.events if e == ("start", "recovery")]
        assert len(recoveries) == (1 if failure else 0)
        if failure:
            assert harness.events.index(("reap", "chord")) < harness.events.index(("start", "recovery"))
            assert states(d) == ["uncertain"]


@pytest.mark.parametrize("route", ["paused", "cancel"])
async def test_key_recovery_failure_warns_even_after_session_ended(monkeypatch, route):
    harness = ProcessHarness(monkeypatch, failure="key_release")
    async with session(monkeypatch, harness=harness) as (d, _, inj, c, events, sid, _):
        notices = record_notices(d, events)
        if route == "cancel":
            harness.callback = d.cancel
        enqueue(d, c)
        await settle(d)
        assert d.metadata()["reason"] == ("user_cancel" if route == "cancel" else "key_release_failed")
        assert any("keys may be held" in text and sticky for text, sticky in notices)
        if route == "paused":
            assert states(d) == ["uncertain"]
            d.suspend("ptt")
            assert d.metadata()["reason"] == "key_release_failed"
            await resume_and_stop(d, sid, inj)
        assert len([e for e in harness.events if e == ("start", "chord")]) == 1
