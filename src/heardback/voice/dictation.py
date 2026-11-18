"""Bounded in-memory audio capture and accumulated-audio dictation preview."""
import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Callable
from uuid import uuid4

import numpy as np

from .audio_in import FRAME, RATE

MAX_AUDIO_S = 120
MAX_SAMPLES = MAX_AUDIO_S * RATE
MAX_SESSION_S = 5 * 60
MAX_TEXT_CHARS = 4000
PREVIEW_INTERVAL_S = 3.0
STT_TIMEOUT_S = 15.0


class DictationError(ValueError):
    """A short, safe control error code; never includes dictated text."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class _Session:
    session_id: str
    target: dict
    started: float
    phase: str = "listening"
    reason: str | None = None
    frames: list[np.ndarray] = field(default_factory=list)
    samples: int = 0
    revision: int = 0
    generation: int = 1
    preview: str = ""
    preview_revision: int = 0
    text_revision: int = 0
    final_revision: int | None = None
    final: bool = False
    speech_pending: bool = False
    last_preview_at: float = 0.0
    commit_started: bool = False
    commit_attempt: int = 0
    wall_timer: asyncio.TimerHandle | None = None


@dataclass
class _Job:
    session_id: str
    generation: int
    revision: int
    kind: str
    future: asyncio.Future
    settled: asyncio.Event
    timed_out: bool = False
    result: str | None = None
    error: Exception | None = None


def _transcribe(stt, frames: tuple[np.ndarray, ...]) -> str:
    audio = np.concatenate(frames) if frames else np.zeros(0, dtype=np.int16)
    return stt.transcribe_dictation(audio)


class Dictation:
    """Own one bounded session and at most one actual executor job at a time."""

    def __init__(self, stt, gpu, prob: Callable[[np.ndarray], float], *, timeout_s: float = STT_TIMEOUT_S,
                 event: Callable[..., None] = lambda **_: None):
        self.stt, self.gpu, self.prob = stt, gpu, prob
        self.timeout_s, self.event = timeout_s, event
        self._lock = threading.RLock()
        self._session: _Session | None = None
        self._job: _Job | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._vad_rest = np.zeros(0, dtype=np.int16)

    @property
    def engaged(self) -> bool:
        with self._lock:
            return bool(self._session and self._session.phase not in ("finished", "cancelled"))

    @property
    def phase(self) -> str | None:
        with self._lock:
            return self._session.phase if self._session else None

    def _emit(self, phase: str, reason: str | None, session: _Session, *, chars: int | None = None,
              elapsed_ms: int | None = None) -> None:
        self.event(phase=phase, reason=reason, session_id=session.session_id,
                   chars=len(session.preview) if chars is None else chars,
                   elapsed_ms=elapsed_ms)

    def _current(self, session_id: str) -> _Session:
        s = self._session
        if s is None:
            raise DictationError("no_session")
        if s.session_id != session_id:
            raise DictationError("stale_session")
        return s

    def start(self, target: dict) -> dict:
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._session and self._session.phase not in ("finished", "cancelled"):
                raise DictationError("session_active")
            if self._job is not None:
                raise DictationError("inference_busy")
            sid = uuid4().hex
            now = time.monotonic()
            s = _Session(sid, dict(target), now, last_preview_at=now)
            self._session, self._loop = s, loop
            s.wall_timer = loop.call_later(MAX_SESSION_S, self._wall_limit, sid)
        getattr(self.prob, "reset", lambda: None)()
        self._vad_rest = np.zeros(0, dtype=np.int16)
        self._emit("listening", "start", s)
        return self.metadata()

    def _wall_limit(self, session_id: str) -> None:
        changed = None
        with self._lock:
            s = self._session
            if s and s.session_id == session_id and s.phase in ("listening", "paused"):
                s.phase, s.reason = "review", "duration_limit"
                s.speech_pending = False
                changed = s
        if changed:
            self._emit("review", "duration_limit", changed)

    def _speech(self, frame: np.ndarray) -> bool:
        buf = np.concatenate((self._vad_rest, frame)) if len(self._vad_rest) else frame
        usable = len(buf) // 512 * 512
        self._vad_rest = buf[usable:].copy()
        speech = False
        for i in range(0, usable, 512):
            speech = self.prob(buf[i:i + 512]) >= 0.5 or speech
        return speech

    def feed(self, frame: np.ndarray) -> bool:
        """Consume mic frames exclusively while active; capture only in listening phase."""
        with self._lock:
            s = self._session
            if not s or s.phase in ("finished", "cancelled"):
                return False
            if s.phase != "listening":
                return True
        samples = np.asarray(frame, dtype=np.int16).reshape(-1)
        if not len(samples):
            return True
        speech = self._speech(samples)
        now = time.monotonic()
        changed = None
        schedule = False
        with self._lock:
            s = self._session
            if not s or s.phase != "listening":
                return True
            if now - s.started >= MAX_SESSION_S:
                s.phase, s.reason = "review", "duration_limit"
                s.speech_pending = False
                changed = (s, "review", "duration_limit")
            else:
                room = MAX_SAMPLES - s.samples
                captured = samples[:room]
                if len(captured):
                    s.frames.append(captured.copy())
                    s.samples += len(captured)
                    s.revision += 1
                    s.speech_pending |= speech
                if s.samples >= MAX_SAMPLES:
                    s.phase, s.reason = "review", "audio_limit"
                    s.speech_pending = False
                    changed = (s, "review", "audio_limit")
                elif s.speech_pending and now - s.last_preview_at >= PREVIEW_INTERVAL_S and self._job is None:
                    schedule = True
        if changed:
            self._emit(changed[1], changed[2], changed[0])
        if schedule:
            self._launch_preview()
        return True

    def _launch(self, s: _Session, kind: str) -> _Job | None:
        with self._lock:
            if self._job is not None or self._session is not s:
                return None
            frames = tuple(s.frames)
            if not frames:
                return None
            loop = asyncio.get_running_loop()
            job = _Job(s.session_id, s.generation, s.revision, kind,
                       loop.run_in_executor(self.gpu, _transcribe, self.stt, frames), asyncio.Event())
            self._job = job
            job.future.add_done_callback(lambda _f, owned=job: self._settle(owned))
            loop.create_task(self._watch(job))
            return job

    def _launch_preview(self) -> None:
        with self._lock:
            s = self._session
            if not s or s.phase != "listening" or not s.speech_pending or self._job is not None:
                return
            s.speech_pending = False
            s.last_preview_at = time.monotonic()
        job = self._launch(s, "preview")
        if job is None:
            with self._lock:
                if self._session is s and s.phase == "listening":
                    s.speech_pending = True

    async def _watch(self, job: _Job) -> None:
        done, _ = await asyncio.wait({job.future}, timeout=self.timeout_s)
        if done:
            return
        changed = None
        with self._lock:
            job.timed_out = True
            s = self._session
            if s and s.session_id == job.session_id and s.generation == job.generation:
                if job.kind == "preview" and s.phase in ("listening", "paused", "review"):
                    s.reason = "preview_timeout"
                    changed = (s, s.phase, s.reason)
                elif job.kind == "final" and s.phase == "transcribing":
                    s.reason = "final_timeout"
                    changed = (s, s.phase, s.reason)
        if changed:
            self._emit(changed[1], changed[2], changed[0])

    def _settle(self, job: _Job) -> None:
        try:
            job.result = job.future.result()
        except BaseException as e:
            job.error = e
        job.settled.set()
        with self._lock:
            if self._job is job:
                self._job = None
            s = self._session
            apply = bool(s and s.session_id == job.session_id and s.generation == job.generation and
                         not job.timed_out and job.kind == "preview" and
                         s.phase in ("listening", "paused", "review"))
            changed = None
            if apply:
                if job.error:
                    s.reason = "preview_error"
                    changed = (s, s.phase, s.reason)
                elif job.result is not None and len(job.result) <= MAX_TEXT_CHARS and \
                        job.revision >= s.preview_revision:
                    if job.result != s.preview:
                        s.preview = job.result
                        s.text_revision += 1
                    s.preview_revision = job.revision
                    changed = (s, s.phase, s.reason)
                elif job.result is not None:
                    s.phase, s.reason = "review", "text_limit"
                    s.speech_pending = False
                    changed = (s, s.phase, s.reason)
            schedule = bool(s and s.phase == "listening" and s.speech_pending and
                            time.monotonic() - s.last_preview_at >= PREVIEW_INTERVAL_S)
        if changed:
            elapsed = int((time.monotonic() - (s.started if s else time.monotonic())) * 1000)
            self._emit(changed[1], changed[2], changed[0], elapsed_ms=elapsed)
        if schedule:
            self._launch_preview()

    def pause(self, session_id: str) -> dict:
        with self._lock:
            s = self._current(session_id)
            if s.phase != "listening":
                raise DictationError("not_listening")
            s.phase, s.reason = "paused", "user_pause"
            s.speech_pending = False
        getattr(self.prob, "reset", lambda: None)()
        self._vad_rest = np.zeros(0, dtype=np.int16)
        self._emit("paused", "user_pause", s)
        return self.metadata()

    def resume(self, session_id: str) -> dict:
        with self._lock:
            s = self._current(session_id)
            if s.phase != "paused":
                raise DictationError("not_paused")
            if time.monotonic() - s.started >= MAX_SESSION_S:
                s.phase, s.reason = "review", "duration_limit"
                raise DictationError("duration_limit")
            s.phase, s.reason = "listening", None
            s.speech_pending = False
        getattr(self.prob, "reset", lambda: None)()
        self._vad_rest = np.zeros(0, dtype=np.int16)
        self._emit("listening", "resume", s)
        return self.metadata()

    def begin_finish(self, session_id: str) -> dict:
        with self._lock:
            s = self._current(session_id)
            if s.final_revision is not None:
                return self.metadata()
            if s.phase not in ("listening", "paused", "review"):
                raise DictationError("not_finishable")
            if time.monotonic() - s.started >= MAX_SESSION_S and s.reason is None:
                s.reason = "duration_limit"
            s.phase, s.generation, s.speech_pending = "transcribing", s.generation + 1, False
            s.reason = s.reason or "finish"
        self._emit("transcribing", "finish", s)
        return self.metadata()

    async def finish(self, session_id: str) -> dict:
        with self._lock:
            s = self._current(session_id)
            if s.final_revision is not None:
                return {"text": s.preview, "revision": s.final_revision, "target": dict(s.target)}
            if s.phase != "transcribing":
                self.begin_finish(session_id)
            generation = s.generation
            job = self._job
        if job is not None:
            try:
                await asyncio.wait_for(job.settled.wait(), timeout=self.timeout_s)
            except TimeoutError:
                return self._final_failure(s, "preview_busy", generation)
        with self._lock:
            if self._session is not s or s.phase != "transcribing":
                raise DictationError("stale_session")
            if s.generation != generation:
                raise DictationError("stale_session")
            job = self._launch(s, "final")
        if job is None:
            return self._final_failure(s, "empty_audio", generation)
        try:
            await asyncio.wait_for(job.settled.wait(), timeout=self.timeout_s)
        except TimeoutError:
            job.timed_out = True
            return self._final_failure(s, "final_timeout", job.generation)
        if job.error:
            return self._final_failure(s, "final_error", job.generation)
        if job.result is None or not job.result.strip():
            return self._final_failure(s, "empty_transcript", job.generation)
        if len(job.result) > MAX_TEXT_CHARS:
            return self._final_failure(s, "text_limit", job.generation)
        with self._lock:
            if self._session is not s or s.generation != job.generation or s.phase != "transcribing":
                raise DictationError("stale_session")
            s.preview = job.result
            s.final = True
            s.text_revision += 1
            s.final_revision = s.text_revision
            s.frames.clear()
            s.samples = 0
            s.phase = "review"
            s.reason = "finalized"
        self._emit("review", "finalized", s, elapsed_ms=int((time.monotonic() - s.started) * 1000))
        return {"text": job.result, "revision": s.final_revision, "target": dict(s.target)}

    def _final_failure(self, s: _Session, reason: str, generation: int) -> dict:
        changed = False
        with self._lock:
            if self._session is s and s.phase == "transcribing" and s.generation == generation:
                s.generation += 1
                s.phase, s.reason = "review", reason
                changed = True
        if changed:
            self._emit("review", reason, s)
        return {"error": reason}

    def awaiting_commit(self, session_id: str, revision: int, attempt: int,
                        reason: str = "confirmation_required") -> dict:
        with self._lock:
            s = self._current(session_id)
            if s.final_revision != revision or not s.final or s.phase != "preparing" or \
                    s.commit_attempt != attempt:
                raise DictationError("stale_revision")
            if s.commit_started:
                raise DictationError("commit_consumed")
            s.phase, s.reason = "awaiting_commit", reason
            s.commit_attempt += 1
        self._emit("awaiting_commit", reason, s)
        return self.metadata()

    def begin_commit(self, session_id: str, revision: int, *, explicit: bool) -> dict:
        with self._lock:
            s = self._current(session_id)
            if s.final_revision != revision or not s.final or s.commit_started:
                raise DictationError("stale_revision")
            if explicit and s.phase != "awaiting_commit":
                raise DictationError("confirmation_not_pending")
            if not explicit and s.phase != "review":
                raise DictationError("not_reviewable")
            s.phase, s.reason = "preparing", "commit_check"
            s.commit_attempt += 1
            attempt = s.commit_attempt
        self._emit("preparing", "explicit_commit" if explicit else "finish", s)
        return {**self.metadata(), "attempt": attempt}

    def commit_payload(self, session_id: str, revision: int) -> dict:
        """Return the in-memory final revision and pinned target for the insertion worker."""
        with self._lock:
            s = self._current(session_id)
            if s.final_revision != revision or not s.final or s.commit_started or \
                    s.phase not in ("review", "awaiting_commit"):
                raise DictationError("stale_revision")
            return {"target": dict(s.target), "text": s.preview}

    def claim_commit(self, session_id: str, revision: int, attempt: int) -> bool:
        """Called immediately before the one paste dispatch; never re-arm an uncertain send."""
        with self._lock:
            try:
                s = self._current(session_id)
            except DictationError:
                return False
            if s.final_revision != revision or not s.final or s.commit_started or \
                    s.phase != "preparing" or s.commit_attempt != attempt:
                return False
            s.commit_started = True
            s.phase, s.reason = "inserting", "dispatch_started"
            return True

    def complete_commit(self, session_id: str, revision: int, attempt: int, *, accepted: bool,
                        reason: str) -> dict:
        with self._lock:
            s = self._current(session_id)
            if s.final_revision != revision or not s.commit_started or s.commit_attempt != attempt:
                raise DictationError("stale_revision")
            s.phase, s.reason = ("finished", "accepted") if accepted else ("review", reason)
            if accepted:
                s.preview = ""
                s.frames.clear()
                s.samples = 0
                s.final = False
                if s.wall_timer:
                    s.wall_timer.cancel()
        phase, why = s.phase, s.reason
        self._emit(phase, why, s)
        return self.metadata()

    def attempt_failed(self, session_id: str, revision: int, reason: str, attempt: int) -> dict | None:
        with self._lock:
            s = self._session
            if not s or s.session_id != session_id or s.final_revision != revision or \
                    s.phase != "preparing" or s.commit_attempt != attempt:
                return None
            s.phase, s.reason = "review", reason
            s.commit_attempt += 1
        self._emit("review", reason, s)
        return self.metadata()

    def attempt_phase(self, session_id: str, revision: int, attempt: int) -> str | None:
        """Return phase only while a delayed insertion result still belongs to this exact attempt."""
        with self._lock:
            s = self._session
            if not s or s.session_id != session_id or s.final_revision != revision or \
                    s.commit_attempt != attempt:
                return None
            return s.phase

    def cancel(self, session_id: str, *, reason: str = "user_cancel") -> dict:
        with self._lock:
            s = self._current(session_id)
            if s.phase == "inserting":
                raise DictationError("insertion_in_progress")
            s.generation += 1
            s.commit_attempt += 1
            s.phase, s.reason = "cancelled", reason
            s.preview, s.final, s.final_revision = "", False, None
            s.frames.clear()
            s.samples = 0
            s.speech_pending = False
            if s.wall_timer:
                s.wall_timer.cancel()
        getattr(self.prob, "reset", lambda: None)()
        self._vad_rest = np.zeros(0, dtype=np.int16)
        self._emit("cancelled", reason, s)
        return self.metadata()

    def suspend(self, reason: str) -> dict | None:
        """Stop capture for mic-off, auto-mute, device loss, or shutdown; never finish or insert."""
        with self._lock:
            s = self._session
            if not s or s.phase in ("finished", "cancelled", "inserting"):
                return None
            if s.phase == "preparing":
                s.commit_attempt += 1
                s.phase, s.reason = "review", reason
                changed = s
            elif s.phase not in ("listening", "paused", "transcribing"):
                return self.metadata()
            else:
                s.generation += 1
                s.phase, s.reason = "paused", reason
                s.speech_pending = False
                changed = s
        getattr(self.prob, "reset", lambda: None)()
        self._vad_rest = np.zeros(0, dtype=np.int16)
        self._emit(changed.phase, reason, changed)
        return self.metadata()

    def shutdown(self) -> None:
        with self._lock:
            s = self._session
            if not s or s.phase in ("finished", "cancelled"):
                return
            if s.commit_started:
                return
            s.generation += 1
            s.commit_attempt += 1
            s.phase, s.reason = "cancelled", "shutdown"
            s.preview, s.final, s.final_revision = "", False, None
            s.frames.clear()
            s.samples = 0
            s.speech_pending = False
            if s.wall_timer:
                s.wall_timer.cancel()
        self._emit("cancelled", "shutdown", s)

    def dashboard(self) -> dict | None:
        """Only this explicit local dashboard projection includes transcript text."""
        with self._lock:
            s = self._session
            if not s:
                return None
            return {"session_id": s.session_id, "phase": s.phase, "reason": s.reason,
                    "target": s.target.get("target"), "pane_id": s.target.get("pane_id"),
                    "text": s.preview[:MAX_TEXT_CHARS], "provisional": not s.final,
                    "revision": s.final_revision or s.text_revision,
                    "since": time.time() - max(0.0, time.monotonic() - s.started)}

    def metadata(self) -> dict:
        with self._lock:
            s = self._session
            if not s:
                return {"phase": "idle"}
            return {"session_id": s.session_id, "phase": s.phase, "reason": s.reason,
                    "target": s.target.get("target"), "pane_id": s.target.get("pane_id"),
                    "chars": len(s.preview), "revision": s.final_revision or s.text_revision,
                    "final": s.final, "commit_started": s.commit_started}
