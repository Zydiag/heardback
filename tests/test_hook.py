import pytest

from heardback.status.hook import transition


def test_session_start_fills_fields():
    r = transition({"hook_event_name": "SessionStart", "session_id": "a", "cwd": "/x/career",
                    "transcript_path": "/t.jsonl"}, None, 0)
    assert r["state"] == "idle" and r["project"] == "career" and r["background"] == 0
    assert r["session_id"] == "a" and r["transcript_path"] == "/t.jsonl" and r["updated_at"]


def test_post_tool_use_only_from_waiting():
    assert transition({"hook_event_name": "PostToolUse", "session_id": "a"}, {"state": "working"}, 0) is None
    r = transition({"hook_event_name": "PostToolUse", "session_id": "a"}, {"state": "waiting", "waiting_for": "x"}, 0)
    assert r["state"] == "working" and r["waiting_for"] is None


def test_idle_prompt_silent_only_from_working():
    ev = {"hook_event_name": "Notification", "notification_type": "idle_prompt", "session_id": "a"}
    assert transition(ev, {"state": "idle"}, 0) is None
    assert transition(ev, {"state": "working"}, 0)["state"] == "idle"


@pytest.mark.parametrize("kind", ["permission_prompt", "elicitation_dialog", "agent_needs_input"])
def test_notification_waiting_sets_waiting_for(kind):
    r = transition({"hook_event_name": "Notification", "notification_type": kind, "session_id": "a",
                    "message": "Claude needs your permission to use Bash"}, {"state": "working"}, 0)
    assert r["state"] == "waiting" and r["waiting_for"] == "Claude needs your permission to use Bash"


def test_stop():
    r = transition({"hook_event_name": "Stop", "session_id": "a", "last_assistant_message": "Done.",
                    "background_tasks": [{}, {}]}, {"state": "working", "waiting_for": "x"}, 0)
    assert r["state"] == "idle" and r["last_reply"] == "Done." and r["background"] == 2 and r["waiting_for"] is None


def test_stop_failure_reply():
    r = transition({"hook_event_name": "StopFailure", "session_id": "a", "error": "rate_limit"}, {"state": "working"}, 0)
    assert r["last_reply"] == "stopped with an error: rate limit"


def test_prompt_truncated():
    r = transition({"hook_event_name": "UserPromptSubmit", "session_id": "a", "prompt": "x" * 500}, None, 0)
    assert r["state"] == "working" and len(r["last_prompt"]) == 80
    assert r["session_id"] == "a" and r["turn_started_at"]


def test_session_end_ended():
    r = transition({"hook_event_name": "SessionEnd", "session_id": "a", "reason": "clear"}, {"state": "idle"}, 0)
    assert r["state"] == "ended" and r["end_reason"] == "clear"


def test_unknown_event_none():
    assert transition({"hook_event_name": "Bogus", "session_id": "a"}, {"state": "idle"}, 0) is None
    assert transition({"hook_event_name": "Notification", "notification_type": "other", "session_id": "a"},
                      {"state": "working"}, 0) is None


def test_does_not_mutate_cur():
    cur = {"state": "waiting", "waiting_for": "x"}
    transition({"hook_event_name": "PostToolUse", "session_id": "a"}, cur, 0)
    assert cur == {"state": "waiting", "waiting_for": "x"}


@pytest.mark.parametrize("text,want", [
    ("Done: the draft is in letters/acme.md. Next I'll…", "Done: the draft is in letters/acme.md."),
    ("**Fixed** the `parse()` bug.\n\n```py\nx=1\n```", "Fixed the parse() bug."),
    ("## Summary\n- added export button\n- tests pass", "Summary added export button tests pass"),
    ("See [the docs](http://x.y/z). More.", "See the docs."),
    ("Here:\n```py\nunterminated", "Here:"),
    (None, ""),
])
def test_first_sentence(text, want):
    from heardback.status.hook import first_sentence
    assert first_sentence(text) == want


def test_first_sentence_cut():
    from heardback.status.hook import first_sentence
    s = first_sentence("a " * 200)
    assert len(s) <= 121 and s.endswith("…")


def _run(tmp_path, ev, **env):
    import json, os, subprocess, sys
    data = ev if isinstance(ev, str) else json.dumps(ev)
    return subprocess.run([sys.executable, "-m", "heardback.status.hook"], input=data.encode(), capture_output=True,
                          env={**os.environ, "XDG_STATE_HOME": str(tmp_path), **env}, timeout=10)


def _events(tmp_path):
    import json
    p = tmp_path / "heardback" / "events.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def _session(tmp_path, sid="s1"):
    import json
    return json.loads((tmp_path / "heardback" / "sessions" / f"{sid}.json").read_text())


def test_prompt_then_stop_files_and_events(tmp_path):
    _run(tmp_path, {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": "/x/career", "prompt": "hi"})
    r = _run(tmp_path, {"hook_event_name": "Stop", "session_id": "s1", "last_assistant_message": "All done. More."})
    assert r.returncode == 0 and r.stdout == b""
    s = _session(tmp_path)
    assert s["state"] == "idle" and s["last_reply"] == "All done." and s["project"] == "career"
    ev = _events(tmp_path)
    assert [e["to"] for e in ev] == ["working", "idle"]
    assert ev[0]["from"] is None and ev[0]["turn_s"] is None
    assert ev[1]["from"] == "working" and ev[1]["turn_s"] >= 0 and ev[1]["hook"] == "Stop"
    assert ev[1]["detail"] == "All done." and ev[1]["project"] == "career"


@pytest.mark.parametrize("stdin", ["", "not json", "[1,2]", '{"hook_event_name":"Bogus","session_id":"x"}',
                                   '{"hook_event_name":"Stop"}', '{"hook_event_name":"Stop","session_id":"../x"}'])
def test_no_stdout_exit0(tmp_path, stdin):
    r = _run(tmp_path, stdin)
    assert r.returncode == 0 and r.stdout == b""
    assert not list(tmp_path.rglob("*.json"))


def test_brain_guard(tmp_path):
    r = _run(tmp_path, {"hook_event_name": "UserPromptSubmit", "session_id": "s1"}, HEARDBACK_BRAIN="1")
    assert r.returncode == 0 and r.stdout == b""
    assert list(tmp_path.iterdir()) == []


def test_ordering_rule(tmp_path, monkeypatch):
    import time
    from heardback.status import store
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    store.write_session({"session_id": "s1", "state": "working", "updated_at": store.now_iso(time.time() + 60)})
    before = store.session_path("s1").read_text()
    _run(tmp_path, {"hook_event_name": "Stop", "session_id": "s1", "last_assistant_message": "x."})
    assert store.session_path("s1").read_text() == before and _events(tmp_path) == []


def test_no_event_when_state_same(tmp_path):
    ev = {"hook_event_name": "Notification", "notification_type": "permission_prompt", "session_id": "s1",
          "message": "Claude needs your permission to use Bash"}
    _run(tmp_path, ev)
    _run(tmp_path, ev)
    (e,) = _events(tmp_path)
    assert e["to"] == "waiting" and e["detail"] == "Claude needs your permission to use Bash"


def test_session_start_tmux(tmp_path, tmux_server):
    pane = tmux_server("display", "-p", "-t", "0", "#{pane_id}").strip()
    ev = {"hook_event_name": "SessionStart", "session_id": "s1", "cwd": "/x/heardback", "source": "startup"}
    _run(tmp_path, ev, TMUX_PANE=pane)
    s = _session(tmp_path)
    assert s["tmux_pane"] == pane and s["tmux_target"] == "0:0.0" and s["state"] == "idle"
    assert "claude_pid" in s
    _run(tmp_path, {**ev, "session_id": "s2"})
    s2 = _session(tmp_path, "s2")
    assert s2["tmux_pane"] is None and s2["tmux_target"] is None


def test_find_claude_pid_none():
    from heardback.status.hook import find_claude_pid
    assert find_claude_pid(1) is None and find_claude_pid(10**9) is None


def test_each_hook_rebinds_session_to_its_own_pane(tmp_path, monkeypatch):
    from heardback.status import hook, store
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(store, "resolve_target", lambda pane, _socket: f"window-{pane}")
    pid = [1]
    monkeypatch.setattr(hook, "find_claude_pid", lambda _ppid: pid[0])

    hook.handle({"hook_event_name": "SessionStart", "session_id": "shared", "cwd": "/x/p"}, 1,
                {"TMUX_PANE": "%A"})
    pid[0] = 2
    hook.handle({"hook_event_name": "SessionStart", "session_id": "shared", "cwd": "/x/p"}, 2,
                {"TMUX_PANE": "%B"})
    pid[0] = 1
    hook.handle({"hook_event_name": "UserPromptSubmit", "session_id": "shared", "cwd": "/x/p"}, 3,
                {"TMUX_PANE": "%A"})

    session = store.read_raw("shared")
    assert (session["tmux_pane"], session["tmux_target"], session["claude_pid"]) == (
        "%A", "window-%A", 1)


def test_hook_identity_refresh_rules(tmp_path, monkeypatch):
    from heardback.status import hook, store
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    calls = []
    monkeypatch.setattr(store, "resolve_target", lambda pane, _socket: calls.append(pane) or f"window-{pane}")
    pid = [1]
    monkeypatch.setattr(hook, "find_claude_pid", lambda _ppid: pid[0])
    start = {"hook_event_name": "SessionStart", "session_id": "s", "cwd": "/x/p"}
    hook.handle(start, 1, {"TMUX_PANE": "%A"})

    hook.handle({"hook_event_name": "UserPromptSubmit", "session_id": "s"}, 2, {})
    assert store.read_raw("s")["tmux_pane"] == "%A"
    assert calls == ["%A"]

    hook.handle({"hook_event_name": "UserPromptSubmit", "session_id": "s"}, 3, {"TMUX_PANE": "%A"})
    assert calls == ["%A"]

    pid[0] = None
    hook.handle({"hook_event_name": "UserPromptSubmit", "session_id": "s"}, 4, {"TMUX_PANE": "%B"})
    assert store.read_raw("s")["claude_pid"] == 1
    assert store.read_raw("s")["tmux_pane"] == "%B"
    assert calls == ["%A", "%B"]


def test_errors_logged(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir(mode=0o500)
    r = _run(ro, {"hook_event_name": "UserPromptSubmit", "session_id": "s1"})
    assert r.returncode == 0 and r.stdout == b""


def test_errors_log_written(tmp_path):
    (tmp_path / "heardback" / "sessions").mkdir(parents=True)
    (tmp_path / "heardback" / "sessions" / "s1.json").mkdir()  # makes the write fail
    r = _run(tmp_path, {"hook_event_name": "UserPromptSubmit", "session_id": "s1"})
    assert r.returncode == 0 and r.stdout == b""
    assert "Traceback" in (tmp_path / "heardback" / "hook-errors.log").read_text()


def test_session_start_claude_pid(tmp_path):
    import json, os, shutil, subprocess, sys
    fake = tmp_path / "bin" / "claude"  # a shell named claude, so /proc/<pid>/comm == "claude"
    fake.parent.mkdir()
    shutil.copy("/bin/sh", fake)
    ev = json.dumps({"hook_event_name": "SessionStart", "session_id": "s1", "cwd": "/x/p"})
    p = subprocess.Popen([fake, "-c", f"{sys.executable} -m heardback.status.hook; true"], stdin=subprocess.PIPE,
                         env={**os.environ, "XDG_STATE_HOME": str(tmp_path / "st")})
    p.communicate(ev.encode(), timeout=10)
    assert _session(tmp_path / "st")["claude_pid"] == p.pid


def test_first_hook_not_session_start_fills_pid(tmp_path):
    import json, os, shutil, subprocess, sys
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    shutil.copy("/bin/sh", fake)
    ev = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": "/x/p", "prompt": "hi"})
    p = subprocess.Popen([fake, "-c", f"{sys.executable} -m heardback.status.hook; true"], stdin=subprocess.PIPE,
                         env={**os.environ, "XDG_STATE_HOME": str(tmp_path / "st")})
    p.communicate(ev.encode(), timeout=10)
    s = _session(tmp_path / "st")
    assert s["claude_pid"] == p.pid and s["state"] == "working"


def test_bad_updated_at_recovers(tmp_path, monkeypatch):
    from heardback.status import speak, store
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    store.write_session({"session_id": "s1", "state": "working", "updated_at": "garbage", "turn_started_at": 5})
    assert store.epoch("garbage") == 0.0 and store.epoch(5) == 0.0
    live = {"session_id": "s1", "project": "p", "state": "working", "updated_at": "garbage", "turn_started_at": 5}
    assert speak.overview([live], 100) == "1 live local Claude Code session. P is working, 1 minute in."
    assert speak.one({**live, "state": "idle", "last_reply": "ok"}, [live], 100).startswith("P finished")
    _run(tmp_path, {"hook_event_name": "Stop", "session_id": "s1", "last_assistant_message": "x."})
    assert _session(tmp_path)["state"] == "idle"


def test_ordering_rule_subsecond(tmp_path, monkeypatch):
    import time
    from heardback.status import store
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    store.write_session({"session_id": "s1", "state": "working", "updated_at": store.now_iso(time.time() + 0.5)})
    before = store.session_path("s1").read_text()
    _run(tmp_path, {"hook_event_name": "Stop", "session_id": "s1", "last_assistant_message": "x."})
    assert store.session_path("s1").read_text() == before


@pytest.mark.parametrize("state", ["working", "waiting"])
def test_session_start_keeps_busy_state(state):
    cur = {"session_id": "a", "state": state, "waiting_for": "w", "last_reply": "r"}
    r = transition({"hook_event_name": "SessionStart", "session_id": "a", "cwd": "/x/p", "source": "compact"}, cur, 0)
    assert r["state"] == state and r["waiting_for"] == "w" and r["last_reply"] == "r" and r["project"] == "p"


def test_turn_s_none_without_start(tmp_path, monkeypatch):
    from heardback.status import store
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    store.write_session({"session_id": "s1", "state": "working", "updated_at": store.now_iso(0)})
    _run(tmp_path, {"hook_event_name": "Stop", "session_id": "s1", "last_assistant_message": "x."})
    (e,) = _events(tmp_path)
    assert e["to"] == "idle" and e["turn_s"] is None


def test_error_log_mode(tmp_path):
    (tmp_path / "heardback" / "sessions").mkdir(parents=True)
    (tmp_path / "heardback" / "sessions" / "s1.json").mkdir()
    _run(tmp_path, {"hook_event_name": "UserPromptSubmit", "session_id": "s1"})
    assert (tmp_path / "heardback" / "hook-errors.log").stat().st_mode & 0o777 == 0o600
    assert not list((tmp_path / "heardback" / "sessions").glob(".*.tmp"))  # failed write leaves no tmp file


def test_error_log_appends_then_truncates(tmp_path):
    (tmp_path / "heardback" / "sessions").mkdir(parents=True)
    (tmp_path / "heardback" / "sessions" / "s1.json").mkdir()
    log = tmp_path / "heardback" / "hook-errors.log"
    ev = {"hook_event_name": "UserPromptSubmit", "session_id": "s1"}
    _run(tmp_path, ev)
    _run(tmp_path, ev)
    assert log.read_text().count("Traceback") == 2
    log.write_text("x" * 100_001)
    _run(tmp_path, ev)
    assert log.read_text().count("Traceback") == 1 and "xxx" not in log.read_text()
