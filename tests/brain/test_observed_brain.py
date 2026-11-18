"""E4a: speech, MCP receipts, and the shared direct/jv_run budget. Helpers stay real."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

from brainutil import jv_calls, run_brain, says, script, utt, _inputs
from heardback.brain import brain, catalogue, mcp_jv, play_result, router
from jv import cli
from jv.verb import UNCONFIRMED

FLAGGED = {"volume", "brightness", "hypr.focus", "zen.switch", "media"}


class Voice:
    def __init__(self):
        self.messages = []

    def is_closing(self):
        return False

    def write(self, data):
        self.messages.append(json.loads(data))

    async def drain(self):
        pass

    def says(self):
        return [m["text"] for m in self.messages if m["type"] == "say"]


class Model:
    def __init__(self):
        self.responses = {}

    async def respond(self, rid, response):
        self.responses[rid] = response


@pytest.fixture
def cat():
    selected = FLAGGED | {"local.time", "wall.next"}
    return catalogue._index(cli.catalogue([v for v in cli.all_verbs() if v.name in selected]))


@pytest.mark.parametrize("verb", sorted(FLAGGED))
@pytest.mark.parametrize("observed", [None, False, True])
@pytest.mark.parametrize("model", [False, True])
def test_both_speech_paths_require_the_receipt(cat, verb, observed, model):
    async def go():
        b, voice = brain.Brain(), Voice()
        b.router = router.Router(cat)
        data = {"ok": True, "say": "Done."}
        if observed is not None:
            data["observed"] = observed
        if model:
            turn = brain.Turn(voice, "do it", tid="observed", sent=True)
            b.turns.append(turn)
            name = catalogue.tool_name(cat[verb])
            await b._on_event({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "act", "name": "mcp__jv__jv_run",
                 "input": {"verb": name, "args": {}}}]}})
            await b._on_event({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "act", "content": json.dumps(data)}]}})
            if observed is not True:
                # An optimistic model summary after an unverified result cannot reach speech.
                await b._on_event({"type": "stream_event", "event": {"type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Done.\n"}}})
                turn.reply_text = "Done."
            await b.finish(turn)
        else:
            await b._speak_jv(router.Route(verb, {}, 1), 0, json.dumps(data), "", voice, "do it")
        assert voice.says() == (["Done."] if observed is True else [UNCONFIRMED])
        assert voice.messages[-1]["type"] == "done"
    asyncio.run(go())


@pytest.mark.parametrize("verb", ["volume", "media"])
@pytest.mark.parametrize("observed", [None, False, True])
@pytest.mark.parametrize("via_run", [False, True])
def test_mcp_model_result_keeps_verification(env, cat, verb, observed, via_run):
    data = {"ok": True, "say": "volume 30" if verb == "volume" else "playing a song"}
    if observed is not None:
        data["observed"] = observed
    args = {"action": "set", "n": 30} if verb == "volume" else {"action": "play"}
    script(env, {"volume --json": {"stdout": json.dumps(data)}, "media --json": {"stdout": json.dumps(data)}})
    name = catalogue.tool_name(cat[verb])
    result = mcp_jv.call(cat, "jv_run" if via_run else name,
                         {"verb": name, "args": args} if via_run else args, expected_turn="receipt")
    output = json.loads(result["content"][0]["text"])
    assert not result["isError"] and output["ok"] is True
    assert output.get("observed") is observed
    assert (output["say"] == UNCONFIRMED) is (observed is not True)
    if verb == "media":
        assert play_result.take(output["receipt"], "receipt", "") == (data["say"] if observed is True else UNCONFIRMED)


def test_media_observation_cannot_expose_outside_text(env, cat):
    script(env, {"media --json": {"stdout": json.dumps({
        "ok": True, "say": "playing a song", "observed": {"title": "untrusted page canary"}})}})
    result = mcp_jv.call(cat, "media_media", {"action": "play"}, expected_turn="opaque")
    text = result["content"][0]["text"]
    assert "untrusted page canary" not in text
    visible = json.loads(text)
    assert visible["observed"] is False and visible["say"] == UNCONFIRMED
    assert play_result.take(visible["receipt"], "opaque", "") == UNCONFIRMED


@pytest.mark.parametrize("parallel", [False, True])
def test_budget_mixes_direct_and_run_excludes_reads_and_resets_next_turn(env, cat, parallel):
    script(env, {"media --dry-run --dry-run": {"stdout": json.dumps({
        "ok": True, "source_revision": "media:player:" + "a" * 64})}})
    async def go():
        b, voice, model = brain.Brain(), Voice(), Model()
        b.router, b.claude = router.Router(cat), model

        async def call(turn, rid, verb, args, via_run=False):
            name = catalogue.tool_name(cat[verb])
            await b._can_use_tool(model, turn, rid, {
                "tool_name": "mcp__jv__" + ("jv_run" if via_run else name), "tool_use_id": rid,
                "input": {"verb": name, "args": args} if via_run else args})
            return model.responses[rid]

        for n in range(2):
            turn = brain.Turn(voice, "several steps", tid=f"budget-{n}", sent=True)
            b.turns.append(turn)
            # These reads include verbs whose catalogue effect is reversible/unknown.
            for verb, args in [("local.time", {}), ("volume", {"action": "get"}),
                               ("brightness", {"action": "get"}), ("media", {"action": "status"})]:
                assert (await call(turn, f"{n}-read-{verb}", verb, args, True))["behavior"] == "allow"
            assert turn.acting_calls == 0
            calls = [call(turn, f"{n}-{i}", "volume", {"action": "set", "n": 30}, i % 2 == 1)
                     for i in range(5)]
            results = await asyncio.gather(*calls) if parallel else [await c for c in calls]
            assert [r["behavior"] for r in results] == ["allow"] * 4 + ["deny"]
            assert results[-1] == {"behavior": "deny", "message": "Too many steps; ask me one at a time."}
            assert turn.acting_calls == 4
            for i in range(4):
                brain.act(turn, f"{n}-{i}", "volume_volume").update(state="ran")
            before = len(jv_calls(env))
            assert (await call(turn, f"{n}-six", "volume", {"action": "mute"}))["behavior"] == "deny"
            assert len(jv_calls(env)) == before  # refused before preflight or grant
            assert (await call(turn, f"{n}-last-read", "local.time", {}, True))["behavior"] == "allow"
            await b.finish(turn)
            b.turns.popleft()
        assert voice.says() == [brain.STEP_LIMIT, brain.STEP_LIMIT]
        assert len([c for c in jv_calls(env) if "--dry-run" in c and "set" in c]) == 8
    asyncio.run(go())


def test_socket_turn_lifecycle_resets_the_budget(env, cat, monkeypatch):
    data = {"version": 1, "verbs": list(cat.values())}
    path = env / "catalogue.json"
    path.write_text(json.dumps(data))
    monkeypatch.setenv("FAKE_JV_CATALOGUE", str(path))
    calls = [{"name": "mcp__jv__jv_run" if i % 2 else "mcp__jv__volume_volume",
              "args": {"verb": "volume", "args": {"action": "set", "n": 30}} if i % 2 else
                      {"action": "set", "n": 30},
              "result": {"ok": True, "observed": True, "say": "volume 30"}} for i in range(5)]
    plan = {"calls": calls, "text": ""}
    # A refused tool taints the model conversation; direct reversible tools remain permitted next turn.
    second = {"calls": [{**c, "name": "mcp__jv__volume_volume", "args": {"action": "set", "n": 30}}
                        for c in calls], "text": ""}
    monkeypatch.setenv("PERSONAL_STREAM", json.dumps({"first request": plan, "second request": second}))
    out = run_brain(env, [utt("first request"), utt("second request")],
                    claude_cmd=[sys.executable, str(Path(__file__).with_name("personal_stream.py"))])
    replies = [m["response"] for _, m in _inputs(env) if m.get("type") == "control_response"]
    assert len(replies) == 10
    for group in (replies[:5], replies[5:]):
        by_id = {r["request_id"]: r["response"] for r in group}
        assert [by_id[str(i)]["behavior"] for i in range(5)] == ["allow"] * 4 + ["deny"]
        assert by_id["4"]["message"] == brain.STEP_LIMIT
    assert says(out).count(brain.STEP_LIMIT) == 2
    assert len([c for c in jv_calls(env) if "--dry-run" in c]) == 8
