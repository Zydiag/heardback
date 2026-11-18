"""Pure VAD-based speech chunking for continuous dictation."""
from dataclasses import dataclass
from typing import Callable

import numpy as np

from heardback.voice.endpoint import CHUNK, RATE, Endpointer


@dataclass(frozen=True)
class Chunk:
    seq: int
    audio: np.ndarray
    reason: str
    voiced_s: float
    speech_end_s: float


class Chunker:
    def __init__(self, prob: Callable[[np.ndarray], float], *, silence_s: float = 0.6,
                 soft_cap_s: float = 8.0, soft_silence_s: float = 0.25,
                 hard_cap_s: float = 20.0, min_burst_s: float = 0.15,
                 min_voiced_s: float = 0.3):
        self.prob = prob
        self.silence_s, self.soft_cap_s = silence_s, soft_cap_s
        self.soft_silence_s, self.hard_cap_s = soft_silence_s, hard_cap_s
        self.min_burst_s, self.min_voiced_s = min_burst_s, min_voiced_s
        self.seq = 0
        self.stream_s = 0.0
        self._ep = self._new_endpoint()
        self._ep_origin_s = 0.0
        self._carry: list[tuple[np.ndarray, float]] = []
        self._pending = np.zeros(0, np.int16)
        self._closed = False

    def _new_endpoint(self) -> Endpointer:
        return Endpointer(self.prob, end_silence=self.silence_s, wait_s=None,
                          max_s=self.hard_cap_s, min_burst_s=self.min_burst_s,
                          preroll_s=0.0, reset=False, soft_after_s=self.soft_cap_s,
                          soft_silence=self.soft_silence_s)

    def feed(self, frame: np.ndarray) -> list[Chunk]:
        if self._closed:
            return []
        out = []
        # Feeding one VAD unit at a time makes boundary ownership explicit;
        # the public frame's remainder is always visited in order.
        buf = np.concatenate([self._pending, frame])
        k = len(buf) // CHUNK * CHUNK
        self._pending = buf[k:].copy()
        for i in range(0, k, CHUNK):
            part = buf[i:i + CHUNK]
            self.stream_s += CHUNK / RATE
            status = self._ep.feed(part)
            if status == "done":
                chunk = self._close(self._ep.reason or "silence")
                if chunk:
                    out.append(chunk)
        return out

    def _close(self, reason: str) -> Chunk | None:
        ep = self._ep
        audio = ep.audio
        probs = ep.probs
        if reason == "max_duration" or reason == "hard_cap":
            reason = "hard_cap"
            # ceil ensures the inclusive search window covers at least 2 s.
            window = int(np.ceil(2 * RATE / CHUNK))
            candidates = list(range(max(0, len(probs) - window), len(probs)))
            lowest = min((probs[i] for i in candidates), default=0.0)
            cut = max((i for i in candidates if probs[i] == lowest), default=len(probs) - 1)
            cut_sample = min(len(audio), max(0, (cut + 1) * CHUNK))
            audio = audio[:cut_sample]
            cut_units = min(len(probs), cut + 1)
            suffix = ep.audio[cut_sample:].reshape(-1, CHUNK)
            self._carry = [(unit.copy(), p) for unit, p in zip(suffix, probs[cut_units:])]
            probs = probs[:cut_units]
        voiced_idxs = [i for i, p in enumerate(probs) if p >= ep.threshold]
        voiced_s = len(voiced_idxs) * CHUNK / RATE
        speech_start = ep.speech_start_s or 0.0
        speech_end = self._ep_origin_s + speech_start + (voiced_idxs[-1] + 1) * CHUNK / RATE if voiced_idxs else self._ep_origin_s
        result = None
        if voiced_s >= self.min_voiced_s and audio.size:
            result = Chunk(self.seq, audio.copy(), reason, voiced_s, speech_end)
            self.seq += 1
        previous_origin = self._ep_origin_s
        self._ep = self._new_endpoint()
        if self._carry:
            self._ep_origin_s = previous_origin + speech_start + (len(probs) * CHUNK / RATE)
            self._ep.continue_scored(self._carry)
            self._carry = []
        else:
            self._ep_origin_s = self.stream_s
        return result

    def flush(self) -> Chunk | None:
        if self._closed:
            return None
        self._closed = True
        ep = self._ep
        audio = ep.audio
        if self._pending.size:
            audio = np.concatenate([audio, self._pending])
            self._pending = np.zeros(0, np.int16)
        voiced_idxs = [i for i, p in enumerate(ep.probs) if p >= ep.threshold]
        voiced_s = len(voiced_idxs) * CHUNK / RATE
        if voiced_s < self.min_voiced_s or not audio.size:
            return None
        result = Chunk(self.seq, audio.copy(), "stop", voiced_s,
                       self._ep_origin_s + (ep.speech_start_s or 0.0) + (voiced_idxs[-1] + 1) * CHUNK / RATE)
        self.seq += 1
        return result
