"""Chime tones, raw playback through `pw-play`, desktop notifications."""
import subprocess
import threading

import numpy as np

OUT_RATE = 24000
PW_PLAY = ["pw-play", "--raw", "--rate", str(OUT_RATE), "--channels", "1", "--format", "f32", "-"]


def tone(f0: float, f1: float, ms: int, vol: float = 0.25) -> np.ndarray:
    n = OUT_RATE * ms // 1000
    freq = np.linspace(f0, f1, n)
    x = vol * np.sin(2 * np.pi * np.cumsum(freq) / OUT_RATE)
    fade = min(n // 2, OUT_RATE // 100)  # 10 ms
    ramp = np.linspace(0.0, 1.0, fade)
    x[:fade] *= ramp
    x[-fade:] *= ramp[::-1]
    return x.astype(np.float32)


def _gap(ms: int) -> np.ndarray:
    return np.zeros(OUT_RATE * ms // 1000, np.float32)


CUES = {
    "wake": tone(880, 1320, 80),
    "tick": tone(1000, 1000, 40),
    "nothing": tone(660, 440, 100, vol=0.1),
    "error": np.concatenate([tone(330, 330, 80), _gap(40), tone(330, 330, 80)]),
    "ready": tone(660, 990, 120),
}


def play_pcm(pcm: np.ndarray) -> None:
    subprocess.run(PW_PLAY, input=pcm.astype(np.float32).tobytes(),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Cues:
    def __init__(self, notify: bool):
        self._notify = notify

    def play(self, name: str) -> None:
        threading.Thread(target=self._play, args=(name,), daemon=True).start()

    def _play(self, name: str) -> None:
        try:
            play_pcm(CUES[name])
        except OSError:
            pass

    def notify(self, text: str, sticky: bool = False) -> None:
        """sticky: never expires (a question waiting for its answer); the next note replaces it."""
        if not self._notify:
            return
        try:
            subprocess.run(["notify-send", "-a", "heardback", *(["-t", "0"] if sticky else []), "-h",
                            "string:x-canonical-private-synchronous:heardback", "heardback", text],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass
