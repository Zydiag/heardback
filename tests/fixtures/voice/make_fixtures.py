"""Regenerate the committed voice fixtures (needs the voice extra + `heardback-voice setup`).

uv run --extra voice python tests/fixtures/voice/make_fixtures.py
"""
import json
import wave
from pathlib import Path

import numpy as np
import torch
import torchaudio

from heardback.voice.tts import load_kokoro

HERE = Path(__file__).parent
CMD = "Switch to tmux window two."


def to16k(a: np.ndarray) -> np.ndarray:
    return torchaudio.functional.resample(torch.from_numpy(a), 24000, 16000).numpy()


def trim(a: np.ndarray) -> np.ndarray:
    """Cut Kokoro's lead/tail padding: keep 10 ms windows above 2% of the peak RMS."""
    w = a[: len(a) // 160 * 160].reshape(-1, 160)
    rms = np.sqrt((w ** 2).mean(axis=1))
    on = np.flatnonzero(rms > 0.02 * rms.max())
    return a[on[0] * 160:(on[-1] + 1) * 160]


def write(name: str, a: np.ndarray) -> None:
    with wave.open(str(HERE / name), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes((np.clip(a, -1, 1) * 32767).astype(np.int16).tobytes())


def main():
    heart, george = load_kokoro("af_heart", "cuda"), load_kokoro("bm_george", "cuda")
    sil = lambda s: np.zeros(int(s * 16000), np.float32)
    meta = {}
    for name, synth, text, lead in [("cmd.wav", heart, CMD, 0.5), ("yes.wav", heart, "Yeah, do it.", 0.3),
                                    ("maybe.wav", heart, "Maybe later.", 0.3), ("other.wav", george, CMD, 0.3)]:
        speech = trim(to16k(synth(text)))
        write(name, np.concatenate([sil(lead), speech, sil(1.5)]))
        meta[name] = {"text": text, "speech_start_s": lead, "speech_end_s": round(lead + len(speech) / 16000, 3)}
    noise = np.random.default_rng(0).standard_normal(6 * 16000).astype(np.float32) * 0.003
    write("noise.wav", noise)
    meta["noise.wav"] = {"text": "", "speech_start_s": None, "speech_end_s": None}
    (HERE / "fixtures.json").write_text(json.dumps(meta, indent=1) + "\n")


if __name__ == "__main__":
    main()
