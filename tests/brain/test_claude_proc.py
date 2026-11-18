import asyncio
from pathlib import Path

from brainutil import FAKE, user_messages
from model_text import model_text
from heardback.brain.claude_proc import PROMPT, ClaudeProc, build_cmd, child_env, prompt_text
from heardback.config import config_path


def test_cmd_flags():
    c = build_cmd(["claude"], Path("/p.md"))
    assert "--bare" not in c and c[c.index("--model") + 1] == "haiku"
    assert c[c.index("--tools") + 1] == "" and "--restricted" in c and "--no-session-persistence" in c
    assert c[c.index("--setting-sources") + 1] == ""   # R9: no user allow rules / hooks in the brain
    assert c[c.index("--max-turns") + 1] == "8" and c[c.index("--max-budget-usd") + 1] == "0.50"
    assert c[c.index("--system-prompt-file") + 1] == "/p.md" and "--mcp-config" not in c


def test_cmd_with_mcp(monkeypatch):
    import json
    monkeypatch.setenv("PATH", "/x/bin")
    monkeypatch.delenv("LANG", raising=False); monkeypatch.delenv("LC_ALL", raising=False)
    c = build_cmd(["claude"], Path("/p.md"), ["mcp__jv__wall_next", "mcp__jv__tmux_select"], "/venv/python",
                  execution_owner="process-owner")
    assert json.loads(c[c.index("--mcp-config") + 1]) == {"mcpServers": {"jv": {
        "type": "stdio", "command": "/venv/python", "args": ["-m", "heardback.brain.mcp_jv"], "env": {"PATH": "/venv:/x/bin",
        "HEARDBACK_REQUIRE_GRANT": "1", "HEARDBACK_EXECUTION_GATE": "1",
        "HEARDBACK_EXECUTION_OWNER": "process-owner", "HEARDBACK_CONFIG": str(config_path().absolute())}}}}
    assert c[c.index("--allowedTools") + 1] == "mcp__jv__wall_next,mcp__jv__tmux_select"
    assert c[c.index("--permission-prompt-tool") + 1] == "stdio" and "--bare" not in c


def test_mcp_path_puts_install_bin_first(monkeypatch):
    from heardback.brain.claude_proc import with_bin
    monkeypatch.setenv("PATH", "/repo/.venv/bin:/usr/bin:/tool/bin")
    assert with_bin("/tool/bin/python") == "/tool/bin:/repo/.venv/bin:/usr/bin"


def test_cmd_mcp_env_carries_locale(monkeypatch):
    import json
    monkeypatch.setenv("LANG", "en_US.UTF-8"); monkeypatch.delenv("LC_ALL", raising=False)
    env = json.loads((c := build_cmd(["claude"], Path("/p.md"), [], "/py"))[c.index("--mcp-config") + 1])
    env = env["mcpServers"]["jv"]["env"]
    assert env["LANG"] == "en_US.UTF-8" and "LC_ALL" not in env


def test_mcp_keeps_config_when_child_changes_directory(monkeypatch):
    import json
    monkeypatch.setenv("HEARDBACK_CONFIG", "examples/config.toml")
    cmd = build_cmd(["claude"], PROMPT, [], "/py")
    env = json.loads(cmd[cmd.index("--mcp-config") + 1])["mcpServers"]["jv"]["env"]
    assert env["HEARDBACK_CONFIG"] == str(Path("examples/config.toml").absolute())


def test_prompt_is_spec_text(monkeypatch):
    monkeypatch.delenv("HEARDBACK_CONFIG", raising=False)
    t = model_text()
    assert t.startswith("You are heardback, a voice assistant on {{user}}'s Linux laptop.")
    assert prompt_text().startswith("You are heardback, a voice assistant on the user's Linux laptop.")
    assert "Never claim an action you didn't run." in t
    assert "Screen locking has been removed from heardback." in t
    assert "Never launch a locker, run a lock command, or delegate a lock request" in t


def test_prompt_type_requests_act():
    """Live 15:34: "type in this window voice typing is working" got a chat reply, then typed the command words."""
    t = model_text()
    assert '"Type X" or "write X"' in t and "is an order" in t and "only X as the text, without the command words" in t
    assert 'target "current" unless a window is named' in t and 'press enter only if they say "and press enter"' in t


def test_prompt_clipboard_and_selection():
    """Live 18:57 (turns 20261031-185728-5, -185742-6): "Send the Slector Test to third window" typed "Slector
    Test"; "Selected Text Text I have Copied and Press Enter" sent tmux_type text="" enter=true."""
    t = model_text()
    assert '"Copied" or "clipboard" means the clipboard' in t and '"selected", "highlighted" or a bare "this"' in t
    assert "call tmux_type with clipboard or selection true and no text" in t and '"Slector Test" is "selected text"' in t
    assert "Never press enter in a window you didn't type into in this turn, unless the user asked only to press enter" in t
    assert "say so and do nothing" in t and "ctx_ctx" in t
    assert "selected text from the clipboard" in t and "ask one short question about which source" in t
    assert "Want me to paste it? Which window?" in t and "Never invent a source_revision" in t


def test_prompt_web_apps_open_in_zen():
    """Live 16:26: "open whatsapp" launched htop twice via hypr_open/hypr_launch."""
    t = model_text()
    assert "WhatsApp, YouTube or Gmail are Zen tabs" in t and 'says "no installed app", use zen_open' in t
    assert '"Open Spotify" means hypr_open with app Spotify' in t
    assert "an explicit Spotify website or Spotify in Zen means zen_open with query spotify" in t
    assert "unless the user explicitly required the Spotify desktop app" in t
    assert "never launch an app whose name differs from what the user said" in t


def test_prompt_project_by_name():
    """Live 16:33: "open a new claude in web app project" asked for a path, then typed a zoxide cd."""
    t = model_text()
    assert 'tmux_new with dir set to the project name as heard' in t
    assert "never build cd, zoxide or other shell commands to reach a folder" in t
    assert "say the closest projects from the error instead of asking for a path" in t


def test_child_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    e = child_env()
    assert "ANTHROPIC_API_KEY" not in e and e["HEARDBACK_BRAIN"] == "1"


async def _events(p: ClaudeProc, n: int) -> list[dict]:
    return [await asyncio.wait_for(p.events.get(), 5) for _ in range(n)]


def test_turn_roundtrip(env):
    async def go():
        p = ClaudeProc(FAKE, env / "cwd")
        await p.start()
        await p.send("hi")
        seen = await _events(p, 5)
        await p.kill()
        return seen
    seen = asyncio.run(go())   # live shape (Task 11): the whole `assistant` message follows the deltas
    assert [m["type"] for m in seen] == ["system", "stream_event", "stream_event", "assistant", "result"]
    assert seen[-1]["result"] == "You said: hi." and user_messages(env) == ["hi"]


def test_crash_gives_exit_event(env):
    async def go():
        p = ClaudeProc(FAKE, env / "cwd")
        await p.start()
        await p.send("cue:crash")
        seen = await _events(p, 2)
        return seen, p.alive
    seen, alive = asyncio.run(go())
    assert seen[-1] == {"type": "_exit", "code": 1} and not alive


def test_control_and_respond(env):
    async def go():
        p = ClaudeProc(FAKE, env / "cwd")
        await p.start()
        rid = await p.control("interrupt")
        ack = await _events(p, 1)
        await p.send('cue:tool mcp__jv__wall_next {"a": 1}')
        init, use, req = await _events(p, 3)   # tool_use streams before the ask (live order)
        await p.respond(req["request_id"], {"behavior": "deny", "message": "User said no."})
        rest = await _events(p, 4)   # tool_result, delta, assistant, result
        await p.kill()
        return rid, ack, req, rest
    rid, ack, req, rest = asyncio.run(go())
    assert rid == "jv-1" and ack[0]["response"]["request_id"] == "jv-1"
    assert req["request"] == {"subtype": "can_use_tool", "tool_name": "mcp__jv__wall_next", "input": {"a": 1}}
    assert rest[-1]["result"] == "denied: User said no.."   # brief: "denied: <message>."


def test_interrupt_slow_turn(env):
    async def go():
        p = ClaudeProc(FAKE, env / "cwd")
        await p.start()
        await p.send("cue:slow 5")
        await _events(p, 1)
        await p.control("interrupt")
        seen = await _events(p, 2)
        await p.kill()
        return seen
    seen = asyncio.run(go())
    assert seen[-1]["subtype"] == "error_during_execution"


def test_kill_is_sigterm(env):
    async def go():
        p = ClaudeProc(FAKE, env / "cwd")
        await p.start()
        await p.kill()
        return p.alive, await _events(p, 1)
    alive, ev = asyncio.run(go())
    assert not alive and ev == [{"type": "_exit", "code": -15}]


def test_kill_bounded_when_grandchild_holds_stdout(env, monkeypatch):   # M2 review
    import time
    from heardback.brain import claude_proc
    monkeypatch.setattr(claude_proc, "KILL_GRACE_S", 0.2)

    async def go():
        p = ClaudeProc(["sh", "-c", "sleep 1 & exec sleep 5"], env / "cwd")
        await p.start()
        t0 = time.monotonic()
        await p.kill()
        dt = time.monotonic() - t0
        ev = await _events(p, 1)
        await asyncio.sleep(1)   # let the grandchild go before the loop closes
        return dt, ev
    dt, ev = asyncio.run(go())
    assert dt < 1 and ev == [{"type": "_exit", "code": -15}]
