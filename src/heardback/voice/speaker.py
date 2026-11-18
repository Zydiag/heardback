"""ECAPA speaker check: embed, centroid, cosine (spec §2.4)."""
from pathlib import Path

import numpy as np

from .audio_in import RATE
from .config import Config, data_dir, log


def centroid_path() -> Path:
    return data_dir() / "voice" / "centroid.npy"


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))


def windows(audio: np.ndarray, seconds: float = 3.0) -> list[np.ndarray]:
    """Non-overlapping windows; a tail under half a window is dropped."""
    n = int(seconds * RATE)
    out = [audio[i:i + n] for i in range(0, len(audio), n)]
    return [w for w in out if len(w) >= n / 2]


class SpeakerCheck:
    def __init__(self, centroid: np.ndarray | None, device: str = "cpu", embed=None):
        self.centroid, self.device = centroid, device
        self._embed, self._model = embed, None

    def embed(self, audio: np.ndarray) -> np.ndarray:
        if self._embed:
            return self._embed(audio)
        import torch
        if self._model is None:
            from speechbrain.inference.speaker import EncoderClassifier
            from .models import REPOS, snapshot
            snap = str(snapshot(REPOS["ecapa"]))
            # the yaml's pretrained_path is the Hub id: point it at the pinned snapshot (offline)
            self._model = EncoderClassifier.from_hparams(
                source=snap, savedir=str(data_dir() / "models" / "ecapa"),
                overrides={"pretrained_path": snap}, run_opts={"device": self.device})
        x = torch.from_numpy(audio.astype(np.float32) / 32768.0).unsqueeze(0)
        with torch.no_grad():
            return self._model.encode_batch(x).squeeze().cpu().numpy()

    def score(self, audio: np.ndarray) -> float:
        return cosine(self.embed(audio), self.centroid)


def build_centroid(clips: list[np.ndarray], embed, save: bool = True) -> np.ndarray:
    """Mean of the 3 s window embeddings, L2-normalized; saved as <data>/voice/centroid.npy."""
    embs = [embed(w) for c in clips for w in windows(c)]
    if not embs:
        raise ValueError("no clip is long enough for a 1.5 s window")
    c = np.mean(embs, axis=0)
    c = c / np.linalg.norm(c)
    if save:
        centroid_path().parent.mkdir(parents=True, exist_ok=True)
        np.save(centroid_path(), c)
    return c


def load_speaker(cfg: Config, embed=None) -> SpeakerCheck | None:
    if cfg.speaker_check == "off":
        return None
    if not centroid_path().exists():
        log("speaker_check_off", reason=f"no {centroid_path()}: run `heardback-voice enroll` (and `tune`); "
                                        "running with the speaker check off")
        return None
    return SpeakerCheck(np.load(centroid_path()), embed=embed)
