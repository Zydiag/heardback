"""E2: jv_run resolves to its inner verb before the gate; jv_find reads only the catalogue."""
import asyncio
import json
from pathlib import Path

import pytest

from brainutil import FAKE, _inputs, jv_calls, run_brain, serving, talk, utt
from heardback.brain import brain, catalogue, mcp_jv
from jv.verb import runtime_path

ARGS = {"target": "2", "text": "fix it", "enter": True}
READ = utt("cue:tool mcp__jv__status_list {}")   # status text: outside text, taints the conversation
YES = {"confirm": {"type": "confirm_reply", "ok": True, "text": "yes", "speaker_ok": True}}


def run(verb, args, **extra) -> dict:
    return utt("cue:tool mcp__jv__jv_run " + json.dumps({"verb": verb, "args": args, **extra}))


def responses(env) -> list[dict]:
    return [m["response"]["response"] for _, m in _inputs(env) if m.get("type") == "control_response"]


def confirms(out) -> list[str]:
    return [m["text"] for m in out if m.get("type") == "confirm"]


def acts(env) -> list[list[str]]:
    return [c for c in jv_calls(env) if c[:1] != ["list"]]


@pytest.fixture
def real_grants(env, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MCP", "1")
    monkeypatch.setenv("FAKE_JV_REAL_GRANTS", "1")
    monkeypatch.setenv("FAKE_JV_ACTED", str(env / "acted.json"))


def test_jv_run_gets_the_direct_tools_readback_and_grant(env, real_grants):
    out = run_brain(env, [run("tmux_type", ARGS)], claude_cmd=FAKE, replies=YES)
    assert confirms(out) == ["Send 'fix it' to window 2?"]   # the same read-back the direct tmux_type gets
    assert responses(env) == [{"behavior": "allow", "updatedInput": {"verb": "tmux_type", "args": ARGS}}]
    assert json.loads((env / "acted.json").read_text()) == ARGS   # the grant was keyed on tmux.type + ARGS
    assert not list(Path(runtime_path(".")).glob("heardback-grant-*"))


def test_jv_run_said_no_runs_nothing(env, real_grants):
    no = {"confirm": {"type": "confirm_reply", "ok": False, "text": "no", "speaker_ok": True}}
    out = run_brain(env, [run("tmux_type", ARGS)], claude_cmd=FAKE, replies=no)
    assert confirms(out) == ["Send 'fix it' to window 2?"]
    assert responses(env)[-1]["behavior"] == "deny" and not (env / "acted.json").exists()


def test_tainted_turn_refuses_non_read_jv_run_but_not_the_direct_tool(env, real_grants):
    b = brain.Brain(claude_cmd=FAKE)

    async def go():   # two turns: the read taints the conversation, the next turn's acts see it
        path = brain.sock_path()
        async with serving(b, path):
            await talk(path, [READ])
            assert b._tainted is b.claude
            await talk(path, [run("wall_next", {})])
            via_run = [c for c in acts(env) if c[:2] == ["wall", "next"]]
            await talk(path, [utt("cue:tool mcp__jv__wall_next {}")])
            return via_run
    via_run = asyncio.run(go())
    denied, direct = responses(env)[-2:]
    assert denied == {"behavior": "deny", "message": brain.NOT_DONE["tainted"]}
    assert via_run == []   # refused before any dry run
    assert direct == {"behavior": "allow", "updatedInput": {}}   # direct reversible tool: unchanged rule


def test_tainted_turn_still_allows_read_jv_run(env):
    out = run_brain(env, [READ, run("wall_current", {})], claude_cmd=FAKE)
    assert responses(env)[-1] == {"behavior": "allow", "updatedInput": {"verb": "wall_current", "args": {}}}
    assert confirms(out) == []


@pytest.mark.parametrize("verb,args,extra", [
    ("rm_rf", {}, {}),                                   # unknown verb
    ("zen_install", {}, {}),                             # not voice-enabled: not Claude's
    ("say_say", {"text": "hi"}, {}),                     # CLI-only command
    ("wall_next", {"bogus": "1"}, {}),                   # arg outside the verb's schema
    ("wall_next", {}, {"yes": True}),                    # outer field next to verb/args
    ("wall_next", "x", {}),                              # args not an object
], ids=["unknown", "not-voice", "say", "bad-arg", "outer-field", "args-not-object"])
def test_jv_run_unknown_or_malformed_is_refused_nothing_spawned(env, verb, args, extra):
    run_brain(env, [run(verb, args, **extra)], claude_cmd=FAKE)
    assert responses(env) == [{"behavior": "deny", "message": brain.UNKNOWN_VERB}]
    assert acts(env) == []


def test_mcp_jv_run_refuses_unknown_and_outer_fields_without_spawning(env):
    cat = catalogue.get(force=True)
    for args in ({"verb": "rm_rf"}, {"verb": "wall_next", "args": {}, "grant": "x"}, {"verb": 3}, {"args": {}}):
        r = mcp_jv.call(cat, "jv_run", args)
        assert r["isError"] and "call jv_find" in r["content"][0]["text"]
    assert acts(env) == []


def test_mcp_jv_run_runs_the_inner_verb(env):
    r = mcp_jv.call(catalogue.get(force=True), "jv_run", {"verb": "wall.next"})   # catalogue name works too
    assert not r["isError"] and acts(env) == [["wall", "next", "--json"]]


def test_jv_find_reads_only_the_catalogue(env):
    run_brain(env, [utt('cue:tool mcp__jv__jv_find {"query": "next wallpaper"}')], claude_cmd=FAKE)
    assert responses(env) == [{"behavior": "allow", "updatedInput": {"query": "next wallpaper"}}]
    found = json.loads(mcp_jv.call(catalogue.get(force=True), "jv_find", {"query": "next wallpaper"})["content"][0]["text"])
    assert found["verbs"][0]["verb"] == "wall_next" and "zen_install" not in found["all"]
    assert found["verbs"][0]["args"] == catalogue.get()["wall.next"]["params"]
    assert acts(env) == []


@pytest.mark.parametrize("inp,tainted", [
    ({"verb": "status_list"}, True), ({"verb": "tmux_type", "args": ARGS}, False),
    ({"verb": "nope"}, True), ({"verb": "tmux_type", "args": ARGS, "x": 1}, True)],
    ids=["reader", "clean", "unknown", "malformed"])
def test_jv_run_taints_by_its_inner_verb(env, inp, tainted):
    b = brain.Brain()
    b.router.cat = catalogue.get(force=True)
    b.claude = object()
    asyncio.run(b._on_event({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t", "name": "mcp__jv__jv_run", "input": inp}]}}))
    assert (b._tainted is b.claude) is tainted


def test_jv_find_keeps_the_conversation_clean(env):
    b = brain.Brain()
    b.claude = object()
    asyncio.run(b._on_event({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t", "name": "mcp__jv__jv_find", "input": {"query": "x"}}]}}))
    assert b._tainted is None
