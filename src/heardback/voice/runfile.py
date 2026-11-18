"""`heardback-voice run-file`: push a wav through (wake →) endpoint → STT → the brain socket, printing replies."""
import asyncio
import os
from pathlib import Path

import numpy as np

from .audio_in import FRAME, RATE, wav_frames
from .config import load
from .link import Link
from .loop import STUCK_S, Voice


class PrintTts:
    speaking, first_audio_at = False, None

    async def say(self, text):
        print(f"say: {text}", flush=True)

    async def finish(self):
        pass

    def stop(self):
        pass


class Quiet:
    """Cues and media for run-file: never makes a sound or pauses a player."""
    def play(self, name): pass
    def notify(self, text, sticky=False): pass
    def pause(self, turn_id=None): pass
    def keep(self): pass
    def resume(self, turn_id=None): pass


def default_sock() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "heardback.sock"


async def run_file(wav: Path, sock: Path, use_wake: bool = False) -> int:
    from .endpoint import silero
    from .stt import Stt, load_catalogue, local_words
    from .text import hotwords
    from .wake import load_wake
    cfg = load()
    frames = list(wav_frames(wav))           # format errors before loading any model
    wake = None
    if use_wake and (wake := load_wake(cfg)) is None:
        print("run-file: no wake model (train hey_claude.onnx, plan 04 Task 18)")
        return 1
    stt = Stt(cfg, hotwords(load_catalogue(), cfg.stt_hotwords, local_words()))
    v = Voice(cfg, Link(sock), stt=stt, tts=PrintTts(), cues=Quiet(), media=Quiet(), prob=silero(), wake=wake)
    task = asyncio.create_task(v.run())
    try:
        for _ in range(100):
            if v.link.up:
                break
            await asyncio.sleep(0.02)
        else:
            print(f"run-file: brain socket {sock} is not up")
            return 1
        if wake is None:
            v.ptt()
        tail = [np.zeros(FRAME, np.int16)] * int(5 * RATE / FRAME)
        for f in frames + tail:
            v.feed(f)
            await asyncio.sleep(FRAME / RATE)
        for _ in range(int(STUCK_S / 0.05)):
            if v.state == "IDLE":
                break
            await asyncio.sleep(0.05)
        if wake is not None:
            print(f"wake fired: {v.turn > 0}")   # until speaker check is on, the brain drops wake turns
        return 0 if v.utterances else 1
    finally:
        task.cancel()
