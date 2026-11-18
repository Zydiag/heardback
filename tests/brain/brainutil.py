"""Shared helpers for brain tests (imported by tests; conftest holds the fixtures)."""
import asyncio
import contextlib
import json
import sys
from pathlib import Path

BIN = Path(__file__).parent / "bin"
FIX = Path(__file__).parent / "fixtures"
FAKE = [sys.executable, str(Path(__file__).parent / "fake_claude.py")]
VOICE_TOKEN = "test-voice-token"
HELLO = {"type": "hello", "role": "voice", "token": VOICE_TOKEN}


def script(tmp_path, replies: dict) -> None:
    (tmp_path / "jv-script.json").write_text(json.dumps(replies))


def jv_calls(tmp_path) -> list[list[str]]:
    log = tmp_path / "jv.log"
    return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


def load_fixture() -> dict:
    data = json.loads((FIX / "catalogue.json").read_text())
    return {v["name"]: v for v in data["verbs"] if v.get("voice", True)}


async def voice_connection(path):
    r, w = await asyncio.open_unix_connection(str(path))
    w.write((json.dumps(HELLO) + "\n").encode())
    await w.drain()
    return r, w


async def talk(path, msgs: list[dict], until: str = "done", replies: dict | None = None,
               timeout: float = 5, count: int = 1, gap: float = 0, voice: bool = True) -> list[dict]:
    """Connect, send msgs (`gap` s apart), collect lines until `count` messages of type `until` (or timeout)."""
    r, w = await asyncio.open_unix_connection(str(path))
    if voice and any(m.get("type") == "utterance" for m in msgs):
        w.write((json.dumps(HELLO) + "\n").encode())
    for i, m in enumerate(msgs):
        if gap and i:
            await asyncio.sleep(gap)
        w.write((json.dumps(m) + "\n").encode())
        await w.drain()
    out = []
    try:
        async with asyncio.timeout(timeout):
            while line := await r.readline():
                msg = json.loads(line)
                out.append(msg)
                if msg.get("type") == "confirm" and replies and "confirm" in replies:
                    reply = dict(replies["confirm"])
                    if reply.get("type") == "confirm_reply":
                        reply.setdefault("confirmation_id", msg.get("confirmation_id"))
                    w.write((json.dumps(reply) + "\n").encode())
                    await w.drain()
                if msg.get("type") == until:
                    count -= 1
                    if count <= 0:
                        break
    except TimeoutError:
        pass
    w.close()
    return out


async def wait_socket(path, timeout: float = 3) -> None:
    """Until a connect succeeds (a stale socket file may already exist)."""
    async with asyncio.timeout(timeout):
        while True:
            try:
                _, w = await asyncio.open_unix_connection(str(path))
            except OSError:
                await asyncio.sleep(0.01)
            else:
                w.close()
                return


@contextlib.asynccontextmanager
async def serving(brain, path):
    brain.voice_token = VOICE_TOKEN   # emulate the daemon's in-memory token handoff
    task = asyncio.create_task(brain.serve(path))
    await wait_socket(path)
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def run_brain(env, msgs: list[dict], brain=None, timeout: float = 5, replies: dict | None = None,
              gap: float = 0, **overrides) -> list[dict]:
    """Serve a brain, send msgs on one connection, collect until every utterance got its `done`."""
    from heardback.brain import brain as brain_mod

    n = max(1, sum(m.get("type") == "utterance" for m in msgs))

    async def go():
        path = brain_mod.sock_path()
        async with serving(brain or brain_mod.Brain(**overrides), path):
            return await talk(path, msgs, timeout=timeout, count=n, replies=replies, gap=gap)
    return asyncio.run(go())


def says(out: list[dict]) -> list[str]:
    return [m["text"] for m in out if m.get("type") == "say"]


def _fake_log(env) -> list[dict]:
    p = Path(env) / "claude.log"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def _inputs(env) -> list[tuple[int, dict]]:
    out = []
    for e in _fake_log(env):
        if "in" in e:
            with contextlib.suppress(ValueError):
                out.append((e["pid"], json.loads(e["in"])))
    return out


def user_messages(env) -> list[str]:
    marker = "\n\nCurrent request:\n"
    return [m["message"]["content"].split(marker, 1)[-1]
            for _, m in _inputs(env) if m.get("type") == "user"]


def last_user_message(env) -> str | None:
    return (user_messages(env) or [None])[-1]


def pids(env) -> list[int]:
    """The fake claude pid that received each user message, in order."""
    return [pid for pid, m in _inputs(env) if m.get("type") == "user"]


def control_requests(env) -> list[dict]:
    return [m["request"] for _, m in _inputs(env) if m.get("type") == "control_request"]


def utt(text: str, speaker_ok: bool = True) -> dict:
    return {"type": "utterance", "text": text, "speaker_ok": speaker_ok, "t": 0}
