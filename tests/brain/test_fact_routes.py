"""Source-only memory phrases and dispatch revision refresh."""
import asyncio
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from heardback.brain import brain, memory, router
from jv.cli import all_verbs, catalogue


@pytest.fixture
def cat():
    return {v["name"]: v for v in catalogue(all_verbs())["verbs"] if v["voice"]}


@pytest.mark.parametrize("raw,name,text", [
    ("remember that sample fact", "fact.remember", "sample fact"),
    ("forget that sample fact", "fact.forget", "sample fact"),
    ("forget about sample fact", "fact.forget", "sample fact"),
    ("forget about it", "fact.forget", "it"),
    ("remember that read in the background", "fact.remember", "read in the background"),
    ("remember that " + "word " * 25, "fact.remember", "word " * 24 + "word"),
])
def test_routes(cat, raw, name, text):
    route = router.Router(cat).route(raw)
    assert route and route.verb == name and route.args["text"] == text


def test_collisions(cat):
    assert router.normalise("forget that") in brain.RESET
    assert router.Router(cat).route("remember to call mum at 6") is None


def test_remember_resets_memory_on_next_dispatch(tmp_path, monkeypatch):
    from jv import facts
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = memory.memory_path()
    assert path == tmp_path / "heardback/memory.md" and path.is_relative_to(tmp_path)
    b = brain.Brain()
    b._memory_context_revision = memory.read_memory().revision
    a = {"text": "sample fact"}
    facts.VERBS[0].check(a)
    a["source_revision"] = a["_source_revision"]
    facts.VERBS[0].fn(a)
    b.claude = SimpleNamespace()
    b.turns = deque([brain.Turn(None, "sample request")])
    b._ready = lambda: True
    b.last_send = 0
    async def reset(reason):
        b._ready = lambda: False
    b.reset = AsyncMock(side_effect=reset)
    asyncio.run(b._kick())
    b.reset.assert_awaited_once_with("memory")
