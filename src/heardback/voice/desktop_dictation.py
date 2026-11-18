"""In-memory desktop dictation ledger; only finalized slices can be dispatched."""
import asyncio
import threading
from dataclasses import dataclass, field
from time import monotonic
from uuid import uuid4

from .audio_in import RATE
from .chunker import Chunk, Chunker
from .desktop_input import IGNORE_LAYERS, InputRefused
from .dictation import DictationError, MAX_TEXT_CHARS, STT_TIMEOUT_S

LOCK_TIMER_S = 10.0
SEND_TIMEOUT_S = 5.0
SEND_MARGIN_S = 1.0
MAX_AUDIO_S = 120.0
MAX_WAITING_CHUNKS = 3
MAX_WAITING_AUDIO_S = 30.0
TERMINAL = ("stopped", "cancelled")
FOCUS_REASONS = {"focus_changed", "destination_closed", "focus_watch_lost", "layer_opened", "layer_open"}
INPUT_REASONS = FOCUS_REASONS | {"locked", "unsupported_class", "denied_class", "unsafe_text"}
SUSPEND_REASONS = {"mic_off", "auto_mute", "device_loss", "user_pause", "ptt", "shutdown"}


@dataclass
class _Slice:
    text: str = field(repr=False)
    state: str = "pending"


@dataclass
class _Entry:
    seq: int
    audio: object = field(repr=False)
    audio_s: float
    close_reason: str
    speech_end: float
    final: bool = False
    lost: bool = False
    unsafe: bool = False
    slices: list[_Slice] = field(default_factory=list, repr=False)
    chars: int = 0
    speech_end_to_stt_ms: int = 0
    stt_ms: int = 0
    check_ms: int = 0
    type_ms: int = 0
    traced: bool = False


@dataclass
class _Session:
    session_id: str
    pin: object
    started: float
    chunker: Chunker
    capture_started: float
    phase: str = "listening"
    reason: str | None = None
    ready: bool = False
    ledger: list[_Entry] = field(default_factory=list, repr=False)
    delivered: str = field(default="", repr=False)
    delivered_chars: int = 0
    first: bool = True
    stop_requested: bool = False
    cancel_requested: bool = False
    resume_requested: bool = False
    delivery_blocked: bool = False
    inference_lost: bool = False
    authority: int = 0
    resuming: bool = False
    watch_needed: bool = True
    next_seq: int = 0
    watch: object = None
    wall_timer: asyncio.TimerHandle | None = None
    wake: asyncio.Event = field(default_factory=asyncio.Event)


class DesktopDictation:
    """One FIFO worker, one real STT future, and forward-only authority per slice."""

    def __init__(self, stt, gpu, prob, injector, io, *, settings, event):
        self.stt, self.gpu, self.prob = stt, gpu, prob
        self.injector, self.io, self.settings, self.event = injector, io, settings, event
        self._lock = threading.RLock()
        self._session: _Session | None = None
        self._loop = None
        self._worker: asyncio.Task | None = None
        self._inference = None
        self._closed = False
        self._shortcut_at = float("-inf")
        self._other_engaged = lambda: False
        self._other_inference_busy = lambda: False
        self._on_start = lambda: None

    def _setting(self, name, default):
        return getattr(self.settings, "dictation_" + name, default)

    def _paste_enabled(self, s):
        return s.pin.cls in self._setting("paste_classes", ())

    def _slices(self, s, text):
        if self._paste_enabled(s):
            return [_Slice(text)]
        return [_Slice(part) for part in self.injector.slices(
            text, self._setting("slice_chars", 48))]

    def _chunker(self):
        return Chunker(self.prob, **{name: self._setting(name, default) for name, default in (
            ("silence_s", 0.6), ("soft_cap_s", 8.0), ("soft_silence_s", 0.25),
            ("hard_cap_s", 20.0), ("min_burst_s", 0.15), ("min_voiced_s", 0.3))})

    @property
    def engaged(self):
        with self._lock:
            return bool(self._session and self._session.phase not in TERMINAL)

    @property
    def phase(self):
        with self._lock:
            return self._session.phase if self._session else None

    def _emit(self, s, reason=None, **fields):
        self.event(phase=s.phase, reason=reason or s.reason, session_id=s.session_id, **fields)

    def _trace(self, entry, outcome):
        if entry.traced:
            return
        entry.traced = True
        self.event(event="dictation_chunk", seq=entry.seq, audio_s=entry.audio_s,
                   close_reason=entry.close_reason, speech_end_to_stt_ms=entry.speech_end_to_stt_ms,
                   stt_ms=entry.stt_ms, check_ms=entry.check_ms, type_ms=entry.type_ms,
                   chars=entry.chars, outcome=outcome)

    def _kick(self):
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._ensure_worker)

    def _ensure_worker(self):
        with self._lock:
            if self._session and self._session.phase not in TERMINAL and (self._worker is None or self._worker.done()):
                self._worker = self._loop.create_task(self._run(self._session))

    def start(self, pin):
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._closed:
                raise DictationError("shutdown")
            if self.engaged or self._other_engaged():
                raise DictationError("session_active")
            if self._inference is not None or self._other_inference_busy():
                raise DictationError("inference_busy")
            if self._worker is not None and not self._worker.done():
                raise DictationError("insertion_in_progress")
            discarded = self._held_chars(self._session) if self._session else 0
            getattr(self.prob, "reset", lambda: None)()
            now = monotonic()
            s = _Session(uuid4().hex, pin, now, self._chunker(), now)
            self._session, self._loop = s, loop
            s.wall_timer = loop.call_later(self._setting("max_session_s", 1800.0), self._wall_limit, s)
            self._on_start()
            self._emit(s, "start", discarded_chars=discarded)
            self._kick()
            return {**self.metadata(), "discarded_chars": discarded}

    def _wall_limit(self, s):
        with self._lock:
            if self._session is s and s.phase not in TERMINAL and not s.stop_requested:
                if s.phase == "paused":
                    # A timer cannot grant the delivery authority of a user press.
                    s.delivery_blocked, s.resume_requested = True, False
                    s.authority += 1
                    s.phase, s.reason, s.stop_requested = "stopping", "duration_limit", True
                    self._emit(s)
                    self._kick()
                else:
                    self.toggle_stop()

    def _enqueue(self, s, chunk: Chunk):
        """Called under the session lock, including when a pause flushes capture."""
        entry = _Entry(s.next_seq, chunk.audio, len(chunk.audio) / RATE,
                       chunk.reason, s.capture_started + chunk.speech_end_s)
        s.next_seq += 1
        s.ledger.append(entry)
        if entry.audio_s > MAX_AUDIO_S:
            entry.audio, entry.lost = None, True
            s.inference_lost = True
            self._pause(s, "audio_limit")
        self._kick()

    def _flush(self, s):
        if s.ready:
            s.ready = False
            chunk = s.chunker.flush()
            if chunk is not None:
                self._enqueue(s, chunk)

    def feed(self, frame):
        with self._lock:
            s = self._session
            if not s or s.phase in TERMINAL:
                return False
            if s.phase != "listening" or not s.ready or s.cancel_requested:
                return True
            if monotonic() - s.started >= self._setting("max_session_s", 1800.0):
                self.toggle_stop()
                return True
            for chunk in s.chunker.feed(frame):
                self._enqueue(s, chunk)
            waiting = [e for e in s.ledger if e.audio is not None]
            if len(waiting) > MAX_WAITING_CHUNKS or sum(e.audio_s for e in waiting) > MAX_WAITING_AUDIO_S:
                self._pause(s, "backlog")
            return True

    def _pause(self, s, reason):
        if s.phase in TERMINAL or s.cancel_requested:
            return
        s.phase, s.reason = "paused", reason
        s.authority += 1
        s.resuming = False
        s.watch_needed = True
        s.delivery_blocked, s.resume_requested = True, False
        self._flush(s)
        self._emit(s)
        self._kick()

    def focus_lost(self, reason):
        with self._lock:
            if self._session:
                self._focus_lost(self._session, reason)

    def _focus_lost(self, s, reason):
        with self._lock:
            if self._session is s:
                self._pause(s, reason if reason in FOCUS_REASONS else "focus_watch_lost")

    def suspend(self, reason):
        with self._lock:
            s = self._session
            if not s or s.phase in TERMINAL:
                return None
            if s.phase == "paused" and (s.reason in SUSPEND_REASONS or s.reason in {
                    "insert_uncertain", "ctrl_release_failed", "key_release_failed"}):
                return self.metadata()
            self._pause(s, reason if reason in SUSPEND_REASONS else "user_pause")
            return self.metadata()

    def toggle_stop(self):
        with self._lock:
            s = self._session
            if not s or s.phase in TERMINAL:
                return self.metadata()
            if s.stop_requested:
                self._emit(s, "already_stopping")
                return self.metadata()
            # An uncertain slice requires explicit resume; stop cannot acknowledge it.
            s.delivery_blocked = s.inference_lost or any(e.unsafe for e in s.ledger) or (
                s.phase == "paused" and any(p.state == "uncertain" for e in s.ledger for p in e.slices))
            s.authority += 1
            s.resume_requested = False
            s.phase, s.reason, s.stop_requested = "stopping", "stop", True
            self._flush(s)
            self._emit(s)
            self._kick()
            return self.metadata()

    def resume(self, session_id):
        with self._lock:
            s = self._session
            if s is None:
                raise DictationError("no_session")
            if s.session_id != session_id:
                raise DictationError("stale_session")
            if s.phase != "paused" or s.stop_requested or s.cancel_requested:
                raise DictationError("not_paused")
            if self._inference is not None:
                raise DictationError("inference_busy")
            if any(e.unsafe for e in s.ledger):
                raise DictationError("unsafe_text")
            if monotonic() - s.started >= self._setting("max_session_s", 1800.0):
                raise DictationError("duration_limit")
            s.resume_requested = True
            s.authority += 1
            self._kick()
            return self.metadata()

    def cancel(self):
        with self._lock:
            s = self._session
            if not s or s.phase in TERMINAL:
                return self.metadata()
            s.cancel_requested, s.ready = True, False
            s.delivered = ""
            s.authority += 1
            s.resume_requested = False
            s.reason = "shutdown" if self._closed else "user_cancel"
            for entry in s.ledger:
                entry.audio = None
                for piece in entry.slices:
                    if piece.state != "claimed":
                        piece.text = ""
            self._loop.call_soon_threadsafe(s.wake.set)
            if s.wall_timer:
                s.wall_timer.cancel()
            self._kick()
            return self.metadata()

    def shutdown(self):
        with self._lock:
            self._closed = True
            self.cancel()

    def shortcut_pressed(self):
        with self._lock:
            self._shortcut_at = monotonic()

    async def _io(self, function, *args):
        return await self._loop.run_in_executor(self.io, function, *args)

    async def _check(self, s, entry=None):
        started = monotonic()
        try:
            await self._io(self.injector.check, s.pin, self._setting("ignore_layers", IGNORE_LAYERS))
            return True
        except Exception as error:
            reason = error.code if isinstance(error, InputRefused) and error.code in INPUT_REASONS else "input_check_failed"
            with self._lock:
                self._pause(s, reason)
            return False
        finally:
            if entry is not None:
                entry.check_ms += max(0, int((monotonic() - started) * 1000))

    async def _stop_watch(self, s):
        with self._lock:
            watch, s.watch = s.watch, None
        if watch is not None:
            try:
                await self._io(watch.stop)
            except Exception:
                self._emit(s, "focus_watch_stop_failed")

    async def _start_watch(self, s, *, stopping=False):
        with self._lock:
            authority = s.authority
            resuming = s.resume_requested
            s.resume_requested = False
        await self._stop_watch(s)
        with self._lock:
            if s.cancel_requested or s.stop_requested != stopping or s.authority != authority:
                return False
            # Startup loss must be able to change phase before start() returns.
            try:
                s.watch = self.injector.FocusWatch(
                    s.pin, lambda reason: self._focus_lost(s, reason),
                    self._setting("ignore_layers", IGNORE_LAYERS))
            except Exception:
                self._pause(s, "focus_watch_lost")
                return False
            watch = s.watch
        try:
            await self._io(watch.start)
        except Exception:
            self._focus_lost(s, "focus_watch_lost")
        if not await self._check(s):
            return False
        with self._lock:
            if s.cancel_requested or s.stop_requested != stopping or s.authority != authority:
                return False
            s.delivery_blocked = False
            s.watch_needed = False
            if stopping:
                return True
            s.inference_lost = False  # Explicit resume acknowledges a settled, lost chunk.
            s.phase, s.reason = "listening", None
            s.capture_started = monotonic()
            s.chunker = self._chunker()
            s.resuming = resuming
            s.ready = not resuming
            if s.ready:
                self._emit(s, "listening")
            return True

    def _settle_inference(self, future):
        # The concurrent future, rather than a cancellable asyncio wrapper, owns the slot.
        with self._lock:
            if self._inference is future:
                self._inference = None
        try:
            future.exception()
        except BaseException:
            pass

    async def _transcribe(self, s, entry):
        started = monotonic()
        entry.speech_end_to_stt_ms = max(0, int((started - entry.speech_end) * 1000))
        with self._lock:
            if s.cancel_requested or s.inference_lost:
                return
            audio, entry.audio = entry.audio, None
            try:
                future = self.gpu.submit(self.stt.transcribe_dictation, audio)
            except Exception:
                entry.lost, s.inference_lost = True, True
                self._pause(s, "inference_error")
                self._trace(entry, "lost")
                return
            self._inference = future
            future.add_done_callback(self._settle_inference)
        wrapped = asyncio.wrap_future(future)
        # Consume exceptions even when a timed-out or cancelled session discards the result.
        wrapped.add_done_callback(lambda f: None if f.cancelled() else f.exception())
        cancel = self._loop.create_task(s.wake.wait())
        try:
            done, _ = await asyncio.wait({wrapped, cancel}, timeout=STT_TIMEOUT_S,
                                         return_when=asyncio.FIRST_COMPLETED)
            with self._lock:
                if s.cancel_requested:
                    return
                if wrapped not in done:
                    entry.lost, s.inference_lost = True, True
                    self._pause(s, "inference_timeout")
                    return
                try:
                    text = wrapped.result()
                    if not isinstance(text, str):
                        raise ValueError
                except Exception:
                    entry.lost, s.inference_lost = True, True
                    self._pause(s, "inference_error")
                    return
                entry.final = True
                entry.chars = len(text)
                if not text:
                    return
                try:
                    if len(text) > MAX_TEXT_CHARS:
                        raise InputRefused("text_limit")
                    text = self.injector.sanitize(text)
                    joined = self.injector.join(text, s.first)
                    entry.slices = self._slices(s, joined)
                    s.first = False
                except Exception:
                    entry.slices = [_Slice(text)]
                    entry.unsafe = True
                    self._pause(s, "text_limit" if len(text) > MAX_TEXT_CHARS else "unsafe_text")
        finally:
            entry.stt_ms = max(0, int((monotonic() - started) * 1000))
            if entry.lost:
                self._trace(entry, "lost")
            elif entry.final and not entry.chars:
                self._trace(entry, "empty")
            cancel.cancel()
            await asyncio.gather(cancel, return_exceptions=True)

    def _allowed(self, s):
        return (self._session is s and not s.cancel_requested and not s.delivery_blocked
                and not s.watch_needed and s.phase in ("listening", "stopping"))

    def _claim(self, s, entry, piece):
        with self._lock:
            if not entry.final or entry.unsafe or piece.state != "pending" or not self._allowed(s):
                return False
            if monotonic() - self._shortcut_at < self._setting("post_shortcut_ms", 491) / 1000:
                return False
            piece.state = "claimed"
            return True

    def _dispatch(self, s, entry, piece):
        # Renew and confirm after slow checks, without an executor/loop gap before send.
        try:
            renewed_at = monotonic()
            armed = self.injector.arm_lock_timer(LOCK_TIMER_S)
        except Exception:
            armed = False
        if not armed:
            with self._lock:
                self._pause(s, "lock_timer_failed")
            return None
        try:
            held = self.injector.lock_held()
        except Exception:
            held = False
        with self._lock:
            if not held or LOCK_TIMER_S - (monotonic() - renewed_at) < SEND_TIMEOUT_S + SEND_MARGIN_S:
                self._pause(s, "lock_lost")
                return None
            # Signals may arrive after claim; consume authority without dispatch in that case.
            if not self._claim(s, entry, piece):
                return None
            if not self._allowed(s):
                return False
            text = piece.text
        send = self.injector.paste if self._paste_enabled(s) else self.injector.send
        return send(text, self._setting("type_delay_ms", 20), SEND_TIMEOUT_S)

    async def _timer(self, s):
        try:
            armed = await self._io(self.injector.arm_lock_timer, LOCK_TIMER_S)
        except Exception:
            armed = False
        if not armed:
            with self._lock:
                self._pause(s, "lock_timer_failed")
        return bool(armed)

    async def _leave_lock(self, s):
        try:
            left = await self._io(self.injector.lock_leave)
        except Exception:
            left = False
        if not left:
            self._emit(s, "lock_leave_failed")
            return False  # Keep the outside reset armed if leaving could not be confirmed.
        try:
            disarmed = await self._io(self.injector.disarm_lock_timer)
        except Exception:
            disarmed = False
        if not disarmed:
            self._emit(s, "disarm_lock_timer_failed")
        return True

    async def _deliver(self, s, entry):
        attempted_lock = False
        try:
            while True:
                with self._lock:
                    if not self._allowed(s) or entry.unsafe:
                        return
                    piece = next((p for p in entry.slices if p.state == "pending"), None)
                    if piece is None:
                        return
                while True:
                    with self._lock:
                        remaining = self._setting("post_shortcut_ms", 491) / 1000 - (monotonic() - self._shortcut_at)
                        if not self._allowed(s):
                            return
                    if remaining <= 0:
                        break
                    if attempted_lock:
                        attempted_lock = False
                        if not await self._leave_lock(s):
                            with self._lock:
                                self._pause(s, "lock_leave_failed")
                            return
                    await asyncio.sleep(min(remaining, 0.01))
                if not attempted_lock:
                    if not await self._check(s, entry):
                        return
                    if not await self._timer(s):
                        return
                    attempted_lock = True
                    try:
                        entered = await self._io(self.injector.lock_enter)
                    except Exception:
                        entered = False
                    if not entered:
                        with self._lock:
                            self._pause(s, "lock_enter_failed")
                        return
                if not await self._check(s, entry):
                    return
                with self._lock:
                    if not self._allowed(s):
                        return
                    authority = s.authority
                started = monotonic()
                failure_reason = "insert_uncertain"
                try:
                    sent = await self._io(self._dispatch, s, entry, piece)
                except Exception as error:
                    if isinstance(error, InputRefused) and error.code in {"ctrl_release_failed", "key_release_failed"}:
                        failure_reason = error.code
                    sent = False
                entry.type_ms += max(0, int((monotonic() - started) * 1000))
                with self._lock:
                    if piece.state != "claimed":
                        continue  # A shortcut during checks defers this still-pending slice.
                matched = await self._check(s, entry)
                with self._lock:
                    # A stop press alone keeps the watch; a pause always revokes it.
                    uninterrupted = s.authority == authority or (
                        s.stop_requested and not s.watch_needed and not s.cancel_requested)
                    piece.state = "accepted" if sent and matched and uninterrupted else "uncertain"
                    if piece.state == "accepted":
                        s.delivered_chars += len(piece.text)
                        s.delivered = (s.delivered + piece.text)[-MAX_TEXT_CHARS:]
                        piece.text = ""
                    else:
                        self._pause(s, failure_reason)
                        self._trace(entry, "uncertain")
                        return failure_reason
        finally:
            if attempted_lock:
                await self._leave_lock(s)

    async def _run(self, s):
        warning = None
        try:
            while True:
                with self._lock:
                    if self._session is not s or s.cancel_requested:
                        break
                    resume = s.resume_requested
                    need_watch = (s.watch_needed and not s.delivery_blocked
                                  and s.phase in ("listening", "stopping"))
                if resume:
                    await self._start_watch(s)
                elif need_watch:
                    await self._start_watch(s, stopping=s.stop_requested)
                with self._lock:
                    entry = next((e for e in s.ledger if e.audio is not None), None)
                    if s.inference_lost:
                        entry = None
                if entry is not None:
                    await self._transcribe(s, entry)
                with self._lock:
                    if s.cancel_requested:
                        break
                    deliverable = [e for e in s.ledger if e.final and not e.unsafe
                                   and any(p.state == "pending" for p in e.slices)] if self._allowed(s) else []
                for ready in deliverable:
                    warning = await self._deliver(s, ready) or warning
                    with self._lock:
                        if any(p.state == "pending" for p in ready.slices):
                            break
                        if ready.slices and all(p.state == "accepted" for p in ready.slices):
                            self._trace(ready, "accepted")
                with self._lock:
                    if entry is not None and entry.final and not entry.traced:
                        self._trace(entry, "held")
                    if s.cancel_requested:
                        break
                    more = not s.inference_lost and any(e.audio is not None for e in s.ledger)
                    stop_watch = (s.stop_requested and s.watch_needed and not s.delivery_blocked
                                  and any(p.state == "pending" for e in s.ledger for p in e.slices))
                    if more or s.resume_requested or stop_watch:
                        continue
                    if s.resuming and self._allowed(s):
                        s.resuming = False
                        s.ready = True
                        self._emit(s, "listening")
                    break
        finally:
            with self._lock:
                leave_watch = s.cancel_requested or s.phase == "paused" or s.stop_requested
                settle_stop = s.stop_requested
            if leave_watch:
                await self._stop_watch(s)
            with self._lock:
                if s.cancel_requested:
                    for entry in s.ledger:
                        entry.audio = None
                        for piece in entry.slices:
                            piece.text = ""
                        self._trace(entry, "cancelled")
                    s.phase = "cancelled"
                    self._emit(s)
                elif s.stop_requested and settle_stop:
                    s.phase = "stopped"
                    if s.wall_timer:
                        s.wall_timer.cancel()
                    for held in s.ledger:
                        self._trace(held, "lost" if held.lost else "held")
                    self._emit(s)
                if warning in {"ctrl_release_failed", "key_release_failed"}:
                    self._emit(s, warning, event="dictation_warning")
                self._worker = None
                # A feed/signal may have queued work while watch.stop was in flight.
                if not s.cancel_requested and s.phase not in TERMINAL and (s.resume_requested or s.stop_requested or (
                        not s.inference_lost and any(e.audio is not None for e in s.ledger))):
                    self._kick()

    @staticmethod
    def _held_chars(s):
        return sum(len(p.text) for e in s.ledger for p in e.slices if p.state != "accepted")

    def metadata(self):
        with self._lock:
            s = self._session
            if s is None:
                return {"phase": "idle"}
            return {"session_id": s.session_id, "mode": "desktop", "phase": s.phase,
                    "reason": s.reason, "class": s.pin.cls, "delivered_chars": s.delivered_chars,
                    "held_chars": self._held_chars(s)}

    def dashboard(self):
        with self._lock:
            s = self._session
            if s is None:
                return None
            delivered = {} if s.cancel_requested else {"delivered": s.delivered}
            return {**self.metadata(), **delivered,
                    "held": "".join(p.text for e in s.ledger for p in e.slices if p.state != "accepted")}


class DictationSlot:
    """Keep tmux controls intact while exposing the mode that owns the mic slot."""

    def __init__(self, tmux, desktop):
        self.tmux, self.desktop = tmux, desktop
        self._mode = "desktop" if desktop.engaged else "tmux"
        desktop._other_engaged = lambda: tmux.engaged
        desktop._other_inference_busy = lambda: tmux._job is not None
        desktop._on_start = self._select_desktop

    def _select_desktop(self):
        self._mode = "desktop"

    def start(self, *args, **kwargs):
        with self.desktop._lock:
            if self.desktop.engaged:
                raise DictationError("session_active")
            if self.desktop._inference is not None:
                raise DictationError("inference_busy")
            result = self.tmux.start(*args, **kwargs)
            self._mode = "tmux"
            return result

    @property
    def engaged(self):
        return self.desktop.engaged or self.tmux.engaged

    @property
    def phase(self):
        return self.desktop.phase if self._mode == "desktop" else self.tmux.phase

    def metadata(self):
        return self.desktop.metadata() if self._mode == "desktop" else self.tmux.metadata()

    def dashboard(self):
        return self.desktop.dashboard() if self._mode == "desktop" else self.tmux.dashboard()

    def __getattr__(self, name):
        return getattr(self.tmux, name)
