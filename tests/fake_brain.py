"""A tiny brain socket server for voice tests: records what clients send, sends what the test says."""
import asyncio
import json


class FakeBrain:
    def __init__(self, path):
        self.path = str(path)
        self.received: list[dict] = []
        self.on_utterance = None   # optional: async fn(brain, msg) run for each utterance
        self._w = None
        self._server = None

    async def start(self):
        self._server = await asyncio.start_unix_server(self._client, self.path)

    async def _client(self, r, w):
        self._w = w
        while line := await r.readline():
            msg = json.loads(line)
            self.received.append(msg)
            if msg.get("type") == "utterance" and self.on_utterance:
                asyncio.create_task(self.on_utterance(self, msg))

    async def send(self, msg: dict):
        self._w.write((json.dumps(msg) + "\n").encode())
        await self._w.drain()

    def of(self, type_: str) -> list[dict]:
        return [m for m in self.received if m.get("type") == type_]

    async def wait_for(self, type_: str, timeout: float = 5, n: int = 1) -> dict:
        async def poll():
            while len(self.of(type_)) < n:
                await asyncio.sleep(0.01)
            return self.of(type_)[n - 1]
        return await asyncio.wait_for(poll(), timeout)

    async def drop(self):
        if self._w:
            self._w.close()
            self._w = None

    async def close(self):
        await self.drop()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
