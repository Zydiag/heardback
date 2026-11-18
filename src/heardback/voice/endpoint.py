"""Endpointing: VAD over 512-sample chunks → waiting / speech / done / timeout (stream time)."""
import math
import time
from collections import deque
from typing import Callable

import numpy as np

CHUNK = 512
RATE = 16000


def _chunks(seconds: float) -> int:
    return max(1, math.ceil(round(seconds * RATE / CHUNK, 6)))


class Endpointer:
    def __init__(self, prob: Callable[[np.ndarray], float], *, end_silence: float = 0.7, wait_s: float | None = 4.0,
                 max_s: float = 15.0, min_burst_s: float = 0.25, preroll_s: float = 0.3, threshold: float = 0.5,
                 reset: bool = True, soft_after_s: float | None = None, soft_silence: float = 0.25,
                 short_silence: float | None = None, short_s: float = 2.0):
        self.prob, self.threshold = prob, threshold
        self.deadline = time.monotonic() + (wait_s or 0) + max_s
        if reset:   # Silero's RNN state: fresh for every listen / confirm listen (not for a continuation)
            getattr(prob, "reset", lambda: None)()
        self._end = _chunks(end_silence)
        # Under short_s of speech (a quick command), end after short_silence instead (user, 2026-11-17).
        self._short = None if short_silence is None else (_chunks(short_silence), _chunks(short_s))
        self._wait = None if wait_s is None else _chunks(wait_s)
        self._max = _chunks(max_s)
        self._soft_after = None if soft_after_s is None else _chunks(soft_after_s)
        self._soft_end = _chunks(soft_silence)
        self._burst = _chunks(min_burst_s)
        self._pre: deque = deque(maxlen=int(preroll_s * RATE / CHUNK))
        self._run: list[np.ndarray] = []
        self._parts: list[np.ndarray] = []
        self._rest = np.zeros(0, np.int16)
        self._n = 0                 # chunks seen
        self._start: int | None = None
        self.speech_start_s: float | None = None
        self._silence = 0
        self._probs: list[float] = []
        self._run_probs: list[float] = []
        self.status = "waiting"
        self.reason: str | None = None
        self.speech_end_s: float | None = None

    @property
    def audio(self) -> np.ndarray:
        return np.concatenate(self._parts) if self._parts else np.zeros(0, np.int16)

    def feed(self, frame: np.ndarray) -> str:
        if self.status in ("done", "timeout"):
            return self.status
        buf = np.concatenate([self._rest, frame])
        k = len(buf) // CHUNK * CHUNK
        self._rest = buf[k:]
        for i in range(0, k, CHUNK):
            self._chunk(buf[i:i + CHUNK])
            if self.status in ("done", "timeout"):
                # Preserve the unprocessed tail from this frame as well as the
                # sub-512-sample remainder for a caller continuing the stream.
                self._rest = buf[i + CHUNK:].copy()
                break
        return self.status

    def take_rest(self) -> np.ndarray:
        """Return all buffered samples not inspected by VAD."""
        rest, self._rest = self._rest, np.zeros(0, np.int16)
        return rest

    def _chunk(self, c: np.ndarray) -> None:
        p = self.prob(c)
        self._n += 1
        loud = p >= self.threshold
        if self._start is None:
            if loud:
                self._run.append(c)
                self._run_probs.append(p)
                if len(self._run) >= self._burst:
                    self._start = self._n - len(self._run)
                    self.speech_start_s = self._start * CHUNK / RATE
                    self._parts = [*self._pre, *self._run]
                    self._probs = self._run_probs.copy()
                    self.speech_end_s = self._n * CHUNK / RATE
                    self.status = "speech"
            else:
                self._pre.extend(self._run)  # a short burst is just preroll context
                self._run = []
                self._run_probs = []
                self._pre.append(c)
                if self._wait is not None and self._n >= self._wait:
                    self.status = "timeout"
                    self.reason = "no_speech_timeout"
            return
        self._probs.append(p)
        self._parts.append(c)
        if loud:
            self._silence = 0
            self.speech_end_s = self._n * CHUNK / RATE
        else:
            self._silence += 1
        # Resolve a simultaneous boundary in favor of the hard cap so callers
        # never mistake a truncated utterance for a silence-complete phrase.
        if self._n - self._start >= self._max:
            self.status = "done"
            self.reason = "max_duration"
        elif self._silence >= (self._soft_end if self._soft_after is not None and self._n - self._start >= self._soft_after
                               else self._short[0] if self._short and self._n - self._start - self._silence < self._short[1]
                               else self._end):
            self.status = "done"
            soft = self._soft_after is not None and self._n - self._start >= self._soft_after
            self.reason = "soft_cap" if soft else "silence"

    @property
    def probs(self) -> list[float]:
        return self._probs

    def continue_scored(self, units: list[tuple[np.ndarray, float]]) -> None:
        """Continue an open utterance from units already passed through VAD."""
        self._parts = [audio.copy() for audio, _ in units]
        self._probs = [p for _, p in units]
        self._run = []
        self._run_probs = []
        self._pre.clear()
        self._n = len(units)
        self._start = 0 if units else None
        self.speech_start_s = 0.0 if units else None
        self._silence = 0
        for _, p in reversed(units):
            if p >= self.threshold:
                break
            self._silence += 1
        voiced = [i for i, (_, p) in enumerate(units) if p >= self.threshold]
        self.speech_end_s = (voiced[-1] + 1) * CHUNK / RATE if voiced else None
        self.status = "speech" if units else "waiting"
        self.reason = None


_silero_model = None


def silero() -> Callable[[np.ndarray], float]:
    global _silero_model
    if _silero_model is None:
        from silero_vad import load_silero_vad
        _silero_model = load_silero_vad(onnx=True)
    import torch
    model = _silero_model
    model.reset_states()

    def prob(chunk: np.ndarray) -> float:
        return float(model(torch.from_numpy(chunk.astype(np.float32) / 32768), RATE).item())
    prob.reset = model.reset_states
    return prob
