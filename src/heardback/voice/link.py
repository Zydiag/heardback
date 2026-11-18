"""Client side of the brain socket: newline-delimited JSON over a unix socket."""
import asyncio
import json
from pathlib import Path

from .config import log


class Link:
    def __init__(self, path: Path, voice_token: str | None = None):
        self.path = Path(path)
        self.voice_token = voice_token
        self.up = False
        self._r: asyncio.StreamReader | None = None
        self._w: asyncio.StreamWriter | None = None

    async def connect(self) -> bool:
        try:
            self._r, self._w = await asyncio.open_unix_connection(str(self.path), limit=1 << 20)
        except OSError:
            self.up = False
            return False
        self.up = True
        return await self.send({"type": "hello", "role": "voice", "token": self.voice_token})

    def post(self, msg: dict) -> bool:
        """Write without waiting for the buffer to drain: keeps messages in call order."""
        if not self.up:
            return False
        try:
            self._w.write((json.dumps(msg) + "\n").encode())
            return True
        except (OSError, RuntimeError):
            self._down()
            return False

    async def send(self, msg: dict) -> bool:
        if not self.post(msg):
            return False
        try:
            await self._w.drain()
            return True
        except (OSError, RuntimeError):
            self._down()
            return False

    async def recv(self) -> dict | None:
        while self.up:
            try:
                line = await self._r.readline()
            except (OSError, ValueError):
                line = b""
            if not line:
                self._down()
                return None
            try:
                msg = json.loads(line)
            except ValueError:
                log("bad_line", line=line[:200].decode(errors="replace"))
                continue
            if isinstance(msg, dict):
                return msg
        return None

    def _down(self) -> None:
        self.up = False
        if self._w:
            self._w.close()

    def close(self) -> None:
        self._down()
