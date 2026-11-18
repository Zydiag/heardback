"""Kokoro synthesis and one `pw-play` per turn: sentence N+1 is synthesized while N plays."""
import asyncio
import functools
import re
import time
from concurrent.futures import Executor, ThreadPoolExecutor
from typing import Callable

import numpy as np

from .cues import PW_PLAY
from .models import REPOS, snapshot
from .text import split_sentences

_END = object()
# Kokoro says nothing for a leading "!": a read-back of "!ls" came out as "ls" and the user declined (live 10:06:45).
_BANG = re.compile(r"(?<![\w!])!(?=\w)")


def load_kokoro(voice: str, device: str) -> Callable[[str], np.ndarray]:
    from kokoro import KModel, KPipeline
    snap = snapshot(REPOS["tts"])
    # Explicit files: KPipeline's own hf_hub_download asks for revision "main", not our pinned snapshot.
    model = KModel(repo_id=REPOS["tts"], config=str(snap / "config.json"),
                   model=str(snap / "kokoro-v1_0.pth")).to(device).eval()
    pipe = KPipeline(lang_code=voice[0], repo_id=REPOS["tts"], model=model)
    vpath = str(snap / "voices" / f"{voice}.pt")

    def synth(text: str) -> np.ndarray:
        parts = [np.asarray(r.audio.cpu() if hasattr(r.audio, "cpu") else r.audio, np.float32)
                 for r in pipe(text, voice=vpath) if r.audio is not None]
        return np.concatenate(parts) if parts else np.zeros(0, np.float32)
    return synth


def louder(synth: Callable[[str], np.ndarray], volume: float) -> Callable[[str], np.ndarray]:
    """Kokoro peaks ~0.33, about 10 dB under the chimes; clip so a high gain can't wrap."""
    return lambda text: np.clip(synth(text) * volume, -1.0, 1.0).astype(np.float32)


def cached(synth: Callable[[str], np.ndarray]) -> Callable[[str], np.ndarray]:
    """Replies of ≤3 words ("Done.", "On it.") are synthesized once."""
    short = functools.lru_cache(maxsize=64)(synth)
    return lambda text: short(text) if len(text.split()) <= 3 else synth(text)


class Tts:
    def __init__(self, voice: str, device: str = "cuda", synth: Callable[[str], np.ndarray] | None = None,
                 *, pool: Executor | None = None, volume: float = 1.0):
        self.synth = synth or cached(louder(load_kokoro(voice, device), volume))
        self.pool = pool or ThreadPoolExecutor(1)
        self.first_audio_at: float | None = None
        self.player_returncode: int | None = None
        self.stopped = False
        self._q: asyncio.Queue | None = None
        self._writer: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None

    @property
    def speaking(self) -> bool:
        return self._writer is not None and not self._writer.done()

    async def say(self, text: str) -> None:
        if self.stopped and self.speaking:
            await asyncio.gather(self._writer, return_exceptions=True)
        if not self.speaking:
            self.player_returncode = None
            self.stopped = False
            self._q = asyncio.Queue()
            self._writer = asyncio.create_task(self._write(self._q))
        for s in split_sentences(text):
            # Kokoro can clip the final word without sentence-ending punctuation.
            if not s.rstrip('\"\'”’)]}»›').endswith(('.', '!', '?', '…', '。', '！', '？')):
                s += "."
            self._q.put_nowait(_BANG.sub("exclamation mark ", s))

    async def finish(self) -> None:
        if not self.speaking:
            return
        writer = self._writer
        self._q.put_nowait(_END)
        try:
            await asyncio.shield(writer)
        except asyncio.CancelledError:
            if not writer.cancelled():
                raise  # we were cancelled, not stop()ped

    def stop(self) -> None:
        self.stopped = True
        if self._writer:
            self._writer.cancel()
        if self._proc and self._proc.returncode is None:
            self._proc.kill()

    async def _write(self, q: asyncio.Queue) -> None:
        loop = asyncio.get_running_loop()
        self._proc = None
        try:
            while (s := await q.get()) is not _END:
                pcm = await loop.run_in_executor(self.pool, self.synth, s)
                if self._proc is None:
                    self._proc = await asyncio.create_subprocess_exec(
                        *PW_PLAY, stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    self.first_audio_at = None
                self._proc.stdin.write(pcm.astype(np.float32).tobytes())
                if self.first_audio_at is None:  # pw-play starts on first data; drain() ends near the sentence's end
                    self.first_audio_at = time.monotonic()
                await self._proc.stdin.drain()
            if self._proc:
                self._proc.stdin.close()
                await self._proc.wait()
        except (BrokenPipeError, ConnectionResetError):
            pass  # player died (sink gone); the turn just ends quieter
        finally:
            if self._proc:
                if self._proc.returncode is None:
                    self._proc.kill()
                self.player_returncode = await self._proc.wait()
