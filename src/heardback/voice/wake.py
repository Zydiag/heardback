"""Wake word: openWakeWord scores per 80 ms frame, `WakeGate` (patience + debounce), a 2 s ring buffer."""
from collections import deque
from pathlib import Path

import numpy as np

from .audio_in import FRAME, RATE
from .config import Config, data_dir, log

PATIENCE = 2
DEBOUNCE_S = 2.0
BARGE_BOOST = 0.2
RING_S = 2.0


class WakeGate:
    """Fires when the last `patience` scores are all ≥ threshold + boost, at most once per `debounce_s`.
    Done here on raw scores: openWakeWord's predict() refuses patience and debounce_time together."""
    def __init__(self, threshold: float, patience: int = PATIENCE, debounce_s: float = DEBOUNCE_S):
        self.threshold, self.debounce_s = threshold, debounce_s
        self._last: deque[float] = deque(maxlen=patience)
        self._fired: float | None = None

    def update(self, score: float, t: float, boost: float = 0.0) -> bool:
        self._last.append(score)
        if len(self._last) < self._last.maxlen or min(self._last) < self.threshold + boost:
            return False
        if self._fired is not None and t - self._fired < self.debounce_s:
            return False
        self._fired = t
        self._last.clear()
        return True


class Wake:
    def __init__(self, model_path: Path, threshold: float, model=None):
        self.threshold = threshold
        self.key = model_path.stem               # openWakeWord keys scores by the model file's stem
        if model is None:
            from openwakeword.model import Model
            model = Model(wakeword_model_paths=[str(model_path)], vad_threshold=0.5)
        self.model = model
        self.ring: deque[np.ndarray] = deque(maxlen=int(RING_S * RATE / FRAME))

    def score(self, frame: np.ndarray) -> float:
        self.ring.append(frame)
        return float(self.model.predict(frame)[self.key])

    def ring_audio(self, seconds: float) -> np.ndarray:
        if not self.ring:
            return np.zeros(0, np.int16)
        return np.concatenate(self.ring)[-int(seconds * RATE):]

    def reset(self) -> None:
        self.model.reset()


def wake_path() -> Path:
    return data_dir() / "models" / "hey_claude.onnx"


def load_wake(cfg: Config) -> Wake | None:
    path = wake_path()
    if not path.exists():
        log("wake_missing", path=str(path), mode="push-to-talk", hint="Send SIGUSR1 to heardbackd to speak")
        return None
    return Wake(path, cfg.wake_threshold)
