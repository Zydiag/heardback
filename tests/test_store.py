import json
import pytest

from heardback.status import store


def test_write_read_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    rec = {"session_id": "a", "state": "idle", "project": "p"}
    store.write_session(rec)
    assert store.read_raw("a") == rec
    assert store.session_path("a").stat().st_mode & 0o777 == 0o600


def test_read_raw_corrupt(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    p = store.session_path("a")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{")
    assert store.read_raw("a") is None
    assert store.read_raw("missing") is None


def test_append_event_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    store.append_event({"to": "working"})
    store.append_event({"to": "idle"})
    lines = (store.state_dir() / "events.jsonl").read_text().splitlines()
    assert [json.loads(l)["to"] for l in lines] == ["working", "idle"]


def test_append_event_caps_4k(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    store.append_event({"to": "idle", "detail": "é" * 10_000})
    raw = (store.state_dir() / "events.jsonl").read_bytes()
    assert len(raw) < 4096 and json.loads(raw)["detail"]


def test_epoch():
    assert store.epoch(None) == 0.0
    assert abs(store.epoch(store.now_iso()) - __import__("time").time()) < 2


def test_now_iso_millis():
    assert ".500" in store.now_iso(1.5) and store.epoch(store.now_iso(1.5)) == 1.5


def test_write_session_failure_leaves_no_tmp(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    with pytest.raises(TypeError):
        store.write_session({"session_id": "a", "bad": object()})
    assert list(store.session_path("a").parent.iterdir()) == []


def test_list_sessions_no_create_path_is_read_only_until_default_path_is_used(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "missing-state-home"))
    assert store.list_sessions(create_state_dir=False) == []
    assert not (tmp_path / "missing-state-home").exists()

    # Existing callers keep their original default directory-creation behavior.
    assert store.list_sessions() == []
    assert (tmp_path / "missing-state-home" / "heardback").is_dir()


FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures" / "claude_sessions"


def _registry(monkeypatch, live=(4242, 4343, 4444)):
    import os
    import shutil
    d = store.claude_dir()
    shutil.copytree(FIXTURES, d / "sessions", dirs_exist_ok=True)
    monkeypatch.setattr(store, "alive", lambda pid: pid in live)
    assert d == __import__("pathlib").Path(os.environ["CLAUDE_CONFIG_DIR"])
    return d


def test_registry_maps_fields(monkeypatch):
    d = _registry(monkeypatch)
    t = d / "projects" / "-home-u-work-demo-app" / "aaaa1111-0000-4000-8000-000000000001.jsonl"
    t.parent.mkdir(parents=True)
    t.touch()
    recs = {r["claude_pid"]: r for r in store.registry_sessions()}
    assert sorted(recs) == [4242, 4343, 4444]
    r = recs[4242]
    assert r["session_id"] == "aaaa1111-0000-4000-8000-000000000001" and r["source"] == "registry"
    assert r["state"] == "working" and r["tmux_pane"] == "%1" and r["project"] == "demo-app"
    assert r["cwd"] == "~/user/u/work/demo-app" and r["transcript_path"] == str(t)
    assert store.epoch(r["updated_at"]) == 1798000300.5
    assert r["last_reply"] is None and r["tmux_target"] is None
    assert recs[4343]["state"] == "idle"  # "shell" = idle at the prompt (probe 2026-10-30)
    assert recs[4343]["tmux_pane"] is None and recs[4343]["transcript_path"] is None
    assert recs[4444]["state"] == "waiting" and recs[4444]["waiting_for"] == "permission to run tests"
    assert recs[4444]["tmux_pane"] is None


def test_registry_transcript_in_other_project_dir(monkeypatch):
    d = _registry(monkeypatch)
    t = d / "projects" / "-home-u-elsewhere" / "cccc3333-0000-4000-8000-000000000003.jsonl"
    t.parent.mkdir(parents=True)
    t.touch()
    recs = {r["claude_pid"]: r for r in store.registry_sessions()}
    assert recs[4444]["transcript_path"] == str(t) and recs[4343]["transcript_path"] is None


def test_registry_skips_dead_pid(monkeypatch):
    _registry(monkeypatch, live=(4343,))
    assert [r["claude_pid"] for r in store.registry_sessions()] == [4343]


def test_registry_bad_json_skipped(monkeypatch):
    d = _registry(monkeypatch)
    (d / "sessions" / "1.json").write_text("{")
    (d / "sessions" / "2.json").write_text("[1]")
    (d / "sessions" / "3.json").write_text('{"pid": 4242}')
    (d / "sessions" / "4.json").write_text('{"pid": 4242, "sessionId": ["x"], "cwd": "/a", "updatedAt": 1}')
    assert sorted(r["claude_pid"] for r in store.registry_sessions()) == [4242, 4343, 4444]


def test_registry_missing_dir_empty():
    assert store.registry_sessions() == []


def test_alive():
    import os
    assert store.alive(None) is False and store.alive(999999999) is False
    assert store.alive(os.getpid()) is False  # python, not claude


def _hook(sid, **kw):
    store.write_session({"session_id": sid, "state": "working", "claude_pid": 4242, "tmux_pane": None,
                         "updated_at": store.now_iso(), **kw})


def _by_id(recs):
    return {r["session_id"]: r for r in recs}


def test_dead_pid_shows_ended():
    _hook("a", claude_pid=999999999)
    (r,) = store.list_sessions(resolve=False)
    assert r["state"] == "ended" and r["end_reason"] == "gone" and r["source"] == "hook"


def test_live_pid_kept(monkeypatch):
    monkeypatch.setattr(store, "alive", lambda pid: True)
    _hook("a")
    (r,) = store.list_sessions(resolve=False)
    assert r["state"] == "working" and r["end_reason"] is None and r["last_reply"] is None


def test_corrupt_and_partial_files():
    d = store.session_path("x").parent
    d.mkdir(parents=True, exist_ok=True)
    (d / "bad.json").write_text("{")
    (d / "list.json").write_text("[1]")
    (d / "x.json").write_text('{"session_id": "x"}')
    (d / "y.json").write_text(json.dumps({"session_id": "y", "claude_pid": None, "tmux_pane": 7,
                                          "updated_at": store.now_iso()}))
    (d / "z.json").write_text('{"session_id": "z", "state": "ended", "updated_at": "yesterday"}')
    now = store.now_iso()
    (d / "l.json").write_text(json.dumps({"session_id": ["l"], "state": "ended", "updated_at": now}))
    (d / "t.json").write_text(json.dumps({"session_id": "t", "state": "ended", "updated_at": now,
                                          "claude_pid": [1], "tmux_pane": {"a": 1}}))
    (d / "u.json").write_text(json.dumps({"session_id": "u", "state": "working", "updated_at": now,
                                          "claude_pid": True, "tmux_pane": ["%1"]}))
    recs = _by_id(store.list_sessions())
    assert recs["y"]["state"] == "ended" and recs["y"]["tmux_target"] is None
    assert recs["t"]["claude_pid"] is None and recs["t"]["tmux_pane"] is None
    assert recs["u"]["state"] == "ended" and recs["u"]["claude_pid"] is None
    assert "x" not in recs and "z" not in recs  # no usable updated_at: ended long ago
    assert store.get_by_id("x")["state"] in (None, "ended") and store.get_by_id("bad") is None


def test_same_pane_newest_wins(monkeypatch):
    import time
    monkeypatch.setattr(store, "alive", lambda pid: True)
    _hook("old", tmux_pane="%5", updated_at=store.now_iso(time.time() - 10))
    _hook("new", tmux_pane="%5", state="idle")
    _hook("other", tmux_pane="%6", updated_at=store.now_iso(time.time() - 5))
    recs = store.list_sessions(resolve=False)
    assert [r["session_id"] for r in recs] == ["new", "other", "old"]
    r = _by_id(recs)
    assert r["new"]["state"] == "idle" and r["other"]["state"] == "working"
    assert r["old"]["state"] == "ended" and r["old"]["end_reason"] == "replaced"


def test_session_identity_and_observation_freshness(monkeypatch):
    # now_iso stores milliseconds; an integral clock preserves an exact four-second boundary.
    now = 1_800_000_000.0
    monkeypatch.setattr(store, "alive", lambda pid: True)
    _hook("observed", tmux_pane="%5", updated_at=store.now_iso(now - 4))
    (rec,) = store.list_sessions(now=now, resolve=False)
    assert rec["identity"] == {"session_id": "observed", "pane_id": "%5"}
    assert rec["observation"]["source"] == "hook"
    assert rec["observation"]["state_updated_at"] == rec["updated_at"]
    assert rec["observation"]["state_age_s"] == 4


def test_newer_registry_identity_replaces_hook_without_rewriting_it(monkeypatch):
    import time
    now = time.time()
    monkeypatch.setattr(store, "alive", lambda pid: True)
    old = {"session_id": "old", "state": "working", "claude_pid": 4242, "tmux_pane": "%5",
           "started_at": store.now_iso(now - 30), "updated_at": store.now_iso(now - 10)}
    store.write_session(old)
    candidate = {"session_id": "new", "state": "idle", "claude_pid": 4242, "tmux_pane": "%5",
                 "started_at": store.now_iso(now - 1), "updated_at": store.now_iso(now - 1),
                 "source": "registry", "internal": False}
    monkeypatch.setattr(store, "registry_sessions", lambda: [candidate])
    recs = _by_id(store.list_sessions(now=now, resolve=False))
    assert recs["new"]["source"] == "registry" and recs["new"]["state"] == "idle"
    assert recs["old"]["state"] == "ended" and recs["old"]["end_reason"] == "replaced"
    assert store.read_raw("old") == old


@pytest.mark.parametrize("started_at", [None, "older"])
def test_registry_identity_without_newer_start_does_not_replace_hook(monkeypatch, started_at):
    import time
    now = time.time()
    monkeypatch.setattr(store, "alive", lambda pid: True)
    old = {"session_id": "old", "state": "working", "claude_pid": 4242, "tmux_pane": "%5",
           "started_at": store.now_iso(now - 30), "updated_at": store.now_iso(now - 10)}
    store.write_session(old)
    candidate = {"session_id": "new", "state": "idle", "claude_pid": 4242, "tmux_pane": "%5",
                 "started_at": store.now_iso(now - 40) if started_at else None,
                 "updated_at": store.now_iso(now), "source": "registry", "internal": False}
    monkeypatch.setattr(store, "registry_sessions", lambda: [candidate])
    recs = store.list_sessions(now=now, resolve=False)
    assert [r["session_id"] for r in recs] == ["old"]
    assert recs[0]["state"] == "working"


def test_registry_dedupe_by_pid(monkeypatch):
    _registry(monkeypatch)
    _hook("h1", claude_pid=4242)  # same pid as registry 4242, other id
    _hook("bbbb2222-0000-4000-8000-000000000002", claude_pid=None)  # same id as registry 4343
    recs = _by_id(store.list_sessions(resolve=False))
    assert {k: r["source"] for k, r in recs.items()} == {
        "h1": "hook", "bbbb2222-0000-4000-8000-000000000002": "hook",
        "cccc3333-0000-4000-8000-000000000003": "registry"}


def test_old_ended_hidden():
    import time
    now = time.time()
    _hook("recent", state="ended", updated_at=store.now_iso(now - store.LIST_ENDED_WINDOW_S + 60))
    _hook("old", state="ended", updated_at=store.now_iso(now - store.LIST_ENDED_WINDOW_S - 60))
    _hook("gone", updated_at=store.now_iso(now - 2 * store.LIST_ENDED_WINDOW_S), claude_pid=None)
    assert [r["session_id"] for r in store.list_sessions(now=now, resolve=False)] == ["recent"]
    assert len(store.list_sessions(now=now - 3 * store.LIST_ENDED_WINDOW_S, resolve=False)) == 3


def test_resolve_target_jvtest(tmux_server, monkeypatch):
    pane = tmux_server("display", "-p", "-t", "0", "#{pane_id}").strip()
    tmux_server("rename-window", "-t", "0", "build 03")  # a space in the name
    assert store.resolve_target(pane) == "0:0.0"  # socket from JV_TMUX_SOCKET
    assert store.resolve_target(pane, "jvtest-nope") is None and store.resolve_target(None) is None
    monkeypatch.setattr(store, "alive", lambda pid: True)
    _hook("a", tmux_pane=pane, tmux_target="stale")
    _hook("b", tmux_pane="%999", tmux_target="stale")
    monkeypatch.setattr(store, "resolve_target", None)  # the list uses one list-panes, not a display per record
    r = _by_id(store.list_sessions())
    assert r["a"]["tmux_target"] == "0:0.0" and r["b"]["tmux_target"] is None
    assert r["a"]["tmux_window"] == "build 03" and r["b"]["tmux_window"] is None
    assert store.pane_targets() == {pane: ("0:0.0", "build 03")} and store.pane_targets("jvtest-nope") == {}
    monkeypatch.setattr(store, "resolve_target", None)  # resolve=False never shells out
    assert {x["tmux_target"] for x in store.list_sessions(resolve=False)} == {"stale"}


def test_registry_updated_at_fallback(monkeypatch):
    d = _registry(monkeypatch)
    (d / "sessions" / "4444.json").write_text(json.dumps(
        {"pid": 4444, "sessionId": "c", "cwd": "/a", "updatedAt": 1798000000000}))
    (d / "sessions" / "4343.json").write_text(json.dumps(
        {"pid": 4343, "sessionId": "b", "cwd": "/a", "startedAt": 1797000000000}))
    recs = {r["session_id"]: r for r in store.registry_sessions()}
    assert store.epoch(recs["c"]["updated_at"]) == 1798000000 and store.epoch(recs["b"]["updated_at"]) == 1797000000


def test_registry_after_missed_clear(monkeypatch):
    # /clear ends the old session id; the same claude pid runs a new one whose SessionStart was missed.
    _registry(monkeypatch)
    _hook("old", claude_pid=4242, state="ended", end_reason="clear")
    recs = _by_id(store.list_sessions(resolve=False))
    assert recs["old"]["state"] == "ended" and recs["aaaa1111-0000-4000-8000-000000000001"]["source"] == "registry"


def _gc_files(state_dir, monkeypatch, now):
    monkeypatch.setattr(store, "alive", lambda pid: pid == 7)
    day = 86400
    recs = {"old-ended": ("ended", None, now - 8 * day), "old-crashed": ("working", 99, now - 8 * day),
            "new-ended": ("ended", None, now - 6 * day), "old-live": ("idle", 7, now - 30 * day)}
    for sid, (st, pid, t) in recs.items():
        (state_dir / f"{sid}.json").write_text(json.dumps({"session_id": sid, "state": st, "claude_pid": pid,
                                                           "updated_at": store.now_iso(t)}))
    (state_dir / "junk.json").write_text("{")


def test_gc_deletes_old_ended(state_dir, monkeypatch):
    now = 1_800_000_000.0
    _gc_files(state_dir, monkeypatch, now)
    assert store.gc(now) == {"deleted": 2, "rotated": False}
    assert not (state_dir / "old-ended.json").exists() and not (state_dir / "old-crashed.json").exists()


def test_gc_keeps_recent_and_live(state_dir, monkeypatch):
    now = 1_800_000_000.0
    _gc_files(state_dir, monkeypatch, now)
    store.gc(now)
    assert sorted(p.name for p in state_dir.iterdir()) == ["junk.json", "new-ended.json", "old-live.json"]


def test_gc_rotates_events(state_dir, monkeypatch):
    ev = state_dir.parent / "events.jsonl"
    ev.write_bytes(b"x" * 100)
    assert store.gc()["rotated"] is False and ev.exists()
    monkeypatch.setattr(store, "EVENTS_ROTATE_BYTES", 50)
    assert store.gc()["rotated"] is True
    assert not ev.exists() and (state_dir.parent / "events.jsonl.1").read_bytes() == b"x" * 100
    store.append_event({"to": "idle"})
    assert ev.exists()


def test_gc_verb(state_dir, monkeypatch, run_jv):
    assert run_jv("status", "gc", "--json")[1]["deleted"] == 0
