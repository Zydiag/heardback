"""Session tests use real executor threads, synthetic audio and fake desktop I/O."""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")

from heardback.voice import desktop_dictation as core
from heardback.voice import desktop_input as boundary
from heardback.voice.chunker import Chunk
from heardback.voice.dictation import Dictation, DictationError

PIN = boundary.Pin("0x123", "kitty", 42)
SECRET = "private dictated phrase"


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Stt:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.block = False
        self.active = self.peak = 0

    def transcribe_dictation(self, audio):
        self.calls.append(audio.copy())
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.started.set()
        try:
            if self.block:
                assert self.release.wait(4), "test did not release STT"
            result = self.results[len(self.calls) - 1]
            if isinstance(result, Exception):
                raise result
            return result
        finally:
            self.active -= 1


class Injector:
    sanitize = staticmethod(boundary.sanitize)
    join = staticmethod(boundary.join)
    slices = staticmethod(boundary.slices)

    def __init__(self):
        self.calls = []
        self.sent = []
        self.focused = True
        self.on = {}
        self._locked = False
        self.expires_at = None
        self.expiries = 0
        self.send_locked = []
        self.watch_stops = 0
        self.watch = None
        self.start_loss = None
        self.loop_thread = threading.get_ident()

    def _expire(self):
        if self.expires_at is not None and core.monotonic() >= self.expires_at:
            self._locked = False
            self.expires_at = None
            self.expiries += 1

    @property
    def locked(self):
        self._expire()
        return self._locked

    def invoke(self, name, *args):
        assert threading.get_ident() != self.loop_thread, name
        self._expire()
        self.calls.append((name, args))
        callback = self.on.get(name)
        try:
            return callback(*args) if callback else True
        finally:
            self._expire()

    def check(self, pin, ignore_layers):
        self.invoke("check", pin, ignore_layers)
        assert pin == PIN
        assert ignore_layers == ("custom-notification",)
        if not self.focused:
            raise boundary.InputRefused("focus_changed")

    def send(self, text, delay_ms, timeout_s=5.0):
        self.send_locked.append(self.locked)
        assert self.locked
        self.sent.append(text)
        return self.invoke("send", text, delay_ms, timeout_s)

    def lock_enter(self):
        self._expire()
        self._locked = True
        return self.invoke("lock_enter")

    def lock_held(self):
        return bool(self.invoke("lock_held")) and self.locked

    def lock_leave(self):
        result = self.invoke("lock_leave")
        if result:
            self._locked = False
        return result

    def arm_lock_timer(self, seconds):
        assert seconds == 10.0
        self.expires_at = core.monotonic() + seconds
        return self.invoke("arm_lock_timer", seconds)

    def disarm_lock_timer(self):
        assert not self.locked
        disarmed = self.invoke("disarm_lock_timer")
        if disarmed:
            self.expires_at = None
        return disarmed

    def FocusWatch(self, pin, on_lost, ignore_layers):
        outer = self

        class Watch:
            def start(self):
                outer.invoke("watch_start")
                if outer.start_loss:
                    on_lost(outer.start_loss)

            def stop(self):
                outer.invoke("watch_stop")
                outer.watch_stops += 1

            def lose(self, reason="focus_changed"):
                on_lost(reason)

        self.watch = Watch()
        return self.watch


async def until(predicate, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.002)


def chunk(seq=0, seconds=0.4):
    return Chunk(seq, np.ones(int(seconds * 16000), np.int16), "silence", seconds, seconds)


class Chunker:
    def __init__(self):
        self.chunks = []
        self.rest = None
        self.flushes = 0

    def feed(self, _frame):
        result, self.chunks = self.chunks, []
        return result

    def flush(self):
        self.flushes += 1
        result, self.rest = self.rest, None
        return result


@asynccontextmanager
async def session(*results, monkeypatch=None, **overrides):
    settings = SimpleNamespace(**(dict(
        dictation_slice_chars=8, dictation_type_delay_ms=20,
        dictation_post_shortcut_ms=0, dictation_max_session_s=1800,
        dictation_ignore_layers=("custom-notification",)) | overrides))
    gpu, io = ThreadPoolExecutor(2), ThreadPoolExecutor(1)
    stt, injector, events = Stt(results), Injector(), []
    d = core.DesktopDictation(stt, gpu, lambda samples: float(bool(np.any(samples))), injector, io,
                             settings=settings, event=lambda **e: events.append(e))
    c = Chunker()
    if monkeypatch:
        monkeypatch.setattr(core, "Chunker", lambda *a, **kw: c)
    try:
        sid = d.start(PIN)["session_id"]
        await until(lambda: d._session.ready or d.phase == "paused")
        yield d, stt, injector, c, events, sid
    finally:
        d.shutdown()
        stt.release.set()
        await until(lambda: d._worker is None or d._worker.done())
        await asyncio.to_thread(gpu.shutdown, wait=True)
        await asyncio.to_thread(io.shutdown, wait=True)


def enqueue(d, c, *chunks):
    c.chunks = list(chunks)
    assert d.feed(np.ones(1280, np.int16))


def states(d):
    return [piece.state for entry in d._session.ledger for piece in entry.slices]


async def test_chunks_typed_in_order_with_slow_stt(monkeypatch):
    async with session("first", "second", monkeypatch=monkeypatch) as (d, stt, inj, c, *_):
        stt.block = True
        enqueue(d, c, chunk(), chunk(1))
        await until(stt.started.is_set)
        assert not inj.sent and len(stt.calls) == 1
        stt.release.set()
        d.toggle_stop()
        await until(lambda: d.phase == "stopped")
        assert "".join(inj.sent) == "first second"
        assert stt.peak == 1


async def test_one_inference_in_flight(monkeypatch):
    async with session("a", "b", "c", monkeypatch=monkeypatch) as (d, stt, inj, c, *_):
        stt.block = True
        enqueue(d, c, chunk(), chunk(1), chunk(2))
        await until(stt.started.is_set)
        await asyncio.sleep(0.02)
        assert len(stt.calls) == 1
        stt.release.set()
        d.toggle_stop()
        await until(lambda: d.phase == "stopped")
        assert stt.peak == 1 and "".join(inj.sent) == "a b c"


@pytest.mark.parametrize("silent_frames", [0, 100])
async def test_empty_session_stops_clean(silent_frames):
    async with session() as (d, stt, inj, *_):
        for _ in range(silent_frames):
            assert d.feed(np.zeros(1280, np.int16))
        d.toggle_stop()
        await until(lambda: d.phase == "stopped")
        assert stt.calls == inj.sent == []
        assert d.dashboard()["delivered"] == "" and not d.engaged


async def test_only_final_ledger_entries_reach_injector(monkeypatch):
    async with session(SECRET, monkeypatch=monkeypatch) as (d, stt, inj, c, *_):
        stt.block = True
        enqueue(d, c, chunk())
        await until(stt.started.is_set)
        assert not d._session.ledger[0].final and not inj.sent
        def sending(*_):
            assert d._session.ledger[0].final
            assert "claimed" in states(d)
            return True
        inj.on["send"] = sending
        stt.release.set()
        d.toggle_stop()
        await until(lambda: d.phase == "stopped")
        assert "".join(inj.sent) == SECRET
        assert all(not p.text for e in d._session.ledger for p in e.slices if p.state == "accepted")


@pytest.mark.parametrize("failure", ["before_claim", "after_claim", "send_false", "timeout", "post_check"])
async def test_no_slice_sent_twice(monkeypatch, failure):
    async with session("one two three four", monkeypatch=monkeypatch) as (d, stt, inj, c, events, sid):
        if failure == "before_claim":
            inj.on["lock_enter"] = lambda: False
        elif failure == "after_claim":
            original = d._claim
            def claim(*args):
                result = original(*args)
                d.focus_lost("focus_changed")
                return result
            monkeypatch.setattr(d, "_claim", claim)
        elif failure == "send_false":
            inj.on["send"] = lambda *_: False
        elif failure == "timeout":
            def timeout(*_):
                raise TimeoutError(SECRET)
            inj.on["send"] = timeout
        else:
            def send(*_):
                inj.focused = False
                return True
            inj.on["send"] = send
        enqueue(d, c, chunk())
        await until(lambda: d.phase == "paused")
        await until(lambda: not inj.locked)
        assert states(d)[0] == ("pending" if failure == "before_claim" else "uncertain")
        assert len(inj.sent) <= 1
        assert SECRET not in repr(events)
        inj.on.clear()
        inj.focused = True
        if failure == "after_claim":
            monkeypatch.setattr(d, "_claim", original)
        d.resume(sid)
        await until(lambda: d.phase == "listening")
        d.toggle_stop()
        await until(lambda: d.phase == "stopped")
        assert inj.sent.count("one two ") <= 1


async def test_uncertain_pauses_and_holds_rest_of_chunk(monkeypatch):
    async with session("one two three four", monkeypatch=monkeypatch) as (d, _, inj, c, *_):
        inj.on["send"] = lambda *_: False
        enqueue(d, c, chunk())
        await until(lambda: d.phase == "paused")
        assert d.metadata()["reason"] == "insert_uncertain"
        assert states(d) == ["uncertain", "pending", "pending"]
        assert d.metadata()["held_chars"] == len("one two three four")


async def test_stop_while_paused_uncertain_holds_everything(monkeypatch):
    async with session("one two three four", monkeypatch=monkeypatch) as (d, _, inj, c, *_):
        inj.on["send"] = lambda *_: False
        enqueue(d, c, chunk())
        await until(lambda: d.phase == "paused")
        d.toggle_stop()
        await until(lambda: d.phase == "stopped")
        assert len(inj.sent) == 1 and d.metadata()["held_chars"] == 18


@pytest.mark.parametrize("focused", [True, False])
async def test_stop_while_paused_delivers_if_pin_focused(monkeypatch, focused):
    async with session("held", monkeypatch=monkeypatch) as (d, _, inj, c, *_):
        d.suspend("mic_off")
        c.rest = chunk()
        # The pause closed the old chunk; put a finalized chunk through the queued path.
        d._enqueue(d._session, chunk())
        inj.focused = focused
        d.toggle_stop()
        await until(lambda: d.phase == "stopped")
        assert inj.sent == (["held"] if focused else [])


async def test_stop_while_paused_keeps_held_if_not_focused(monkeypatch):
    await test_stop_while_paused_delivers_if_pin_focused(monkeypatch, False)


async def test_resume_requires_pin_and_skips_uncertain(monkeypatch):
    async with session("one two three four", monkeypatch=monkeypatch) as (d, _, inj, c, events, sid):
        inj.on["send"] = lambda *_: False
        enqueue(d, c, chunk())
        await until(lambda: d.phase == "paused" and not inj.locked)
        inj.on.clear()
        inj.focused = False
        prior_events, prior_stops = len(events), inj.watch_stops
        d.resume(sid)
        await until(lambda: any(e.get("phase") == "paused" and e.get("reason") == "focus_changed"
                                for e in events[prior_events:]))
        await until(lambda: inj.watch_stops > prior_stops and d._worker is None)
        assert d.phase == "paused" and len(inj.sent) == 1
        inj.focused = True
        prior_starts = sum(name == "watch_start" for name, _ in inj.calls)
        d.resume(sid)
        await until(lambda: sum(name == "watch_start" for name, _ in inj.calls) > prior_starts
                    and d._session.ready and states(d) == ["uncertain", "accepted", "accepted"])
        assert len(inj.sent) == 3
        assert states(d) == ["uncertain", "accepted", "accepted"]
        with pytest.raises(DictationError, match="stale_session"):
            d.resume("wrong")


async def test_returning_to_pin_does_not_resume(monkeypatch):
    async with session("held", monkeypatch=monkeypatch) as (d, _, inj, c, *_):
        c.rest = chunk()
        inj.watch.lose()
        inj.focused = True
        await until(lambda: len(d._session.ledger) == 1 and d._session.ledger[0].final)
        assert d.phase == "paused" and not inj.sent


async def test_focus_lost_closes_open_chunk_and_holds_it(monkeypatch):
    async with session("held", monkeypatch=monkeypatch) as (d, _, inj, c, *_):
        c.rest = chunk()
        await asyncio.to_thread(inj.watch.lose)
        await until(lambda: d._session.ledger[0].final)
        assert d.phase == "paused" and c.flushes == 1 and not inj.sent


async def test_focus_lost_during_stopping_holds(monkeypatch):
    async with session("held", monkeypatch=monkeypatch) as (d, stt, inj, c, *_):
        stt.block = True
        c.rest = chunk()
        d.toggle_stop()
        await until(stt.started.is_set)
        inj.watch.lose()
        stt.release.set()
        await until(lambda: d.phase == "stopped")
        assert not inj.sent and d.metadata()["held_chars"] == 4


async def test_cancel_during_slice_settles_then_sends_nothing(monkeypatch):
    async with session("one two three four", monkeypatch=monkeypatch) as (d, _, inj, c, *_):
        entered, release = threading.Event(), threading.Event()
        def sending(*_):
            entered.set()
            assert release.wait(2)
            return True
        inj.on["send"] = sending
        enqueue(d, c, chunk())
        try:
            await until(entered.is_set)
            d.cancel()
            assert d.engaged and inj.locked
        finally:
            release.set()
        await until(lambda: d.phase == "cancelled" and not inj.locked)
        assert len(inj.sent) == 1 and d.metadata()["held_chars"] == 0


async def test_second_chunk_prefixed_with_space_first_not(monkeypatch):
    async with session("first", "second", ", third", monkeypatch=monkeypatch) as (d, _, inj, c, *_):
        enqueue(d, c, chunk(), chunk(1), chunk(2))
        d.toggle_stop()
        await until(lambda: d.phase == "stopped")
        assert "".join(inj.sent) == "first second, third"


async def test_new_start_discards_held_and_reports_count(monkeypatch):
    async with session("held", monkeypatch=monkeypatch) as (d, _, inj, c, *_):
        inj.focused = False
        enqueue(d, c, chunk())
        await until(lambda: d.phase == "paused")
        d.toggle_stop()
        await until(lambda: d.phase == "stopped" and d._worker is None)
        inj.focused = True
        result = d.start(PIN)
        assert result["discarded_chars"] == 4
        assert d.dashboard()["held"] == ""


@pytest.mark.parametrize("boundary_kind", ["chunks", "audio"])
async def test_backlog_pauses(monkeypatch, boundary_kind):
    at_limit = [chunk(i) for i in range(3)] if boundary_kind == "chunks" else [chunk(i, 15) for i in range(2)]
    next_chunk = chunk(len(at_limit), 0.032)
    async with session(*(["held"] * (len(at_limit) + 1)), monkeypatch=monkeypatch) as (d, stt, inj, c, *_):
        stt.block = True
        enqueue(d, c, *at_limit)
        assert d.phase == "listening"
        enqueue(d, c, next_chunk)
        assert d.phase == "paused" and d.metadata()["reason"] == "backlog"
        assert not inj.sent


@pytest.mark.parametrize("paused", [False, True])
async def test_wall_cap_acts_as_stop(monkeypatch, paused):
    # Narrow plan 11's name: only a listening session drains at the wall cap.
    async with session("last", monkeypatch=monkeypatch, dictation_max_session_s=0.15) as (d, _, inj, c, *_):
        c.rest = chunk()
        if paused:
            prior_stops = inj.watch_stops
            d.suspend("mic_off")
            await until(lambda: inj.watch_stops > prior_stops and d._worker is None)
            assert d.phase == "paused"
        # No more feed calls: only the timer installed by start() can stop this session.
        await until(lambda: d.phase == "stopped")
        assert "".join(inj.sent) == ("" if paused else "last")
        assert d.metadata()["held_chars"] == (4 if paused else 0)


async def test_inference_timeout_marks_lost_and_pauses(monkeypatch):
    monkeypatch.setattr(core, "STT_TIMEOUT_S", 0.02)
    async with session("late", monkeypatch=monkeypatch) as (d, stt, inj, c, *_):
        stt.block = True
        enqueue(d, c, chunk(), chunk(1))
        await until(lambda: d.phase == "paused")
        assert d.metadata()["reason"] == "inference_timeout"
        assert d._session.ledger[0].lost and not inj.sent
        assert len(stt.calls) == 1


async def test_late_result_discarded(monkeypatch):
    monkeypatch.setattr(core, "STT_TIMEOUT_S", 0.02)
    async with session(SECRET, monkeypatch=monkeypatch) as (d, stt, inj, c, *_):
        stt.block = True
        enqueue(d, c, chunk())
        await until(lambda: d.phase == "paused")
        stt.release.set()
        await until(lambda: d._inference is None)
        assert not inj.sent and SECRET not in repr(d.dashboard())


async def test_stuck_job_refuses_new_start(monkeypatch):
    monkeypatch.setattr(core, "STT_TIMEOUT_S", 0.02)
    async with session("late", monkeypatch=monkeypatch) as (d, stt, inj, c, *_):
        stt.block = True
        enqueue(d, c, chunk())
        await until(lambda: d.phase == "paused")
        d.cancel()
        await until(lambda: d.phase == "cancelled" and d._worker is None)
        with pytest.raises(DictationError, match="inference_busy"):
            d.start(PIN)


async def test_unsafe_text_refused_and_held(monkeypatch):
    async with session("unsafe\u202etext", monkeypatch=monkeypatch) as (d, _, inj, c, *_):
        enqueue(d, c, chunk())
        await until(lambda: d.phase == "paused")
        assert not inj.sent and d.metadata()["reason"] == "unsafe_text"
        assert d.dashboard()["held"] == "unsafe\u202etext"


async def test_post_shortcut_delay_holds_slice(monkeypatch):
    clock = Clock()
    async with session("held", monkeypatch=monkeypatch) as (d, _, inj, c, *_):
        monkeypatch.setattr(core, "monotonic", clock)
        d.settings.dictation_post_shortcut_ms = 491
        attempts, attempted = [], threading.Event()
        original_claim = d._claim
        def claim(*args):
            result = original_claim(*args)
            attempts.append(result)
            attempted.set()
            return result
        monkeypatch.setattr(d, "_claim", claim)
        def renew(*_):
            if inj.locked:
                inj.on.pop("arm_lock_timer")
                d.shortcut_pressed()
            return True
        inj.on["arm_lock_timer"] = renew
        d.shortcut_pressed()
        enqueue(d, c, chunk())
        await until(lambda: d._session.ledger[0].final)
        assert not inj.sent and not attempts
        clock.now += 0.5
        await until(attempted.is_set)
        assert attempts == [False] and states(d) == ["pending"] and not inj.sent
        clock.now += 0.5
        await until(lambda: states(d) == ["accepted"])
        assert inj.sent == ["held"]


async def test_lock_entered_before_first_slice_and_left_after_last(monkeypatch):
    async with session("one two three", monkeypatch=monkeypatch) as (d, _, inj, c, *_):
        enqueue(d, c, chunk())
        d.toggle_stop()
        await until(lambda: d.phase == "stopped" and not inj.locked)
        names = [name for name, _ in inj.calls]
        assert names.index("lock_enter") < names.index("send")
        assert names.index("lock_leave") > max(i for i, n in enumerate(names) if n == "send")
        assert names[-1] == "watch_stop"


async def test_lock_enter_failure_pauses_and_types_nothing(monkeypatch):
    async with session("held", monkeypatch=monkeypatch) as (d, _, inj, c, *_):
        inj.on["lock_enter"] = lambda: False
        enqueue(d, c, chunk())
        await until(lambda: d.phase == "paused" and not inj.locked)
        assert not inj.sent and states(d) == ["pending"]


@pytest.mark.parametrize("action", ["pause", "cancel", "stop", "send_failure"])
async def test_lock_left_on_pause_cancel_stop_and_send_failure(monkeypatch, action):
    async with session("one two three", monkeypatch=monkeypatch) as (d, _, inj, c, *_):
        def sending(*_):
            if action == "pause":
                d.suspend("mic_off")
            elif action == "cancel":
                d.cancel()
            elif action == "stop":
                d.toggle_stop()
            return action != "send_failure"
        inj.on["send"] = sending
        enqueue(d, c, chunk())
        await until(lambda: any(n == "lock_leave" for n, _ in inj.calls))
        assert not inj.locked
        if action != "stop":
            assert len(inj.sent) == 1


async def test_lock_timer_renewed_per_slice(monkeypatch):
    clock = Clock()
    async with session("one two three four", monkeypatch=monkeypatch) as (d, _, inj, c, *_):
        monkeypatch.setattr(core, "monotonic", clock)
        def advance(seconds):
            def run(*_):
                clock.now += seconds
                return True
            return run
        inj.on.update(check=advance(1), lock_held=advance(1), send=advance(5))
        enqueue(d, c, chunk())
        d.toggle_stop()
        await until(lambda: d.phase == "stopped")
        names = [n for n, _ in inj.calls]
        assert "".join(inj.sent) == "one two three four" and not inj.expiries
        assert inj.send_locked and all(inj.send_locked)
        for i, name in enumerate(names):
            if name == "send":
                previous = max((j for j in range(i) if names[j] == "send"), default=-1)
                assert "arm_lock_timer" in names[previous + 1:i]
                assert names[i - 3:i] == ["check", "arm_lock_timer", "lock_held"]


async def test_layer_open_pauses_with_text_held(monkeypatch):
    async with session("held", monkeypatch=monkeypatch) as (d, _, inj, c, *_):
        c.rest = chunk()
        inj.watch.lose("layer_opened")
        await until(lambda: d._session.ledger[0].final)
        assert d.phase == "paused" and not inj.sent


async def test_trace_and_metadata_contain_no_text(monkeypatch, caplog):
    async with session(SECRET, monkeypatch=monkeypatch) as (d, _, inj, c, events, *_):
        enqueue(d, c, chunk())
        d.toggle_stop()
        await until(lambda: d.phase == "stopped")
        assert SECRET not in repr(events) + repr(d.metadata()) + caplog.text
        traces = [e for e in events if e.get("event") == "dictation_chunk"]
        assert len(traces) == 1
        assert {"seq", "audio_s", "close_reason", "speech_end_to_stt_ms", "stt_ms", "check_ms",
                "type_ms", "chars", "outcome"} <= traces[0].keys()
        assert len(d.dashboard()["delivered"]) <= 4000


async def test_slot_engaged_for_either_mode(monkeypatch):
    async with session(monkeypatch=monkeypatch) as (desktop, _, _, _, _, _):
        tmux = Dictation(desktop.stt, desktop.gpu, lambda _: 0.0)
        slot = core.DictationSlot(tmux, desktop)
        assert slot.engaged and slot.phase == "listening"
        assert slot.metadata() == desktop.metadata() and slot.dashboard() == desktop.dashboard()
        desktop.cancel()
        await until(lambda: not desktop.engaged and desktop._worker is None)
        slot.start({"target": "1", "pane_id": "%1"})
        assert slot.engaged and slot.metadata() == tmux.metadata()
        slot.cancel(tmux.metadata()["session_id"])
        assert not slot.engaged


async def test_slot_refuses_second_session(monkeypatch):
    async with session(monkeypatch=monkeypatch) as (desktop, _, _, _, _, _):
        tmux = Dictation(desktop.stt, desktop.gpu, lambda _: 0.0)
        slot = core.DictationSlot(tmux, desktop)
        with pytest.raises(DictationError, match="session_active"):
            slot.start({"target": "1"})
        desktop.cancel()
        await until(lambda: not desktop.engaged and desktop._worker is None)
        sid = slot.start({"target": "1"})["session_id"]
        with pytest.raises(DictationError, match="session_active"):
            desktop.start(PIN)
        tmux.cancel(sid)


@pytest.mark.parametrize("action", ["focus", "cancel", "suspend"])
async def test_interrupt_during_lock_enter_types_nothing_and_leaves_lock(monkeypatch, action):
    async with session("held", monkeypatch=monkeypatch) as (d, _, inj, c, *_):
        def enter():
            if action == "focus":
                d.focus_lost("focus_changed")
            elif action == "cancel":
                d.cancel()
            else:
                d.suspend("mic_off")
            return True
        inj.on["lock_enter"] = enter
        enqueue(d, c, chunk())
        await until(lambda: any(n == "lock_leave" for n, _ in inj.calls))
        assert not inj.sent and not inj.locked
        assert all(s != "claimed" for s in states(d))


async def test_focus_lost_during_watch_start_is_latched(monkeypatch):
    original = Injector.__init__
    def init(self):
        original(self)
        self.start_loss = "focus_watch_lost"
    monkeypatch.setattr(Injector, "__init__", init)
    async with session(monkeypatch=monkeypatch) as (d, stt, inj, c, *_):
        assert d.phase == "paused"
        assert d.feed(np.ones(1280, np.int16))
        assert not stt.calls and not inj.sent


@pytest.mark.parametrize("step", ["check", "lock_enter", "arm_lock_timer", "send", "lock_leave", "disarm_lock_timer"])
async def test_injector_exceptions_are_private_and_cleanup_is_attempted(monkeypatch, step):
    async with session(SECRET, monkeypatch=monkeypatch) as (d, _, inj, c, events, *_):
        def fail(*_):
            raise RuntimeError(SECRET)
        inj.on[step] = fail
        enqueue(d, c, chunk())
        d.toggle_stop()
        await until(lambda: d.phase in ("paused", "stopped"))
        await until(lambda: d._worker is None)
        assert SECRET not in repr(events) + repr(d.metadata())
        if step in ("check", "lock_enter", "arm_lock_timer"):
            assert not inj.sent and states(d)[0] == "pending"
        elif step == "send":
            assert states(d)[0] == "uncertain"
        if step in ("lock_enter", "send", "lock_leave", "disarm_lock_timer"):
            assert any(n == "lock_leave" for n, _ in inj.calls)
        if step == "lock_leave":
            assert not any(n == "disarm_lock_timer" for n, _ in inj.calls)


@pytest.mark.parametrize("after_entry", [False, True])
async def test_timer_arm_failure_fails_closed(monkeypatch, after_entry):
    async with session("held", monkeypatch=monkeypatch) as (d, _, inj, c, *_):
        count = 0
        def arm(*_):
            nonlocal count
            count += 1
            return count < 2 if after_entry else False
        inj.on["arm_lock_timer"] = arm
        enqueue(d, c, chunk())
        await until(lambda: d.phase == "paused" and not inj.locked)
        assert not inj.sent and states(d) == ["pending"]
        if after_entry:
            assert any(n == "lock_leave" for n, _ in inj.calls)


@pytest.mark.parametrize("step", ["lock_leave", "disarm_lock_timer"])
async def test_cleanup_false_is_traced_without_looping(monkeypatch, step):
    async with session("held", monkeypatch=monkeypatch) as (d, _, inj, c, events, *_):
        inj.on[step] = lambda: False
        enqueue(d, c, chunk())
        d.toggle_stop()
        await until(lambda: d.phase == "stopped" and d._worker is None)
        assert sum(n == step for n, _ in inj.calls) == 1
        assert any(e.get("reason") == step + "_failed" for e in events)
        if step == "lock_leave":
            assert not any(n == "disarm_lock_timer" for n, _ in inj.calls)


async def test_shutdown_while_slice_in_flight_and_twice(monkeypatch):
    async with session("one two three", monkeypatch=monkeypatch) as (d, _, inj, c, *_):
        entered, release = threading.Event(), threading.Event()
        def send(*_):
            entered.set()
            assert release.wait(2)
            return True
        inj.on["send"] = send
        enqueue(d, c, chunk())
        try:
            await until(entered.is_set)
            d.shutdown()
            d.shutdown()
            assert inj.locked and d.engaged
        finally:
            release.set()
        await until(lambda: d.phase == "cancelled" and d._worker is None)
        assert len(inj.sent) == 1 and not inj.locked
        assert sum(n == "lock_leave" for n, _ in inj.calls) == 1


async def test_toggle_stop_twice_quickly(monkeypatch):
    async with session("last", monkeypatch=monkeypatch) as (d, stt, inj, c, events, *_):
        stt.block = True
        c.rest = chunk()
        d.toggle_stop()
        d.toggle_stop()
        await until(stt.started.is_set)
        stt.release.set()
        await until(lambda: d.phase == "stopped")
        assert len(stt.calls) == len(inj.sent) == c.flushes == 1
        assert any(e.get("reason") == "already_stopping" for e in events)


async def test_shortcut_during_lock_enter_delays_dispatch(monkeypatch):
    clock = Clock()
    async with session("held", monkeypatch=monkeypatch) as (d, _, inj, c, *_):
        monkeypatch.setattr(core, "monotonic", clock)
        d.settings.dictation_post_shortcut_ms = 491
        def enter():
            inj.on.pop("lock_enter")
            d.shortcut_pressed()
            return True
        inj.on["lock_enter"] = enter
        enqueue(d, c, chunk())
        await until(lambda: any(n == "lock_enter" for n, _ in inj.calls))
        await asyncio.sleep(0.02)
        assert not inj.sent
        clock.now += 0.5
        await until(lambda: inj.sent)


async def test_resume_does_not_erase_focus_loss_during_old_watch_cleanup(monkeypatch):
    async with session("held", monkeypatch=monkeypatch) as (d, stt, inj, c, _, sid):
        c.rest = chunk()
        d.suspend("mic_off")
        entered, release = threading.Event(), threading.Event()
        def stop():
            entered.set()
            assert release.wait(2)
        inj.on["watch_stop"] = stop
        try:
            await until(entered.is_set)
            d.resume(sid)
            d.focus_lost("layer_opened")
        finally:
            release.set()
        await until(lambda: d._worker is None)
        assert d.phase == "paused" and not inj.sent


async def test_unsafe_chunk_blocks_stop_from_delivering_later_text(monkeypatch):
    async with session("unsafe\u202etext", "later", monkeypatch=monkeypatch) as (d, stt, inj, c, _, sid):
        enqueue(d, c, chunk(), chunk(1))
        await until(lambda: len(stt.calls) == 2 and d._worker is None)
        with pytest.raises(DictationError, match="unsafe_text"):
            d.resume(sid)
        d.toggle_stop()
        await until(lambda: d.phase == "stopped")
        assert not inj.sent


async def test_resume_delivers_held_before_accepting_new_audio(monkeypatch):
    async with session("held", monkeypatch=monkeypatch) as (d, stt, inj, c, _, sid):
        c.rest = chunk()
        prior_stops = inj.watch_stops
        d.suspend("mic_off")
        await until(lambda: inj.watch_stops > prior_stops and d._session.ledger[0].final and d._worker is None)
        entered, release = threading.Event(), threading.Event()
        def sending(*_):
            entered.set()
            assert release.wait(2)
            return True
        inj.on["send"] = sending
        d.resume(sid)
        try:
            await until(entered.is_set)
            assert not d._session.ready
            assert d.feed(np.ones(1280, np.int16))
        finally:
            release.set()
        await until(lambda: d._session.ready)
        assert len(stt.calls) == 1


async def test_cancelled_tmux_inference_blocks_desktop_start(monkeypatch):
    async with session(monkeypatch=monkeypatch) as (desktop, _, _, _, _, _):
        tmux = Dictation(desktop.stt, desktop.gpu, lambda _: 0.0)
        slot = core.DictationSlot(tmux, desktop)
        desktop.cancel()
        await until(lambda: not desktop.engaged and desktop._worker is None)
        sid = slot.start({"target": "1"})["session_id"]
        tmux.cancel(sid)
        # A cancelled tmux inference retains ownership until its executor callback settles.
        tmux._job = object()
        with pytest.raises(DictationError, match="inference_busy"):
            desktop.start(PIN)
        tmux._job = None


async def test_stop_during_pause_cleanup_still_delivers_held(monkeypatch):
    async with session("held", monkeypatch=monkeypatch) as (d, _, inj, c, *_):
        entered, release = threading.Event(), threading.Event()
        def cleanup():
            entered.set()
            assert release.wait(2)
        inj.on["watch_stop"] = cleanup
        c.rest = chunk()
        d.suspend("mic_off")
        try:
            await until(entered.is_set)
            d.toggle_stop()
        finally:
            release.set()
        await until(lambda: d.phase == "stopped")
        assert inj.sent == ["held"]


async def test_stop_from_pause_reestablishes_watch_and_latches_flicker(monkeypatch):
    async with session("held", monkeypatch=monkeypatch) as (d, _, inj, c, *_):
        c.rest = chunk()
        prior_stops = inj.watch_stops
        d.suspend("mic_off")
        await until(lambda: inj.watch_stops > prior_stops and d._session.ledger[0].final and d._worker is None)
        starts = sum(n == "watch_start" for n, _ in inj.calls)
        inj.start_loss = "focus_changed"
        d.toggle_stop()
        await until(lambda: d.phase == "stopped")
        assert not inj.sent
        assert sum(n == "watch_start" for n, _ in inj.calls) == starts + 1


async def test_stop_after_explicit_resume_does_not_reblock_acknowledged_uncertainty(monkeypatch):
    async with session("one two three", "last", monkeypatch=monkeypatch) as (d, _, inj, c, _, sid):
        inj.on["send"] = lambda *_: False
        enqueue(d, c, chunk())
        await until(lambda: d.phase == "paused" and d._worker is None)
        inj.on.clear()
        d.resume(sid)
        await until(lambda: d._session.ready)
        c.rest = chunk(1)
        d.toggle_stop()
        await until(lambda: d.phase == "stopped")
        assert "last" in "".join(inj.sent)
        assert states(d)[0] == "uncertain"


@pytest.mark.parametrize("step", ["sanitize", "join", "slices"])
async def test_finalization_exception_holds_text_privately(monkeypatch, step):
    async with session(SECRET, monkeypatch=monkeypatch) as (d, _, inj, c, events, *_):
        def fail(*_):
            raise RuntimeError(SECRET)
        monkeypatch.setattr(inj, step, fail)
        enqueue(d, c, chunk())
        await until(lambda: d.phase == "paused" and d._worker is None)
        assert not inj.sent and d.dashboard()["held"] == SECRET
        assert SECRET not in repr(events) + repr(d.metadata())
        assert any(e.get("outcome") == "held" for e in events)


@pytest.mark.parametrize("step", ["watch_start", "watch_stop"])
async def test_watch_exception_is_private_and_fails_closed(monkeypatch, step):
    original = Injector.__init__
    def init(self):
        original(self)
        def fail(*_):
            raise RuntimeError(SECRET)
        self.on[step] = fail
    monkeypatch.setattr(Injector, "__init__", init)
    async with session(monkeypatch=monkeypatch) as (d, _, inj, _, events, *_):
        if step == "watch_stop":
            d.toggle_stop()
            await until(lambda: d.phase == "stopped")
        else:
            assert d.phase == "paused"
        assert not inj.sent and SECRET not in repr(events) + repr(d.metadata())


async def test_inference_error_marks_gap_and_keeps_later_audio_held(monkeypatch):
    async with session(RuntimeError(SECRET), "later", monkeypatch=monkeypatch) as (d, stt, inj, c, events, *_):
        enqueue(d, c, chunk(), chunk(1))
        await until(lambda: d.phase == "paused" and d._worker is None)
        assert d._session.ledger[0].lost and len(stt.calls) == 1
        assert d._session.ledger[1].audio is not None and not inj.sent
        assert SECRET not in repr(events) + repr(d.metadata())
        d.toggle_stop()
        await until(lambda: d.phase == "stopped")
        assert not inj.sent


async def test_explicit_resume_acknowledges_settled_inference_gap(monkeypatch):
    async with session(RuntimeError(SECRET), "later", monkeypatch=monkeypatch) as (d, stt, inj, c, _, sid):
        enqueue(d, c, chunk(), chunk(1))
        await until(lambda: d.phase == "paused" and d._worker is None)
        d.resume(sid)
        await until(lambda: d._session.ready)
        assert len(stt.calls) == 2 and "".join(inj.sent) == "later"
        assert d._session.ledger[0].lost


@pytest.mark.parametrize("stage,elapsed", [("check", 11.0), ("lock_held", 11.0), ("lock_held", 4.01)])
async def test_expired_or_short_lease_never_claims(monkeypatch, stage, elapsed):
    clock = Clock()
    async with session(SECRET, monkeypatch=monkeypatch) as (d, _, inj, c, events, sid):
        monkeypatch.setattr(core, "monotonic", clock)
        def slow(*_):
            if inj.locked:
                clock.now += elapsed
            return True
        inj.on[stage] = slow
        enqueue(d, c, chunk())
        await until(lambda: d._session.ledger[0].final and d._worker is None)
        assert d.phase == "paused"
        assert states(d)[0] == "pending" and not inj.sent and not inj.send_locked
        assert d.metadata()["reason"] == "lock_lost"
        assert d.metadata()["held_chars"] == len(SECRET)
        assert not inj.locked and sum(n == "lock_enter" for n, _ in inj.calls) == 1
        assert inj.expiries == (1 if elapsed > core.LOCK_TIMER_S else 0)
        assert SECRET not in repr(events) + repr(d.metadata())
        # Restoring authority needs a real explicit resume, never a silent re-entry.
        inj.on.clear()
        d.resume(sid)
        await until(lambda: d._session.ready and all(s == "accepted" for s in states(d)))
        assert "".join(inj.sent) == SECRET


async def test_delayed_successful_arm_never_claims_short_lease(monkeypatch):
    assert (core.LOCK_TIMER_S, core.SEND_TIMEOUT_S, core.SEND_MARGIN_S) == (10.0, 5.0, 1.0)
    clock = Clock()
    async with session(SECRET, monkeypatch=monkeypatch, dictation_slice_chars=48) as (d, _, inj, c, *_):
        monkeypatch.setattr(core, "monotonic", clock)
        claims, remaining = [], []
        original_claim = d._claim
        def claim(*args):
            claims.append(True)
            return original_claim(*args)
        monkeypatch.setattr(d, "_claim", claim)
        def arm(*_):
            if inj.locked:
                assert inj.expires_at == clock() + 10.0
                clock.now += 4.0
            return True
        def confirm():
            clock.now += 3.9
            assert inj.locked
            remaining.append(inj.expires_at - clock())
            return True
        inj.on.update(arm_lock_timer=arm, lock_held=confirm)
        enqueue(d, c, chunk())
        await until(lambda: d._session.ledger[0].final
                    and (d._worker is None or states(d) == ["accepted"]))
        phase, metadata, slice_states = d.phase, d.metadata(), states(d)
    assert remaining == pytest.approx([2.1])
    assert phase == "paused" and metadata["reason"] == "lock_lost"
    assert slice_states == ["pending"] and not claims
    assert not inj.sent and not inj.send_locked
    assert metadata["held_chars"] == len(SECRET)
    assert not inj.locked and inj.expiries == 0


async def test_normal_dispatch_always_has_confirmed_lease(monkeypatch):
    clock = Clock()
    async with session("one two three four", monkeypatch=monkeypatch) as (d, _, inj, c, *_):
        monkeypatch.setattr(core, "monotonic", clock)
        def confirm():
            clock.now += 3.9
            return True
        def send(*_):
            assert inj.locked and inj.expires_at - clock() >= 6.0
            clock.now += 5.0
            assert inj.locked
            return True
        inj.on.update(lock_held=confirm, send=send)
        enqueue(d, c, chunk())
        d.toggle_stop()
        await until(lambda: d.phase == "stopped")
        assert "".join(inj.sent) == "one two three four"
        assert all(inj.send_locked) and inj.expiries == 0
        assert sum(name == "lock_held" for name, _ in inj.calls) == len(inj.sent)
        assert states(d) == ["accepted", "accepted", "accepted"]


@pytest.mark.parametrize("failure", [False, RuntimeError(SECRET)])
async def test_lock_confirmation_failure_holds_pending_text(monkeypatch, failure):
    async with session("held", monkeypatch=monkeypatch) as (d, _, inj, c, events, *_):
        def confirm():
            if isinstance(failure, Exception):
                raise failure
            return failure
        inj.on["lock_held"] = confirm
        enqueue(d, c, chunk())
        await until(lambda: d._session.ledger[0].final and d._worker is None)
        assert d.phase == "paused"
        assert states(d) == ["pending"] and not inj.sent
        assert not inj.locked and d.metadata()["reason"] == "lock_lost"
        assert SECRET not in repr(events) + repr(d.metadata())


async def test_stop_during_delivery_requires_a_fresh_watch(monkeypatch):
    async with session("one two three", monkeypatch=monkeypatch) as (d, _, inj, c, *_):
        starts = sum(n == "watch_start" for n, _ in inj.calls)

        def enter():
            inj.on.pop("lock_enter")
            inj.watch.lose("focus_watch_lost")
            d.toggle_stop()
            return True

        def send(*_):
            assert sum(n == "watch_start" for n, _ in inj.calls) > starts
            return True

        inj.on.update(lock_enter=enter, send=send)
        enqueue(d, c, chunk())
        await until(lambda: d.phase == "stopped" and d._worker is None)
        assert "".join(inj.sent) == "one two three"
        assert sum(n == "watch_start" for n, _ in inj.calls) == starts + 1
        assert states(d) == ["accepted", "accepted"]


async def test_stop_during_resume_watch_start_delivers_all_held_text(monkeypatch):
    async with session("one two three", monkeypatch=monkeypatch) as (d, _, inj, c, _, sid):
        c.rest = chunk()
        d.suspend("mic_off")
        await until(lambda: d._session.ledger[0].final and d._worker is None)
        starts = sum(n == "watch_start" for n, _ in inj.calls)

        def start():
            inj.on.pop("watch_start")
            d.toggle_stop()
            return True

        def send(*_):
            assert sum(n == "watch_start" for n, _ in inj.calls) == starts + 2
            return True

        inj.on.update(watch_start=start, send=send)
        d.resume(sid)
        await until(lambda: d._session.stop_requested and d._worker is None)
        assert d.phase == "stopped"
        assert "".join(inj.sent) == "one two three"
        assert sum(n == "watch_start" for n, _ in inj.calls) == starts + 2
        assert states(d) == ["accepted", "accepted"]
        assert d.metadata()["held_chars"] == 0


@pytest.mark.parametrize("failure", ["watch_start", "focus_changed"])
async def test_stop_during_resume_watch_start_failure_holds_text(monkeypatch, failure):
    async with session("one two three", monkeypatch=monkeypatch) as (d, _, inj, c, _, sid):
        c.rest = chunk()
        d.suspend("mic_off")
        await until(lambda: d._session.ledger[0].final and d._worker is None)
        starts = sum(n == "watch_start" for n, _ in inj.calls)
        attempts = []

        def start():
            attempts.append(True)
            if len(attempts) == 1:
                d.toggle_stop()
            elif failure == "watch_start":
                raise RuntimeError(SECRET)
            else:
                inj.focused = False
            return True

        inj.on["watch_start"] = start
        d.resume(sid)
        await until(lambda: d._session.stop_requested and d._worker is None)
        assert d.phase == "stopped"
        assert len(attempts) == 2
        assert sum(n == "watch_start" for n, _ in inj.calls) == starts + 2
        assert not inj.sent
        assert states(d) == ["pending", "pending"]
        assert d.metadata()["held_chars"] == len("one two three")


async def test_stop_during_lock_cleanup_keeps_later_chunks_behind_pending_slices(monkeypatch):
    async with session("aaaa bbbb cccc", "dddd", monkeypatch=monkeypatch) as (d, _, inj, c, _, sid):
        d.suspend("mic_off")
        d._enqueue(d._session, chunk())
        d._enqueue(d._session, chunk(1))
        await until(lambda: len(d._session.ledger) == 2
                    and all(e.final for e in d._session.ledger) and d._worker is None)
        starts = sum(n == "watch_start" for n, _ in inj.calls)

        def check(*_):
            if inj.sent == ["aaaa "]:
                # Lose focus before claiming the next slice, after the first post-check.
                checks.append(True)
                if len(checks) == 2:
                    inj.on.pop("check")
                    inj.watch.lose()
            return True

        def leave():
            inj.on.pop("lock_leave")
            d.toggle_stop()
            return True

        checks = []
        inj.on.update(check=check, lock_leave=leave)
        d.resume(sid)
        await until(lambda: d.phase == "stopped" and d._worker is None)
        assert inj.sent == ["aaaa ", "bbbb ", "cccc", " dddd"]
        assert sum(n == "watch_start" for n, _ in inj.calls) == starts + 2
        assert states(d) == ["accepted"] * 4


@pytest.mark.parametrize("pause_at", ["send", "post_check"])
@pytest.mark.parametrize("reason", ["focus_changed", "mic_off"])
async def test_pause_during_slice_is_uncertain_even_if_post_check_matches(monkeypatch, pause_at, reason):
    async with session("one two three", monkeypatch=monkeypatch) as (d, _, inj, c, *_):
        def pause(*_):
            if pause_at == "send" or inj.sent:
                inj.on.pop(pause_at if pause_at == "send" else "check")
                if reason == "focus_changed":
                    inj.watch.lose()
                else:
                    d.suspend(reason)
            return True

        inj.on["send" if pause_at == "send" else "check"] = pause
        enqueue(d, c, chunk())
        await until(lambda: d._session.ledger[0].final and d._worker is None)
        assert states(d) == ["uncertain", "pending"]
        assert d.metadata()["delivered_chars"] == 0
        assert d.metadata()["reason"] == "insert_uncertain"
        assert len(inj.sent) == 1 and not inj.locked


async def test_repeated_suspend_is_idle_for_any_suspend_reason(monkeypatch):
    async with session(monkeypatch=monkeypatch) as (d, _, inj, c, events, *_):
        d.suspend("auto_mute")
        await until(lambda: d._worker is None)
        before = (d._session.authority, len(events), c.flushes, len(inj.calls))
        kicks = []
        monkeypatch.setattr(d, "_kick", lambda: kicks.append(True))
        for _ in range(10):
            for reason in core.SUSPEND_REASONS:
                assert d.suspend(reason)["reason"] == "auto_mute"
        assert (d._session.authority, len(events), c.flushes, len(inj.calls)) == before
        assert kicks == []


@pytest.mark.parametrize("reason", sorted(core.SUSPEND_REASONS))
async def test_suspend_preserves_insert_uncertain_reason(monkeypatch, reason):
    async with session("one two three", monkeypatch=monkeypatch) as (d, _, inj, c, events, *_):
        inj.on["send"] = lambda *_: False
        enqueue(d, c, chunk())
        await until(lambda: d.metadata()["reason"] == "insert_uncertain" and d._worker is None)
        assert d.suspend(reason)["reason"] == "insert_uncertain"
        assert d.dashboard()["reason"] == "insert_uncertain"
        assert next(e for e in reversed(events) if "phase" in e)["reason"] == "insert_uncertain"
        assert states(d) == ["uncertain", "pending"]


async def test_cancel_drops_delivered_text_from_dashboard_json(monkeypatch):
    async with session("delivered words", monkeypatch=monkeypatch) as (d, _, inj, c, *_):
        enqueue(d, c, chunk())
        await until(lambda: d._session.delivered == "delivered words" and d._worker is None)
        assert d.dashboard()["delivered"] == "delivered words"
        d.cancel()
        assert "delivered" not in d.dashboard()
        await until(lambda: d.phase == "cancelled" and d._worker is None)
        assert "delivered" not in d.dashboard()
        assert d._session.delivered == ""
        assert "delivered words" not in repr(d.dashboard())
