"""Mic frames: from a wav file (tests, run-file) or a `pw-record` child with a stall watchdog."""
import os
import select
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Callable, Iterator

import numpy as np

RATE = 16000
FRAME = 1280          # 80 ms
WATCHDOG_S = 2.0      # no bytes for this long = stalled (PipeWire restart, suspend)
HEALTHY_S = 10.0     # brief bursts before another failure must not reset the backoff


def wav_frames(path: Path) -> Iterator[np.ndarray]:
    with wave.open(str(path)) as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (RATE, 1, 2):
            raise ValueError(f"{path}: need 16 kHz mono 16-bit wav; convert with "
                             f"ffmpeg -i in -ar 16000 -ac 1 -sample_fmt s16 out.wav")
        a = np.frombuffer(w.readframes(w.getnframes()), np.int16)
    a = np.pad(a, (0, -len(a) % FRAME))
    for i in range(0, len(a), FRAME):
        yield a[i:i + FRAME].copy()


def backoff(n: int) -> float:
    return min(30, 2 ** min(n, 5))


class MicSource:
    """Runs `pw-record` in a reader thread; on_frame/on_fail/on_restart are called from that thread."""

    def __init__(self, node: str, on_frame: Callable[[np.ndarray], None], on_fail: Callable[[int], None],
                 on_restart: Callable[[], None] = lambda: None):
        self.node, self.on_frame, self.on_fail, self.on_restart = node, on_frame, on_fail, on_restart
        self._stopping = False
        self._retargeted = False
        self._wake = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._fails = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stopping = False
        self._wake.clear()
        self._thread = threading.Thread(target=self._run, name="heardback-mic", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        self._kill()
        if self._thread:
            self._thread.join(timeout=5)

    def retarget(self, node: str) -> None:
        self.node = node
        self._retargeted = True
        self._kill()

    def _kill(self) -> None:
        p = self._proc
        if p and p.poll() is None:
            p.kill()

    def _sleep(self, s: float) -> None:
        self._wake.wait(s)

    def _argv(self) -> list[str]:
        return ["pw-record", "--target", self.node, "--rate", str(RATE), "--channels", "1",
                "--format", "s16", "--raw", "--latency", "20ms", "-P", "{ node.name = heardback-mic }", "-"]

    def _run(self) -> None:
        size = FRAME * 2
        while not self._stopping:
            self._retargeted = False
            try:
                p = self._proc = subprocess.Popen(self._argv(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except OSError:
                p = None
            buf = b""
            started = time.monotonic()
            while p and not self._stopping:
                r, _, _ = select.select([p.stdout], [], [], WATCHDOG_S)
                if not r:
                    break                                   # stalled
                chunk = os.read(p.stdout.fileno(), size * 4)
                if not chunk:
                    break                                   # exited
                buf += chunk
                while len(buf) >= size:
                    self.on_frame(np.frombuffer(buf[:size], np.int16).copy())
                    buf = buf[size:]
                    if time.monotonic() - started >= HEALTHY_S:
                        self._fails = 0
            if p:
                p.kill(); p.wait(); p.stdout.close()
            if self._stopping or self._retargeted:
                continue                                    # retarget = immediate respawn
            self._fails += 1
            if self._fails >= 3:
                self.on_fail(self._fails)
            self.on_restart()
            self._sleep(backoff(self._fails))
