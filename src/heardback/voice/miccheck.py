"""Compare configured mic modes: record 20 commands, transcribe, report word error rate."""
import json
import threading
import time

import numpy as np

from .config import data_dir, load
from .devices import pactl_json, pick_mic
from .text import norm

PROMPTS = [
    "switch to tmux window three", "close unused tabs in zen", "what are my claude sessions doing",
    "open zen", "next wallpaper", "go to workspace four", "is anything waiting for me",
    "type yes in window two", "mute the announcements", "switch to tmux window one",
    "move this window to workspace two", "ask claude what the weather is like", "set a dark wallpaper",
    "which session needs permission", "focus the terminal", "summarise my sessions",
    "unmute the announcements", "go to the next tmux window", "pin this tab", "close the current window",
]


def wer(ref: str, hyp: str) -> float:
    r, h = norm(ref).split(), norm(hyp).split()
    d = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        prev, d[0] = d[0], i
        for j, hw in enumerate(h, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (rw != hw))
    return d[len(h)] / max(1, len(r))


def pick_node(mic: str) -> str | None:
    try:
        node, fell_back = pick_mic(pactl_json("list", "sources"), mic, "auto")
    except ValueError:
        return None
    return None if fell_back else node


def record(node: str, wait_s: float = 6.0, max_s: float = 15.0) -> np.ndarray:
    from .audio_in import MicSource
    from .endpoint import Endpointer, silero
    ep = Endpointer(silero(), wait_s=wait_s, max_s=max_s)
    done = threading.Event()

    def on_frame(f):
        if ep.feed(f) in ("done", "timeout"):
            done.set()
    mic = MicSource(node, on_frame, lambda n: None)
    mic.start()
    done.wait(wait_s + max_s + 5)
    mic.stop()
    return ep.audio


def run(mic: str, distance: str) -> int:
    from .enroll import write_wav
    from .stt import Stt
    node = pick_node(mic)
    if not node:
        print(f"miccheck: no source for {mic}; connect the configured mic")
        return 1
    out = data_dir() / "voice" / "miccheck" / f"{mic}-{distance}"
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    stt = Stt(load())
    rows = []
    print(f"mic {node}: read each prompt after pressing Enter")
    for i, p in enumerate(PROMPTS, 1):
        input(f"{i:2}/20  {p!r}  [Enter, then speak] ")
        audio = record(node)
        write_wav(out / f"{i:02}.wav", audio)
        hyp = stt.transcribe(audio) if len(audio) else ""
        rows.append(wer(p, hyp))
        print(f"      heard {hyp!r}  WER {rows[-1]:.2f}")
    mean = sum(rows) / len(rows)
    print(f"mean WER {mic}/{distance}: {mean:.3f}")
    with open(data_dir() / "voice" / "miccheck" / "results.jsonl", "a") as f:
        f.write(json.dumps({"t": time.time(), "mic": mic, "node": node, "distance": distance,
                            "mean_wer": round(mean, 4), "wer": [round(x, 3) for x in rows]}) + "\n")
    return 0
