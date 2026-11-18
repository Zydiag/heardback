"""`heardback-voice enroll`: record the wake, reading, yes/no and background clips for `tune` (spec §2.4)."""
import threading
import wave
from pathlib import Path

import numpy as np

from .audio_in import RATE, MicSource
from .config import data_dir, load
from .devices import pactl_json, pick_mic
from .endpoint import Endpointer, silero
from .miccheck import PROMPTS

PARTS = ["wake_desk", "wake_room", "reading", "yesno", "background"]
TALK = [
    "I think I'll make some tea before the next meeting starts.",
    "The build was slow today, probably because of the big test suite.",
    "Remind me to call my sister about the weekend plans.",
    "That video about language models was longer than I expected.",
    "Let's leave the rest of this for tomorrow morning.",
]
YES_SAY = ["yes", "yeah", "yep", "do it", "go ahead", "sure", "okay", "yes do it", "confirm", "yeah go ahead"]
NO_SAY = ["no", "nope", "cancel", "stop", "don't", "abort", "no thanks", "no cancel", "no", "nope"]
BACKGROUND_FILES, BACKGROUND_S = 30, 60.0


def enroll_dir() -> Path:
    return data_dir() / "voice" / "enroll"


def _mkdir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    p.chmod(0o700)
    return p


def write_wav(path: Path, audio: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(audio.astype(np.int16).tobytes())


def record_clip(node: str, seconds: float | None, wait_s: float = 6.0) -> np.ndarray:
    """`seconds` of audio, or (None) one endpointed utterance."""
    ep = Endpointer(silero(), wait_s=wait_s) if seconds is None else None
    got: list[np.ndarray] = []
    need = int((seconds or 0) * RATE)
    done = threading.Event()

    def on_frame(f):
        if ep is not None:
            if ep.feed(f) in ("done", "timeout"):
                done.set()
        else:
            got.append(f)
            if sum(map(len, got)) >= need:
                done.set()
    mic = MicSource(node, on_frame, lambda n: None)
    mic.start()
    try:
        while not done.wait(0.2):     # short waits so Ctrl-C gets through
            pass
    finally:
        mic.stop()
    return ep.audio if ep is not None else np.concatenate(got)[:need]


def _ready(d: Path) -> bool:
    """True to record into `d`: empty, or the user agrees to overwrite."""
    old = sorted(d.glob("*.wav")) if d.exists() else []
    if not old:
        return True
    if input(f"{d.name}: {len(old)} clips already there. Overwrite? [y/N] ").strip().lower() not in ("y", "yes"):
        return False
    for f in old:
        f.unlink()
    return True


def _prompted(node: str, d: Path, prompts: list[str], wait_s: float = 6.0) -> None:
    _mkdir(d)
    for i, p in enumerate(prompts, 1):
        while True:
            input(f"{d.name} {i:2}/{len(prompts)}  say {p!r}  [Enter, then speak] ")
            audio = record_clip(node, None, wait_s)
            if len(audio):
                break
            print("      heard nothing, again")
        write_wav(d / f"{i:02}.wav", audio)


def _background(node: str, d: Path) -> None:
    _mkdir(d)
    print(f"background: {BACKGROUND_FILES} × {BACKGROUND_S:.0f} s with TV/YouTube on (Claude/AI videos are best). "
          "Ctrl-C stops and keeps the finished files.")
    try:
        for i in range(1, BACKGROUND_FILES + 1):
            write_wav(d / f"{i:02}.wav", record_clip(node, BACKGROUND_S))
            print(f"      {i}/{BACKGROUND_FILES} saved", flush=True)
    except KeyboardInterrupt:
        print("\nbackground: stopped, finished files kept")


def run(parts: list[str] | None = None, node: str | None = None) -> int:
    if node is None:
        cfg = load()
        try:
            node, _ = pick_mic(pactl_json("list", "sources"), cfg.mic, cfg.mic_node)
        except ValueError:
            print(f"enroll: no mic for mode {cfg.mic}")
            return 1
    root = enroll_dir()
    _mkdir(root.parent)
    _mkdir(root)
    print(f"enroll on {node}. Turn the heardback mic off first (Super+Shift+C) so it doesn't answer you.")
    for part in parts or PARTS:
        if part == "wake_desk" and _ready(root / part):
            _prompted(node, root / part, ["hey claude"] * 20)
        elif part == "wake_room" and _ready(root / part):
            print("wake_room: stand about 3 m away; play music for 10 of the 20")
            _prompted(node, root / part, ["hey claude"] * 10 + ["hey claude (music on)"] * 10, wait_s=10.0)
        elif part == "reading" and _ready(root / part):
            _prompted(node, root / part, PROMPTS + TALK)
        elif part == "yesno":
            if _ready(root / "yes") and _ready(root / "no"):
                _prompted(node, root / "yes", YES_SAY)
                _prompted(node, root / "no", NO_SAY)
        elif part == "background" and _ready(root / part):
            _background(node, root / part)
    return 0
