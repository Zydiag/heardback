import json
import os
import subprocess
import sys

from brainutil import jv_calls, script


def mcp(env, msgs: list[dict]) -> list[dict]:
    """Run the server as Claude Code would (cwd = an empty dir, PATH carries the fake jv)."""
    cwd = env / "empty"
    cwd.mkdir(exist_ok=True)
    p = subprocess.run([sys.executable, "-m", "heardback.brain.mcp_jv"], input="".join(json.dumps(m) + "\n" for m in msgs),
                       capture_output=True, text=True, timeout=20, cwd=cwd, env=os.environ.copy())
    assert p.returncode == 0, p.stderr
    return [json.loads(l) for l in p.stdout.splitlines()]


def call(env, name: str, args: dict) -> dict:
    out = mcp(env, [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}])
    assert out[0]["id"] == 1
    return out[0]["result"]


def test_initialize_and_list(env):
    out = mcp(env, [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
    assert out[0]["result"]["protocolVersion"] == "2025-06-18" and len(out) == 2
    assert out[0]["result"]["serverInfo"] == {"name": "jv", "version": "1"}
    tools = {t["name"]: t for t in out[1]["result"]["tools"]}
    from heardback.brain.mcp_jv import DIRECT
    assert list(tools)[-3:] == ["jv_find", "jv_run", "ask_user"] and set(list(tools)[:-3]) <= set(DIRECT)
    assert "wall_next" not in tools and "tmux_type" in tools   # E2: a fixed list; other verbs go through jv_run
    assert tools["tmux_type"]["inputSchema"]["properties"]["target"]["type"] == "string"


def test_say_is_not_discovered_or_callable_by_the_model(env, monkeypatch):
    from jv.cli import catalogue as cli_catalogue
    from jv.say import VERBS as say_verbs
    from heardback.brain import catalogue, mcp_jv
    data = catalogue._fetch()
    legacy_say = cli_catalogue(say_verbs)["verbs"][0]
    legacy_say["voice"] = True
    data["verbs"].append(legacy_say)
    monkeypatch.setattr(catalogue, "_fetch", lambda: data)
    cat = catalogue.get(force=True)
    assert "say_say" not in {tool["name"] for tool in mcp_jv.tools(cat)}
    result = mcp_jv.call(cat, "say_say", {"text": "hello"})
    assert result["isError"] and result["content"][0]["text"] == "error: unknown tool"
    assert jv_calls(env) == [["list", "--json"]]


def test_call_argv_no_shell(env):   # R1: flags, extra flags, `--`, positionals
    r = call(env, "tmux_type", {"target": "2", "text": "run the tests; rm -rf ~", "enter": True})
    assert not r["isError"]
    assert jv_calls(env)[-1] == ["tmux", "type", "--enter", "--json", "--", "2", "run the tests; rm -rf ~"]


def test_source_revision_is_forwarded_without_payload(env):
    revision = "clipboard:" + "c" * 64
    call(env, "tmux_type", {"target": "2", "clipboard": True, "source_revision": revision})
    assert jv_calls(env)[-1] == ["tmux", "type", "--clipboard", "--source-revision", revision,
                                 "--json", "--", "2"]


def test_browsing_background_schema_and_argv_agree():
    from jv import zen
    from jv.cli import catalogue as cli_catalogue
    from heardback.brain import catalogue, mcp_jv
    cat = catalogue._index(cli_catalogue(zen.VERBS))
    schemas = {catalogue.tool_name(e): e["params"] for e in cat.values()}
    for name in ("zen.open", "zen.switch", "zen.search"):
        assert schemas[name.replace(".", "_")]["properties"]["background"]["type"] == "boolean"
        assert "--background" in catalogue.build_argv(cat[name], {"query": "gmail", "background": True})
        assert "--background" not in catalogue.build_argv(cat[name], {"query": "gmail", "background": False})
    revision = "zen:" + "d" * 64
    assert catalogue.build_argv(cat["zen.open"], {"query": "gmail", "background": True, "source_revision": revision}) == [
        "jv", "zen", "open", "--source-revision", revision, "--background", "--", "gmail"]


def test_safe_verb_has_no_yes(env):
    script(env, {"wall next": {"rc": 0, "stdout": '{"ok":true,"say":"Next wallpaper."}'}})
    r = call(env, "wall_next", {})
    assert jv_calls(env)[-1] == ["wall", "next", "--json"]
    assert r == {"content": [{"type": "text", "text": '{"ok":true,"say":"Next wallpaper."}'}], "isError": False}


def test_busy_session_refused(env):
    script(env, {"tmux type": {"rc": 4, "stdout": "", "stderr": "pane shows a permission prompt"}})
    r = call(env, "tmux_type", {"target": "2", "text": "x", "enter": True})
    assert r["isError"] and r["content"][0]["text"] == "error: pane shows a permission prompt"


def test_ambiguous_keeps_json(env):
    script(env, {"zen close": {"rc": 2, "stdout": '{"ok":false,"candidates":["A","B"]}'}})
    r = call(env, "zen_close", {"query": "x"})
    assert r["isError"] and json.loads(r["content"][0]["text"])["candidates"] == ["A", "B"]


def test_unknown_tool(env):
    r = call(env, "nope_nope", {})
    assert r == {"content": [{"type": "text", "text": "error: unknown tool"}], "isError": True}
    assert jv_calls(env) == [["list", "--json"]]


def test_timeout(env, monkeypatch):
    monkeypatch.setenv("HEARDBACK_JV_TIMEOUT_S", "0.2")
    script(env, {"wall next": {"sleep": 2}})
    assert call(env, "wall_next", {})["content"][0]["text"] == "error: timed out"


def test_unknown_method_and_ping(env):
    out = mcp(env, [{"jsonrpc": "2.0", "id": 3, "method": "foo/bar"}, {"jsonrpc": "2.0", "id": 4, "method": "ping"},
                    {"jsonrpc": "2.0", "method": "notifications/cancelled"}])
    assert out[0] == {"jsonrpc": "2.0", "id": 3, "error": {"code": -32601, "message": "unknown method"}}
    assert out[1] == {"jsonrpc": "2.0", "id": 4, "result": {}} and len(out) == 2


def test_confirm_verb_never_gets_yes(env):
    script(env, {"tmux type": {"rc": 3, "stdout": '{"ok":false,"confirm":"Type into pane 2?"}'}})
    r = call(env, "tmux_type", {"target": "2", "text": "x", "enter": True})
    assert jv_calls(env)[-1] == ["tmux", "type", "--enter", "--json", "--", "2", "x"]
    assert r["isError"] and json.loads(r["content"][0]["text"])["confirm"] == "Type into pane 2?"


def test_bad_params_keep_server_running(env):   # M3 review: one bad line must not kill the server
    out = mcp(env, [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": "x"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "zen_close", "arguments": 5}},
                    {"jsonrpc": "2.0", "id": 3, "method": "ping"}])
    assert out[0]["error"]["code"] == -32602
    assert out[1]["id"] == 2 and "result" in out[1]
    assert out[2] == {"jsonrpc": "2.0", "id": 3, "result": {}}


def test_revoked_attempt_blocks_preapproved_and_receipt_bound_old_calls(env, monkeypatch):
    from heardback.brain import catalogue, mcp_jv
    monkeypatch.setenv("HEARDBACK_EXECUTION_GATE", "1")
    monkeypatch.setenv("HEARDBACK_EXECUTION_OWNER", "test-owner")
    cat = catalogue.get()
    catalogue.set_current_turn("same-trace-id", owner="test-owner")
    old = catalogue.execution_snapshot()
    catalogue.block_turn(old[0])
    denied = mcp_jv.call(cat, "wall_next", {}, expected_turn=old[0], expected_attempt=old[1])
    assert denied["isError"] and not any(c[:2] == ["wall", "next"] for c in jv_calls(env))
    catalogue.set_current_turn("same-trace-id", owner="test-owner")
    fresh = catalogue.execution_snapshot()
    assert fresh != old
    assert mcp_jv.call(cat, "wall_next", {}, expected_turn=old[0], expected_attempt=old[1])["isError"]
    assert not mcp_jv.call(cat, "wall_next", {}, expected_turn=fresh[0], expected_attempt=fresh[1])["isError"]
    assert sum(c[:2] == ["wall", "next"] for c in jv_calls(env)) == 1


def test_missing_managed_gate_or_provenance_denies_launch(env, monkeypatch):
    from heardback.brain import catalogue, mcp_jv
    monkeypatch.setenv("HEARDBACK_EXECUTION_GATE", "1")
    assert mcp_jv.call(catalogue.get(), "wall_next", {})["isError"]
    assert not any(c[:2] == ["wall", "next"] for c in jv_calls(env))


def test_transport_queue_keeps_old_attempt_after_later_turn(env, monkeypatch):
    import io
    import queue
    from heardback.brain import catalogue, mcp_jv
    monkeypatch.setenv("HEARDBACK_EXECUTION_GATE", "1")
    monkeypatch.setenv("HEARDBACK_EXECUTION_OWNER", "test-owner")
    catalogue.set_current_turn("old", owner="test-owner")
    request = {"id": 2, "method": "tools/call", "params": {"name": "wall_next", "arguments": {}}}
    monkeypatch.setattr(mcp_jv.sys, "stdin", io.StringIO(json.dumps(request) + "\n"))
    inbox = queue.Queue()
    mcp_jv._read_requests(inbox)
    line, tid, attempt = inbox.get_nowait()
    catalogue.block_turn(tid)
    catalogue.set_current_turn("later-user-turn", owner="test-owner")
    result = mcp_jv.handle(json.loads(line), expected_turn=tid, expected_attempt=attempt)
    assert result["result"]["isError"]
    assert not any(c[:2] == ["wall", "next"] for c in jv_calls(env))


def test_managed_state_requires_explicit_allowed_and_string_identity(env, monkeypatch):
    from heardback.brain import catalogue, mcp_jv
    from jv.verb import runtime_path
    monkeypatch.setenv("HEARDBACK_EXECUTION_GATE", "1")
    monkeypatch.setenv("HEARDBACK_EXECUTION_OWNER", "test-owner")
    cat = catalogue.get()
    valid = {"turn": "turn", "attempt": "attempt", "owner": "test-owner", "allowed": True}
    invalid = [[], None, {**valid, "allowed": False}, {**valid, "allowed": "true"},
               {**valid, "allowed": 1}, {**valid, "turn": ""}, {**valid, "attempt": ""},
               {k: v for k, v in valid.items() if k != "allowed"},
               {**valid, "turn": 1}, {**valid, "attempt": ["attempt"]},
               {**valid, "owner": None}]
    for state in invalid:
        with open(runtime_path("heardback-execution.json"), "w") as f:
            json.dump(state, f)
        assert mcp_jv.call(cat, "wall_next", {}, expected_turn="turn", expected_attempt="attempt")["isError"]
    assert not any(c[:2] == ["wall", "next"] for c in jv_calls(env))


def test_surviving_old_mcp_cannot_adopt_replacement_process_attempt(env, monkeypatch):
    from heardback.brain import catalogue, mcp_jv
    monkeypatch.setenv("HEARDBACK_EXECUTION_GATE", "1")
    monkeypatch.setenv("HEARDBACK_EXECUTION_OWNER", "old-process")
    cat = catalogue.get()
    catalogue.set_current_turn("later-turn", owner="replacement-process")
    tid, attempt = catalogue.execution_snapshot()   # even a newly received call in the orphan is denied
    assert mcp_jv.call(cat, "wall_next", {}, expected_turn=tid, expected_attempt=attempt)["isError"]
    monkeypatch.setenv("HEARDBACK_EXECUTION_OWNER", "replacement-process")
    assert not mcp_jv.call(cat, "wall_next", {}, expected_turn=tid, expected_attempt=attempt)["isError"]
    assert sum(c[:2] == ["wall", "next"] for c in jv_calls(env)) == 1


def test_stale_revocation_cannot_revoke_same_trace_new_attempt(env):
    from heardback.brain import catalogue
    old = catalogue.set_current_turn("same", owner="owner")
    fresh = catalogue.set_current_turn("same", owner="owner")
    catalogue.block_turn("same", old)
    assert catalogue.execution_snapshot() == ("same", fresh)


def test_revocation_uses_gate_identity_when_trace_file_is_wrong(env):
    from heardback.brain import catalogue
    from jv.verb import runtime_path
    attempt = catalogue.set_current_turn("active", owner="owner")
    with open(runtime_path(catalogue.TURN_FILE), "w") as f:
        f.write("unrelated-trace")
    catalogue.block_turn("active", attempt)
    assert catalogue.execution_snapshot() == ("", "")


def test_revocation_read_io_failure_is_not_reported_as_success(env, monkeypatch):
    import builtins
    import pytest
    from heardback.brain import catalogue
    from jv.verb import runtime_path
    attempt = catalogue.set_current_turn("active", owner="owner")
    gate = runtime_path("heardback-execution.json")
    original_open = builtins.open
    def inaccessible(path, *args, **kwargs):
        if path == gate:
            raise PermissionError("gate temporarily unreadable")
        return original_open(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", inaccessible)
        assert catalogue.execution_snapshot() == ("", "")   # launch remains fail-closed
        with pytest.raises(PermissionError):
            catalogue.block_turn("active", attempt)
    assert catalogue.execution_snapshot() == ("active", attempt)   # retirement is required


def test_managed_launch_requires_complete_receipt_and_lock(env, monkeypatch):
    import contextlib
    from heardback.brain import catalogue, mcp_jv
    monkeypatch.setenv("HEARDBACK_EXECUTION_GATE", "1")
    monkeypatch.setenv("HEARDBACK_EXECUTION_OWNER", "owner")
    cat = catalogue.get()
    attempt = catalogue.set_current_turn("active", owner="owner")
    assert mcp_jv.call(cat, "wall_next", {}, expected_attempt=attempt)["isError"]
    @contextlib.contextmanager
    def failed_lock():
        raise OSError("lock unavailable")
        yield
    monkeypatch.setattr(catalogue, "execution_lock", failed_lock)
    assert mcp_jv.call(cat, "wall_next", {}, expected_turn="active", expected_attempt=attempt)["isError"]
    assert not any(c[:2] == ["wall", "next"] for c in jv_calls(env))


def test_command_wait_is_outside_admission_lock(env, monkeypatch):
    import contextlib
    from heardback.brain import catalogue, mcp_jv
    cat = catalogue.get()
    locked = False
    @contextlib.contextmanager
    def lock():
        nonlocal locked
        locked = True
        try:
            yield
        finally:
            locked = False
    class Process:
        returncode = 0
        def __init__(self, *args, **kwargs):
            assert locked
        def communicate(self, **kwargs):
            assert not locked
            return '{"ok":true}', ""
    monkeypatch.setattr(catalogue, "execution_lock", lock)
    monkeypatch.setattr(mcp_jv.subprocess, "Popen", Process)
    assert not mcp_jv.call(cat, "wall_next", {})["isError"]


def test_media_schema_and_mcp_argv_keep_explicit_source_and_revision(env, monkeypatch):
    from jv import media
    from jv.cli import catalogue as cli_catalogue
    from heardback.brain import catalogue, mcp_jv
    cat = catalogue._index(cli_catalogue(media.VERBS))
    schema = mcp_jv.tools(cat)[0]["inputSchema"]
    assert schema["properties"]["source"]["enum"] == ["desktop", "site"]
    revision = "media:desktop:" + "a" * 64
    result = mcp_jv.call(cat, "media_media", {"action": "previous", "target": "spotify", "source": "desktop",
                                            "player": "spotify.instance123", "source_revision": revision})
    assert not result["isError"]
    assert jv_calls(env)[-1] == ["media", "--target", "spotify", "--source", "desktop", "--player", "spotify.instance123",
                                 "--source-revision", revision, "--json", "--", "previous"]


def test_replay_records_and_spawns_nothing(env, tmp_path, monkeypatch):
    from heardback.brain import mcp_jv
    log = tmp_path / "replay.jsonl"
    monkeypatch.setenv("HEARDBACK_REPLAY", "1")
    monkeypatch.setenv("HEARDBACK_REPLAY_LOG", str(log))
    cat = {"tmux.type": {"name": "tmux.type", "domain": "tmux", "verb": "type", "argv": ["jv", "tmux", "type"],
                         "params": {}, "description": "d"}}
    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a))
    r = mcp_jv.call(cat, "tmux_type", {"text": "ls"})
    assert json.loads(r["content"][0]["text"]) == {"ok": True, "say": "done"} and not spawned
    assert [json.loads(l) for l in log.read_text().splitlines()] == [{"name": "tmux_type", "args": {"text": "ls"}}]
    found = json.loads(mcp_jv.call(cat, "jv_find", {"query": "type"})["content"][0]["text"])   # E2: real answer
    assert found["verbs"][0]["verb"] == "tmux_type" and not spawned
