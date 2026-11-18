"""LIVE probes: real `claude -p` (subscription login, never --bare), empty temp cwd, fake jv on PATH.
Run each once: `HEARDBACK_LIVE=1 uv run pytest tests/brain/test_live.py -k say_ok -v` (1 haiku turn),
then `-k budget` (at most 3 haiku turns), `-k permission` (1 turn, 2 model calls), `-k tool_turn` (2 turns),
`-k queue` (2 turns)."""
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from brainutil import BIN, FIX, jv_calls
from heardback.brain import brain as brain_module, claude_proc
from heardback.brain.claude_proc import PROMPT, ClaudeProc, build_cmd

# The autouse _safe_env swaps HOME / CLAUDE_CONFIG_DIR for temp dirs; the real login lives under the real ones.
REAL = {k: os.environ.get(k) for k in ("HOME", "CLAUDE_CONFIG_DIR")}
OUT = FIX / "stream_live.ndjson"
live = pytest.mark.skipif(os.environ.get("HEARDBACK_LIVE") != "1", reason="spends quota")


def _fake_first_mcp_path(monkeypatch):
    fake = str(BIN)
    monkeypatch.setattr(claude_proc, "with_bin", lambda _python: os.pathsep.join(
        [fake, *(item for item in os.environ.get("PATH", "").split(os.pathsep) if item and item != fake)]))


def _assert_fake_jv_first(cmd):
    config = json.loads(cmd[cmd.index("--mcp-config") + 1])
    path = config["mcpServers"]["jv"]["env"]["PATH"]
    assert path.split(os.pathsep)[0] == str(BIN)
    assert shutil.which("jv", path=path) == str(BIN / "jv")


@pytest.fixture
def login(env, monkeypatch):
    for k, v in REAL.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cwd = env / "cwd"   # empty
    cwd.mkdir()
    return cwd


async def _turn(p: ClaudeProc, text: str, lines: list, timeout: float = 90) -> list[dict]:
    """Send one message; events up to its result or the process exit."""
    await p.send(text)
    seen = []
    while True:
        ev = await asyncio.wait_for(p.events.get(), timeout)
        seen.append(ev)
        lines.append(ev)
        if ev["type"] in ("result", "_exit"):
            return seen


def _write(lines: list, probe: str, mode: str) -> None:
    with OUT.open(mode) as f:
        f.write(json.dumps({"_probe": probe}) + "\n")
        f.writelines(json.dumps(e) + "\n" for e in lines)


@live
def test_live_say_ok(login):
    cmd = build_cmd(["claude"], PROMPT)
    assert "--bare" not in cmd

    async def go():
        p = ClaudeProc(cmd, login)
        await p.start()
        lines: list = []
        try:
            return await _turn(p, "say ok", lines), lines
        finally:
            await p.kill()
    seen, lines = asyncio.run(go())
    _write(lines, "say_ok", "w")
    inits = [e for e in seen if e["type"] == "system" and e.get("subtype") == "init"]
    assert inits and inits[0]["tools"] == []
    assert any(e["type"] == "stream_event" and e["event"].get("delta", {}).get("type") == "text_delta" for e in seen)
    assert seen[-1]["type"] == "result" and seen[-1]["is_error"] is False


@live
def test_live_budget(login):
    cmd = build_cmd(["claude"], PROMPT)
    budget = os.environ.get("HEARDBACK_LIVE_BUDGET", "0.001")   # 2nd run (M2 review): 0.0001, 1 turn
    turns = int(os.environ.get("HEARDBACK_LIVE_TURNS", "3"))
    cmd[cmd.index("--max-budget-usd") + 1] = budget

    async def go():
        p = ClaudeProc(cmd, login)
        await p.start()
        lines: list = []
        try:
            for _ in range(turns):
                seen = await _turn(p, "say ok", lines)
                if seen[-1]["type"] == "_exit" or seen[-1].get("is_error"):
                    break
            return lines, p.alive
        finally:
            await p.kill()
    lines, alive = asyncio.run(go())
    _write(lines + [{"_alive_after": alive}], f"budget {budget} x{turns}", "a")
    ends = [(e["type"], e.get("subtype"), e.get("is_error"), e.get("total_cost_usd"), e.get("code")) for e in lines
            if e["type"] in ("result", "_exit")]
    print("budget probe ends:", ends, "alive:", alive)


def _two_tool_catalogue(env) -> Path:
    data = json.loads((FIX / "catalogue.json").read_text())
    data["verbs"] = [v for v in data["verbs"] if v["name"] in ("wall.next", "zen.close-unused")]
    p = env / "catalogue2.json"
    p.write_text(json.dumps(data))
    return p


@live
def test_live_permission(login, env, monkeypatch):
    """Task 12: does `--permission-prompt-tool stdio` send can_use_tool for a tool not in --allowedTools?"""
    monkeypatch.setenv("FAKE_JV_CATALOGUE", str(_two_tool_catalogue(env)))
    _fake_first_mcp_path(monkeypatch)
    cmd = build_cmd(["claude"], PROMPT, allowed=["mcp__jv__wall_next"], mcp_python=sys.executable)
    _assert_fake_jv_first(cmd)

    async def go():
        p = ClaudeProc(cmd, login)
        await p.start()
        lines: list = []
        try:
            await p.send("close unused tabs using the zen_close-unused tool")
            while True:
                ev = await asyncio.wait_for(p.events.get(), 90)
                lines.append(ev)
                if ev["type"] == "control_request" and ev["request"].get("subtype") == "can_use_tool":
                    await p.respond(ev["request_id"], {"behavior": "deny", "message": "User said no."})
                if ev["type"] in ("result", "_exit"):
                    return lines
        finally:
            await p.kill()
    lines = asyncio.run(go())
    _write(lines, "permission", "a")
    init = next(e for e in lines if e["type"] == "system" and e.get("subtype") == "init")
    print("init tools:", init.get("tools"), "mcp:", init.get("mcp_servers"), "mode:", init.get("permissionMode"))
    reqs = [e["request"] for e in lines if e["type"] == "control_request"]
    print("control_requests:", reqs)
    print("jv calls:", jv_calls(env))
    assert any(r.get("subtype") == "can_use_tool" and r.get("tool_name") == "mcp__jv__zen_close-unused" for r in reqs)
    assert lines[-1]["type"] == "result"
    assert not any("--yes" in c for c in jv_calls(env))


@live
def test_live_tool_turn(login, env, monkeypatch):
    """Task 16: real claude behind the brain; a pre-allowed tool runs via MCP, a confirm tool asks and a "no" denies."""
    from brainutil import script, serving
    from heardback.brain.brain import Brain, sock_path
    from heardback.status.store import state_dir
    script(env, {"wall next": {"rc": 0, "stdout": '{"ok":true,"say":"Next wallpaper."}'},
                 "zen close-unused --dry-run": {"rc": 3, "stdout": '{"ok":false,"confirm":"Close 3 unused tabs?"}'}})
    _fake_first_mcp_path(monkeypatch)
    original_build_cmd = brain_module.build_cmd

    def checked_build_cmd(*args, **kwargs):
        cmd = original_build_cmd(*args, **kwargs)
        if kwargs.get("mcp_python"):
            _assert_fake_jv_first(cmd)
        return cmd

    monkeypatch.setattr(brain_module, "build_cmd", checked_build_cmd)
    ask = [sys.executable, "-c", "import sys; from jv.ask import main; sys.exit(main(sys.argv[1:]))"]  # fake jv shadows jv
    lines: list = []

    async def jv_ask(text: str, stdin: str = "") -> tuple[int, str]:
        p = await asyncio.create_subprocess_exec(*ask, text, stdin=asyncio.subprocess.PIPE,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(p.communicate(stdin.encode()), 150)
        lines.extend(json.loads(l) for l in (state_dir() / "brain-stream.jsonl").read_text().splitlines())
        return p.returncode, out.decode()

    async def go():
        async with serving(Brain(claude_cmd=["claude"]), sock_path()):
            a = await jv_ask("switch to the next wallpaper and tell me you did")
            calls_a = jv_calls(env)
            b = await jv_ask("close my unused tabs, use the tool", "no\n")
            return a, calls_a, b
    a, calls_a, b = asyncio.run(go())
    _write(lines, "tool_turn", "a")
    print("A:", a, "\nB:", b, "\njv calls:", jv_calls(env))
    assert a[0] == 0 and ["wall", "next", "--json"] in calls_a
    assert b[0] == 0 and "Close 3 unused tabs? [yes/no]" in b[1]
    assert any("--dry-run" in c and c[:2] == ["zen", "close-unused"] for c in jv_calls(env))
    assert not any("--yes" in c for c in jv_calls(env))


@live
def test_live_queue(login):
    """Task 17: a 2nd user message written mid-turn: two results in order, merged, or an error? Recorded only:
    the brain never relies on it (R4, QUEUE_IN_CLAUDE = False)."""
    cmd = build_cmd(["claude"], PROMPT)

    async def go():
        p = ClaudeProc(cmd, login)
        await p.start()
        lines: list = []
        try:
            await p.send("count slowly from one to twenty in words")
            sent2, results = False, 0
            while results < 2:
                try:
                    ev = await asyncio.wait_for(p.events.get(), 30 if sent2 else 90)
                except TimeoutError:
                    lines.append({"_timeout": True})
                    break
                lines.append(ev)
                d = ev.get("event", {}).get("delta", {}) if ev["type"] == "stream_event" else {}
                if not sent2 and d.get("type") == "text_delta":
                    sent2 = True
                    lines.append({"_sent": "say banana"})
                    await p.send("say banana")
                results += ev["type"] == "result"
                if ev["type"] == "_exit":
                    break
            return lines
        finally:
            await p.kill()
    lines = asyncio.run(go())
    _write(lines, "queue", "a")
    res = [(e.get("subtype"), e.get("is_error"), e.get("result")) for e in lines if e.get("type") == "result"]
    print("queue probe results:", res, "timeout:", any("_timeout" in e for e in lines))
    assert res   # the first turn at least ended
