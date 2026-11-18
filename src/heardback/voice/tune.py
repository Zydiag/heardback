"""`heardback-voice tune`: pick thresholds from the enrollment clips (spec §2.4). Never flips speaker_check."""
import wave
from pathlib import Path
from typing import Callable

import numpy as np

from .audio_in import FRAME, RATE, wav_frames
from .config import config_path, load, update
from .enroll import enroll_dir
from .wake import DEBOUNCE_S, PATIENCE, WakeGate

GRID = [round(0.30 + 0.05 * i, 2) for i in range(14)]     # 0.30 … 0.95


def fires(scores: list[float], threshold: float, patience: int = PATIENCE, debounce_s: float = DEBOUNCE_S) -> int:
    g = WakeGate(threshold, patience, debounce_s)
    return sum(g.update(s, i * FRAME / RATE) for i, s in enumerate(scores))


def wake_table(pos: list[list[float]], neg: list[float], neg_hours: float) -> list[tuple[float, float, float]]:
    return [(th, sum(fires(p, th) >= 1 for p in pos) / max(1, len(pos)), fires(neg, th) / neg_hours)
            for th in GRID]


def pick_wake_threshold(pos: list[list[float]], neg: list[float], neg_hours: float,
                        max_fa_per_h: float = 0.5) -> tuple[float, float, float]:
    """(θ, recall, fa_per_h): the lowest θ on the grid with FA/h ≤ max (the top of the grid if none is)."""
    table = wake_table(pos, neg, neg_hours)
    return next((row for row in table if row[2] <= max_fa_per_h), table[-1])


def _read(path: Path) -> np.ndarray:
    return np.concatenate(list(wav_frames(path)))


def clips(*parts: str) -> list[np.ndarray]:
    return [_read(f) for p in parts for f in sorted((enroll_dir() / p).glob("*.wav"))]


def wake_scorer(cfg) -> Callable[[np.ndarray], list[float]] | None:
    from .wake import load_wake
    wake = load_wake(cfg)
    if wake is None:
        return None

    def score(audio: np.ndarray) -> list[float]:
        wake.reset()
        a = np.pad(audio, (0, -len(audio) % FRAME))
        return [wake.score(a[i:i + FRAME]) for i in range(0, len(a), FRAME)]
    return score


def tune_wake(cfg_path: Path, scorer) -> dict | None:
    pos_clips = clips("wake_desk", "wake_room")
    neg_clips = clips("background", "reading", "yes", "no")
    if not pos_clips or not neg_clips:
        print("tune: no enrollment clips; run `heardback-voice enroll` first")
        return None
    neg_audio = np.concatenate(neg_clips)
    neg_hours = len(neg_audio) / RATE / 3600
    pos = [scorer(c) for c in pos_clips]
    neg = scorer(neg_audio)
    print(f"wake: {len(pos)} positive clips, {neg_hours * 60:.1f} min of negatives")
    print("  θ     recall  FA/h")
    for th, rec, fa in wake_table(pos, neg, neg_hours):
        print(f"  {th:.2f}  {rec:5.2f}  {fa:6.2f}")
    th, rec, fa = pick_wake_threshold(pos, neg, neg_hours)
    print(f"wake_threshold = {th:.2f} (recall {rec:.2f}, {fa:.2f} false wakes/h)")
    if rec < 0.9:
        print("recall < 0.9: retrain with n_samples 20000")
    update(cfg_path, wake_threshold=th)
    return {"wake_threshold": th, "recall": rec, "fa_per_h": fa}


SPK_GRID = [round(0.01 * i, 2) for i in range(1, 101)]    # 0.01 … 1.00 (0.0 would read as "not tuned")
MAX_FRR = 0.10


def pick_speaker_threshold(user: list[float], impostor: list[float], max_far: float = 0.01) -> tuple[float, float]:
    """(θ, frr): the lowest θ on a 0.01 grid with mean(impostor ≥ θ) ≤ max_far."""
    imp, usr = np.asarray(impostor), np.asarray(user)
    th = next((t for t in SPK_GRID if (imp >= t).mean() <= max_far), SPK_GRID[-1])
    return th, float((usr < th).mean())


def tune_speaker(cfg_path: Path, embed) -> dict | None:
    from .models import impostors_dir
    from .speaker import build_centroid, cosine, windows
    reading = clips("reading")
    wakes = clips("wake_desk", "wake_room")
    if not reading or not wakes:
        print("tune: no reading/wake clips; skipping the speaker check (run `heardback-voice enroll`)")
        return None
    imp_clips = clips("background") + [_read(f) for f in sorted(impostors_dir().glob("*.wav"))]
    if not any(impostors_dir().glob("*.wav")):
        print("tune: no impostor set; run `heardback-voice setup --impostors` for a real FAR (using background only)")
    win = windows(np.concatenate(reading))          # reading prompts are short: window the whole stream
    centroid = build_centroid([w for w in win[0::2]], embed)   # even windows build it, odd ones are held out
    score = lambda a: cosine(embed(a), centroid)
    tail = int(1.5 * RATE)
    imp15 = [score(w) for c in imp_clips for w in windows(c, 1.5)]
    imp3 = [score(w) for c in imp_clips for w in windows(c, 3.0)]
    th_w, frr_w = pick_speaker_threshold([score(c[-tail:]) for c in wakes], imp15)
    th_u, frr_u = pick_speaker_threshold([score(w) for w in win[1::2]] + [score(c) for c in wakes], imp3)
    yesno = [score(c) for c in clips("yes", "no")]
    frr_yn = float(np.mean([s < th_u for s in yesno])) if yesno else 0.0
    print(f"speaker: centroid from {len(win[0::2])} windows; impostor windows {len(imp15)} × 1.5 s, {len(imp3)} × 3 s")
    print(f"speaker_wake = {th_w:.2f} (FRR {frr_w:.2f}); speaker_utt = {th_u:.2f} (FRR {frr_u:.2f}, "
          f"yes/no FRR {frr_yn:.2f}); FAR ≤ 0.01")
    if max(frr_w, frr_u, frr_yn) > MAX_FRR:
        print("FRR > 0.10: re-enroll with more across-room clips")
    update(cfg_path, speaker_wake=th_w, speaker_utt=th_u)
    print("speaker_check is unchanged: set speaker_check = \"on\" (every turn) or \"wake\" (wake word only) in voice.toml and restart heardback to use it")
    return {"speaker_wake": th_w, "speaker_utt": th_u, "frr_wake": frr_w, "frr_utt": frr_u, "frr_yesno": frr_yn}


def tune(cfg_path: Path | None = None, scorer=None, embed=None) -> dict:
    cfg_path = cfg_path or config_path()
    cfg = load(cfg_path)
    out: dict = {}
    scorer = scorer or wake_scorer(cfg)
    if scorer is None:
        print("tune: no wake model (hey_claude.onnx, plan 04 Task 18); skipping the wake threshold")
    elif (r := tune_wake(cfg_path, scorer)) is not None:
        out.update(r)
    if embed is None:
        from .speaker import SpeakerCheck
        embed = SpeakerCheck(None).embed
    if (r := tune_speaker(cfg_path, embed)) is not None:
        out.update(r)
    return out
