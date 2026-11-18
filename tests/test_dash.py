import http.client
import json
import os
import time
from types import SimpleNamespace
from datetime import datetime

import pytest

from heardback.dash import server, sources
from heardback.status import store

NVIDIA = """#!/bin/sh
case "$1" in
  --query-gpu=*) echo "NVIDIA GeForce RTX 5060 Laptop GPU, 7, 2860, 8151" ;;
  --query-compute-apps=*) echo "$FAKE_PID, 2850"; echo "999999, 10" ;;
esac
"""
CODEBURN = "#!/bin/sh\nprintf '\\n  \\033[1mToday\\033[0m  $17.50  323 calls    Month  $1,038.84  21750 calls\\n'\n"


def _tool(d, name, body):
    p = d / name
    p.write_text(body)
    p.chmod(0o755)


@pytest.fixture
def fakes(tmp_path, state_dir, monkeypatch):
    # Daemon tests publish the process's live objects; each dashboard test owns a clean view.
    sources.LIVE.clear()
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    _tool(bin_, "nvidia-smi", NVIDIA)
    _tool(bin_, "codeburn", CODEBURN)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_PID", str(os.getpid()))
    monkeypatch.setattr(store, "alive", lambda pid: True)
    t = tmp_path / "t.jsonl"
    usage = {"input_tokens": 2, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 74000, "output_tokens": 9}
    t.write_text("\n".join(json.dumps(x) for x in [
        {"type": "assistant", "message": {"model": "claude-opus-5-5", "usage": usage}},
        {"type": "assistant", "isSidechain": True, "message": {"model": "claude-haiku-5-5", "usage": {"input_tokens": 5}}},
        {"type": "user", "message": {"content": "hi"}}]) + "\n")
    store.write_session({**dict.fromkeys(store.FIELDS), "session_id": "abcd1234-x", "claude_pid": 4242, "cwd": "/w/heardback",
                         "project": "heardback", "state": "working", "transcript_path": str(t),
                         "updated_at": store.now_iso()})
    today = datetime.now().astimezone().isoformat(timespec="milliseconds")
    (state_dir.parent / "trace.jsonl").write_text("\n".join(json.dumps(x) for x in [
        {"ts": "2026-01-01T10:00:00.000+05:30", "src": "brain", "ev": "result", "turn": "old", "tokens": 1, "cost": 9},
        {"ts": today, "src": "brain", "ev": "claude_spawn", "turn": None, "pid": 77},
        {"ts": today, "src": "brain", "ev": "result", "turn": "t1", "tokens": 9000, "output_tokens": 40, "cost": 0.002,
         "ms": 1800, "tools": ["tmux_list"], "model": "haiku"},
        {"ts": today, "src": "brain", "ev": "claude_reset", "turn": "t1", "reason": "tokens", "pid": 77},
        "not json"]) + "\n")
    sources.spend.clear()
    sources.gpu.clear()
    srv = server.start(0)
    yield srv
    srv.shutdown()
    srv.server_close()
    sources.LIVE.clear()


def _req(srv, method="GET", path="/api/state", host=None):
    port = srv.server_address[1]
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, headers={"Host": host or f"127.0.0.1:{port}"})
    r = c.getresponse()
    return r.status, r.getheader("Content-Type"), r.read()


def test_state_shape_from_fakes(fakes):
    code, ctype, body = _req(fakes)
    assert code == 200 and ctype == "application/json"
    s = json.loads(body)
    (row,) = s["sessions"]["rows"]
    assert row["project"] == "heardback" and row["state"] == "working"
    assert row["ctx_tokens"] == 75002 and row["ctx_window"] == 1_000_000 and row["model"] == "claude-opus-5-5"
    assert s["sessions"]["spend"] == {"today_usd": 17.5, "calls": 323, "month_usd": 1038.84}
    assert s["sessions"]["limits"]["source"] is None
    b = s["brain"]
    assert b["today"]["turns"] == 1 and b["today"]["cost"] == 0.002 and b["today"]["tokens"] == 9000
    assert b["recent"][0]["tools"] == ["tmux_list"]
    assert [e["ev"] for e in b["lifecycle"]] == ["claude_reset", "claude_spawn"] and b["lifecycle"][0]["reason"] == "tokens"
    g = s["service"]["gpu"]
    assert g == {"name": "NVIDIA GeForce RTX 5060 Laptop GPU", "util_pct": 7, "used_mib": 2860, "total_mib": 8151,
                 "ours_mib": 2850}
    assert s["service"]["pid"] == os.getpid() and s["service"]["voice"] is False


def test_task_panel_removed(fakes):
    assert "tasks" not in json.loads(_req(fakes)[2])
    assert b'data-view="tasks"' not in _req(fakes, path="/")[2]


def test_rate_limit_from_brain(fakes):
    class Rate:
        last_info = {"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.42, "resetsAt": 1793450000}}}
        last_at = 1793440000.0

    sources.LIVE["brain"] = type("B", (), {"rate": Rate(), "model": "haiku", "claude_off": None, "claude": None,
                                           "proc_turns": 3, "last_cost": 0.01})()
    s = json.loads(_req(fakes)[2])
    assert s["sessions"]["limits"]["five_hour_pct"] == 42.0 and s["sessions"]["limits"]["resets_at"] == 1793450000
    assert s["brain"]["live"]["proc_turns"] == 3


def test_brain_dashboard_keeps_unobserved_model_unknown(fakes):
    sources.LIVE["brain"] = SimpleNamespace(model=None, claude_off=None, claude=None,
                                             proc_turns=0, last_cost=0.0)
    state = json.loads(_req(fakes)[2])
    assert state["brain"]["live"]["model"] is None
    page = _req(fakes, path="/")[2]
    assert b"l.model ?? NA" in page


def test_failing_source_is_a_panel_error(fakes, monkeypatch):
    def boom():
        raise RuntimeError("trace unreadable")

    monkeypatch.setitem(server.PANELS, "brain", boom)
    monkeypatch.setenv("PATH", "/nonexistent")   # codeburn + nvidia-smi gone
    sources.spend.clear()
    sources.gpu.clear()
    code, _, body = _req(fakes)
    s = json.loads(body)
    assert code == 200
    assert s["brain"] == {"error": "RuntimeError: trace unreadable"}
    assert "FileNotFoundError" in s["sessions"]["spend"]["error"] and s["sessions"]["rows"]
    assert "error" in s["service"]["gpu"] and s["service"]["pid"] == os.getpid()


def test_tool_failure_is_cached(fakes, monkeypatch, tmp_path):
    calls = tmp_path / "calls"
    _tool(tmp_path / "bin", "codeburn", f"#!/bin/sh\necho x >> {calls}\nexit 3\n")
    sources.spend.clear()
    for _ in range(3):
        with pytest.raises(RuntimeError, match="rc=3"):
            sources.spend()
    assert calls.read_text().count("x") == 1


def test_page_and_methods(fakes):
    code, ctype, body = _req(fakes, path="/")
    assert code == 200 and ctype.startswith("text/html") and b"/api/state" in body
    for m in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
        assert _req(fakes, m)[0] == 405
    assert _req(fakes, path="/etc/passwd")[0] == 404
    assert _req(fakes, path="/api/tree")[0] == 404
    assert _req(fakes, host="evil.example:80")[0] == 403   # DNS rebinding


def test_page_keeps_every_panel_and_stays_self_contained(fakes):
    page = _req(fakes, path="/")[2].decode()
    for panel in ("voice-dock", "live", "mic-state", "fresh", "connection-note", "dictation", "friction", "turns",
                  "sessions", "brain", "service"):
        assert f'id="{panel}"' in page
    assert "http://" not in page and "https://" not in page and "@import" not in page and "<link" not in page
    assert 'id="tree"' not in page and "/api/tree" not in page
    assert "prefers-reduced-motion" in page and ":focus-visible" in page


def test_binds_loopback_only(fakes):
    assert fakes.server_address[0] == "127.0.0.1"


def test_turns_panel_uses_traceview(fakes, state_dir):
    tr = state_dir.parent / "trace.jsonl"
    evs = [
        {"ts": "2026-10-31T14:58:33.000+05:30", "src": "voice", "ev": "heard", "turn": "v1", "text": "open zen", "stt_ms": 400},
        {"ts": "2026-10-31T14:58:33.100+05:30", "src": "brain", "ev": "route", "turn": "v1", "route": "fast",
         "verb": "zen.open", "args": {}},
        {"ts": "2026-10-31T14:58:35.000+05:30", "src": "voice", "ev": "turn_end", "turn": "v1", "outcome": "done",
         "stt_s": 0.4, "first_audio_s": 1.9, "total_s": 3.0},
        {"ts": "2026-10-31T14:59:00.000+05:30", "src": "voice", "ev": "heard", "turn": "v2", "text": "type it", "stt_ms": 500},
        {"ts": "2026-10-31T14:59:00.100+05:30", "src": "brain", "ev": "route", "turn": "v2", "route": "claude", "model": "haiku"},
        {"ts": "2026-10-31T14:59:02.000+05:30", "src": "brain", "ev": "permission", "turn": "v2", "tool": "tmux_type",
         "decision": "deny", "dry_run_rc": 4, "reason": "pane is showing a prompt"},
        {"ts": "2026-10-31T14:59:05.000+05:30", "src": "voice", "ev": "turn_end", "turn": "v2", "outcome": "done",
         "stt_s": 0.5, "first_audio_s": 4.2, "total_s": 6.0}]
    with tr.open("a") as f:
        f.write("".join(json.dumps(e) + "\n" for e in evs))
    t = json.loads(_req(fakes)[2])["turns"]
    ids = [r["id"] for r in t["rows"]]
    assert ids.index("v2") < ids.index("v1")                      # newest first
    v2 = next(r for r in t["rows"] if r["id"] == "v2")
    assert v2["bad"] and v2["route"] == "claude" and v2["first_audio_s"] == 4.2 and len(v2["events"]) == 4
    assert "DENIED" in " ".join(v2["lines"]) and '"type it"' in v2["lines"][0]
    assert v2["kinds"] == ["denied"] and v2["story"]["heard"] == "type it" and v2["story"]["think_s"] == 3.7
    v1 = next(r for r in t["rows"] if r["id"] == "v1")
    assert v1["kinds"] == [] and not v1["bad"] and v1["story"]["verb"] == "zen.open"
    assert t["latency"]["fast"] == {"n": 1, "first_audio_s": 1.9, "stt_s": 0.4}
    assert t["latency"]["claude"]["first_audio_s"] == 4.2


def test_rate_guard_keeps_last_info():
    from heardback.brain.usage import RateGuard
    g = RateGuard()
    g.update({"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.1, "resetsAt": 1}}})
    assert g.last_info["status"] == "allowed" and g.last_at > 0


def test_dash_port_config(tmp_path):
    from heardback.voice import config
    p = tmp_path / "voice.toml"
    assert config.load(p).dash_port == 8787
    p.write_text("dash_port = 0\n")
    assert config.load(p).dash_port == 0
    for bad in ('"8787"', "8787.0", "true", "70000"):
        p.write_text(f"dash_port = {bad}\n")
        with pytest.raises(ValueError, match="dash_port"):
            config.load(p)


def test_heardbackd_dash_off_and_bad_config(monkeypatch, tmp_path, caplog):
    from heardback import daemon
    cfg = tmp_path / "heardback" / "voice.toml"
    cfg.parent.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg.write_text("dash_port = 0\n")
    assert daemon._dash() is None
    cfg.write_text("dash_port = 99999\n")
    assert daemon._dash() is None and "dash off" in caplog.text


def _ev(ts, src, ev, turn, **kw):
    return {"ts": f"2026-10-31T15:00:{ts:06.3f}+05:30", "src": src, "ev": ev, "turn": turn, **kw}


def test_turn_story_and_kinds():
    evs = [_ev(0, "voice", "listen", "a", followup=False), _ev(1, "voice", "heard", "a", text="open htop", stt_ms=400),
           _ev(1.1, "brain", "route", "a", route="claude", model="haiku", parent="p0"),
           _ev(1.5, "brain", "tool_use", "a", name="hypr_open"),
           _ev(1.6, "jv", "run", "a", argv=["hypr", "open", "htop"], rc=1, dry_run=False),
           _ev(1.7, "brain", "tool_result", "a", name="hypr_open", is_error=False),
           _ev(1.8, "brain", "permission", "a", tool="tmux_new", decision="deny", dry_run_rc=3, reason="said-no"),
           _ev(1.9, "brain", "result", "a", cost=0.0003, tokens=11000),
           _ev(2, "voice", "say", "a", text="No match.", expect_reply=True),
           _ev(3, "voice", "turn_end", "a", outcome="done", stt_s=0.4, first_audio_s=2.5, total_s=3.0)]
    st = sources.story(evs)
    assert st["heard"] == "open htop" and st["route"] == "claude" and st["model"] == "haiku" and st["parent"] == "p0"
    assert st["acts"] == [{"name": "hypr_open", "ok": False, "rc": 1, "note": None},
                          {"name": "tmux_new", "ok": False, "rc": 3, "note": "denied: said-no"}]
    assert st["said"] == "No match." and st["asked"] and st["think_s"] == 2.1 and st["cost"] == 0.0003
    assert sources.kinds(evs) == ["confirm_no", "jv_rc"]
    fast = [_ev(0, "brain", "route", "f", route="fast", verb="zen.open"),
            _ev(0.1, "brain", "jv", "f", argv=["jv", "zen", "open", "--dry-run"], rc=4),
            _ev(0.2, "brain", "jv", "f", argv=["jv", "zen", "open", "x"], rc=0),
            _ev(0.3, "brain", "reply", "f", spoken=["Opened."]),
            _ev(1, "voice", "turn_end", "f", outcome="done")]
    st = sources.story(fast)
    assert st["acts"] == [{"name": "zen open", "ok": True, "rc": 0, "note": None}] and st["said"] == "Opened."
    assert sources.kinds(fast) == []                                 # a dry-run refusal alone isn't friction
    nothing = [_ev(0, "voice", "listen", "n", followup=False), _ev(4, "voice", "turn_end", "n", outcome="nothing")]
    assert sources.kinds(nothing) == ["no_speech"]
    unanswered = [_ev(0, "voice", "listen", "u", followup=True), _ev(9, "voice", "turn_end", "u", outcome="nothing")]
    assert sources.kinds(unanswered) == []                           # a follow-up nobody answered
    for ev, kw, k in [("reject", {"why": "empty_stt"}, "empty_stt"), ("reject", {"why": "speaker"}, "reject"),
                      ("stt_timeout", {}, "stt_timeout"), ("confirm_heard", {"answer": "timeout"}, "confirm_timeout"),
                      ("stuck", {}, "stuck"), ("brain_lost", {}, "error"), ("reply", {"why": "rate limit"}, "claude_limit"),
                      ("permission", {"decision": "deny", "reason": "pane is showing a prompt"}, "denied"),
                      ("tool_result", {"is_error": True, "name": "x"}, "tool_error")]:
        assert sources.kinds([_ev(0, "voice", ev, "k", **kw)]) == [k], ev


def test_confirm_reply_folds_into_its_turn(fakes, state_dir):
    with (state_dir.parent / "trace.jsonl").open("a") as f:
        f.write("".join(json.dumps(e) + "\n" for e in [
            _ev(0, "voice", "heard", "20261031-150000-2", text="open a session"),
            _ev(5, "voice", "confirm_heard", "20261031-150000-2", answer="yes", text="yes"),
            _ev(5.1, "brain", "confirm_reply", "20261031-150005-2c", parent="20261031-150000-2", ok=True)]))
    rows = json.loads(_req(fakes)[2])["turns"]["rows"]
    assert not any(r["id"].endswith("c") for r in rows)
    r = next(r for r in rows if r["id"] == "20261031-150000-2")
    assert r["events"][-1]["ev"] == "confirm_reply" and r["story"]["acts"][-1]["name"] == "answer yes"


def test_friction_today(fakes, state_dir):
    from datetime import date
    d = date.today().strftime("%Y%m%d")
    old = [_ev(0, "voice", "reject", "20200101-100000-1", why="speaker")]          # not today: not counted
    ok = [_ev(1, "voice", "heard", f"{d}-150001-1", text="hi"), _ev(2, "voice", "turn_end", f"{d}-150001-1", outcome="done")]
    bad = [_ev(3, "voice", "heard", f"{d}-150003-2", text="x"), _ev(3.5, "voice", "stuck", f"{d}-150003-2"),
           _ev(3.6, "brain", "jv", f"{d}-150003-2", argv=["jv", "hypr", "open"], rc=1),
           _ev(4, "voice", "turn_end", f"{d}-150003-2", outcome="timeout")]
    with (state_dir.parent / "trace.jsonl").open("a") as f:
        f.write("".join(json.dumps(e) + "\n" for e in old + ok + bad))
    t = json.loads(_req(fakes)[2])["turns"]["today"]
    assert t["turns"] == 2 and t["ok"] == 1 and t["success_pct"] == 50
    assert t["friction"]["stuck"] == [f"{d}-150003-2"] and t["friction"]["jv_rc"] == [f"{d}-150003-2"]
    assert t["friction"]["reject"] == [] and list(t["friction"]) == list(sources.FRICTION)


def test_live_voice_state(fakes, state_dir):
    pytest.importorskip("numpy")
    from heardback.voice.loop import Voice
    v = Voice.__new__(Voice)              # just the state property: no models, no audio
    v.state = "IDLE"
    t0 = v.state_since
    v.state = "IDLE"
    assert v.state_since == t0            # same state: no new stamp
    v.tid, v.followup, v.mic_on, v.automute = "20261031-150009-3", False, True, False
    v.dictation_dashboard = lambda: None  # lightweight state-only fake; real Voice initializes Dictation
    sources.LIVE["voice"] = v
    s = json.loads(_req(fakes)[2])
    assert s["live"]["state"] == "idle" and s["live"]["turn"] is None
    v.state = "THINK"
    with (state_dir.parent / "trace.jsonl").open("a") as f:
        f.write(json.dumps(_ev(9, "voice", "heard", v.tid, text="hi")) + "\n")
    s = json.loads(_req(fakes)[2])
    assert s["live"]["state"] == "think" and s["live"]["turn"] == v.tid and s["live"]["since"] >= t0
    row = next(r for r in s["turns"]["rows"] if r["id"] == v.tid)
    assert row["live"] and row["id"] not in sum(s["turns"]["today"]["friction"].values(), [])
    sources.LIVE.pop("voice")
    assert json.loads(_req(fakes)[2])["live"] == {"state": None}


def test_dictation_preview_is_read_only_dashboard_text_and_can_clear(fakes):
    preview = {"session_id": "a" * 32, "phase": "review", "text": "<script>private words</script>",
               "target": "1:0.0", "pane_id": "%1", "revision": 2, "provisional": False}
    voice = SimpleNamespace(state="idle", state_since=time.time(), tid=None, followup=False, mic_on=True,
                            automute=False, dictation_dashboard=lambda: preview)
    sources.LIVE["voice"] = voice
    state = json.loads(_req(fakes)[2])
    assert state["live"]["dictation"] == preview
    voice.dictation_dashboard = lambda: None
    assert json.loads(_req(fakes)[2])["live"]["dictation"] is None
    page = _req(fakes, path="/")[2].decode()
    renderer = page.split("function dictation(d) {", 1)[1].split("function ", 1)[0]
    assert 'text: String(d.text ?? "")' in renderer
    assert 'const el = $(`#dictation-${name}`)' in renderer and 'el.textContent = value' in renderer
    assert 'el.textContent = ""' in renderer and '"text"' in renderer
    assert "innerHTML" not in renderer
    assert 'data-dictation-action' not in page


def _post_note(srv, data=None, *, body=None, headers=None):
    port = srv.server_address[1]
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
    try:
        c.request("POST", "/api/note", body=body if body is not None else json.dumps(data, ensure_ascii=False).encode(),
                  headers={"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}",
                           "Content-Type": "application/json", **(headers or {})})
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


@pytest.mark.parametrize("headers,body,code", [
    ({"Origin": ""}, b'{"text":"hi"}', 403),
    ({"Origin": "http://evil.example"}, b'{"text":"hi"}', 403),
    ({"Origin": "null"}, b'{"text":"hi"}', 403),
    ({"Host": "evil.example"}, b'{"text":"hi"}', 403),
    ({"Content-Type": "application/x-www-form-urlencoded"}, b'text=hi', 415),
    ({}, b'x' * 8193, 413),
    ({}, b'not json', 400),
    ({}, b'\xff', 400),
    ({}, b'[' * 1100 + b']' * 1100, 400),
    ({}, b'[]', 400),
    ({}, b'{"text":42}', 400),
    ({}, b'{"text":""}', 400),
    ({}, b'{"text":"  \\n\\t "}', 400),
    ({}, json.dumps({"text": "x" * 2001}).encode(), 400),
    ({}, b'{"text":"hi","turn":"../../trace"}', 400),
    ({}, b'{"text":"hi","turn":4}', 400),
    ({}, b'{"text":"hi","turn":"20261116-150000-1\\n"}', 400),
    ({}, b'{"text":"\\ud800"}', 400),
    ({"Content-Length": "-1"}, b'{}', 400),
    ({"Transfer-Encoding": "chunked"}, b'{}', 400),
])
def test_note_rejects_invalid_requests(fakes, headers, body, code):
    assert _post_note(fakes, body=body, headers=headers)[0] == code
    assert not sources.notes_path().exists()


def test_note_requires_origin_header(fakes):
    port = fakes.server_address[1]
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        c.request("POST", "/api/note", b'{"text":"hi"}', headers={"Content-Type": "application/json"})
        r = c.getresponse()
        assert r.status == 403 and b"origin" in r.read()
    finally:
        c.close()
    assert not sources.notes_path().exists()


@pytest.mark.parametrize("duplicate,code", [("Host", 403), ("Origin", 403), ("Content-Type", 415), ("Content-Length", 400)])
def test_note_rejects_duplicate_boundary_headers(fakes, duplicate, code):
    port = fakes.server_address[1]
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        c.putrequest("POST", "/api/note", skip_host=True)
        for key, value in {"Host": f"127.0.0.1:{port}", "Origin": f"http://localhost:{port}",
                           "Content-Type": "application/json", "Content-Length": "13"}.items():
            c.putheader(key, value)
            if key == duplicate:
                c.putheader(key, value)
        c.endheaders(b'{"text":"hi"}')
        r = c.getresponse()
        assert r.status == code
        r.read()
    finally:
        c.close()
    assert not sources.notes_path().exists()


def test_notes_append_unicode_create_directory_and_keep_corrupt_lines(fakes, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "new-state"))
    p = sources.notes_path()
    assert not p.parent.exists()
    text = "it heard ‘for’ — काम किया 🎙\nnext line"
    for turn in (None, "20261116-150000-9999", "20261116-150000-1c", "20261116-150000-b2"):
        assert _post_note(fakes, {"turn": turn, "text": f"  {text}  "})[0] == 204
    assert p.stat().st_mode & 0o777 == 0o600
    original = p.read_bytes()
    assert len(original.splitlines()) == 4
    assert all(n["text"] == text for n in sources.notes())
    with p.open("ab") as f:
        f.write(b'broken\n{"ts":null}\n{"ts":1,"text":"missing turn"}\n{"ts":1,"turn":[],"text":"bad"}\n'
                b'{"ts":1e999,"turn":null,"text":"bad timestamp"}\npartial UTF-8 \xe0\xa4\xb9')
    damaged = p.read_bytes()
    assert len(sources.notes()) == 4
    assert _post_note(fakes, {"text": "last"})[0] == 204
    assert p.read_bytes().startswith(damaged) and p.read_bytes().startswith(original)
    assert sources.notes()[0]["text"] == "last" and len(sources.notes()) == 5
    assert _post_note(fakes, {"text": "x" * 2000}, headers={"Origin": f"http://localhost:{fakes.server_address[1]}"})[0] == 204


def test_concurrent_notes_are_complete_lines(fakes):
    from concurrent.futures import ThreadPoolExecutor

    texts = [f"note {i}: " + "न" * 1900 for i in range(24)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda text: _post_note(fakes, {"text": text}), texts))
    assert all(code == 204 for code, _ in results)
    rows = [json.loads(line) for line in sources.notes_path().read_text().splitlines()]
    assert len(rows) == len(texts) and {r["text"] for r in rows} == set(texts)
    assert all(r["turn"] is None and isinstance(r["ts"], float) for r in rows)


def test_notes_attach_to_turns_and_folded_confirmations(fakes, state_dir):
    tid, confirm = "20261116-150000-1", "20261116-150005-1c"
    with (state_dir.parent / "trace.jsonl").open("a") as f:
        for e in [_ev(0, "voice", "heard", tid, text="open it"),
                  _ev(5, "brain", "confirm_reply", confirm, parent=tid),
                  _ev(6, "voice", "turn_end", tid, outcome="done")]:
            f.write(json.dumps(e) + "\n")
    for turn, text in [(tid, "first"), (confirm, "confirmation"), (None, "general"), (tid, "last")]:
        assert _post_note(fakes, {"turn": turn, "text": text})[0] == 204
    data = json.loads(_req(fakes)[2])["turns"]
    row = next(r for r in data["rows"] if r["id"] == tid)
    assert [n["text"] for n in row["notes"]] == ["first", "confirmation", "last"]
    assert [n["text"] for n in data["notes"]] == ["last", "general", "confirmation", "first"]


def test_note_storage_failure_is_reported(fakes, monkeypatch):
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(sources, "append_note", fail)
    code, body = _post_note(fakes, {"text": "keep this draft"})
    assert code == 500 and b"try again" in body
