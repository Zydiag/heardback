"""faster-whisper with a CUDA → CUDA int8 → CPU distil fallback chain."""
import json
import subprocess
from pathlib import Path

import numpy as np

from .config import Config, log
from .models import REPOS, snapshot
from .text import clean, clean_dictation

CHAIN = [("stt", "cuda", "float16"), ("stt", "cuda", "int8_float16"), ("stt_cpu", "cpu", "int8")]


def load_catalogue() -> dict | None:
    try:
        r = subprocess.run(["jv", "list", "--json"], capture_output=True, text=True, timeout=3)
        return json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def local_words() -> list[str]:
    """Project folder names under the configured project roots + installed app names of one or two words."""
    from jv.hypr import desktop_apps
    from heardback.config import load as desktop_config
    words = []
    for r in desktop_config().project_roots:
        try:
            words += sorted(c.name for c in Path(r).expanduser().iterdir() if c.is_dir() and not c.name.startswith("."))
        except OSError:
            pass
    return words + [a for a in desktop_apps() if len(a.split()) <= 2]


class Stt:
    def __init__(self, cfg: Config, hotwords: str = ""):
        from faster_whisper import WhisperModel
        self.hotwords = hotwords or None
        chain = CHAIN[2:] if cfg.stt_device == "cpu" else CHAIN
        if chain[0][1] == "cuda":
            try:
                import torch  # noqa: F401  loads the pip CUDA libs (libcublas.so.12) ctranslate2 needs
            except ImportError:
                pass
        for i, (repo, device, compute) in enumerate(chain):
            try:
                self.model = WhisperModel(str(snapshot(REPOS[repo])), device=device, compute_type=compute)
                self.device, self.fell_back, self.repo = device, i > 0, REPOS[repo]
                self.transcribe(np.zeros(16000, np.int16))
                return
            except Exception as e:  # CUDA init / OOM / missing kernels: try the next one
                log("stt_load_failed", device=device, compute=compute, error=str(e)[:200])
        raise RuntimeError("no STT model could load")

    def _segments(self, audio: np.ndarray):
        return self.model.transcribe(
            audio.astype(np.float32) / 32768, language="en", beam_size=1, condition_on_previous_text=False,
            without_timestamps=True, vad_filter=False, hotwords=self.hotwords)

    def transcribe(self, audio: np.ndarray) -> str:
        segs, _ = self._segments(audio)
        return clean(segs)

    def transcribe_dictation(self, audio: np.ndarray) -> str:
        segs, _ = self._segments(audio)
        return clean_dictation(segs)
