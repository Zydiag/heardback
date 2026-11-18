"""Restart line (T4) and desk cue (T6): real Announcer, real Cues; fakes only for notify-send and pw-play."""
import json
import shutil
import subprocess
import time

import pytest

from heardback.status import store
from heardback.status.announce import Announcer
from event_voice import due as deliver_events
from test_announce import isolated_away  # noqa: F401

T0 = 1_800_000_000.0
PERM = "Claude needs your permission to use Bash"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    for tool in ("notify-send", "pw-play"):
        f = bindir / tool
        f.write_text(f'#!/bin/sh\necho "{tool} $*" >> {log}\ncat >/dev/null\n')
        f.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    fake_claude = tmp_path / "claude"
    shutil.copy(shutil.which("sleep"), fake_claude)
    proc = subprocess.Popen([fake_claude, "60"])   # comm == "claude": a live session pid
    yield type("E", (), {"log": log, "pid": proc.pid})
    proc.kill()


def due(ann):
    return deliver_events(ann, muted=ann.muted, at_desk=ann.at_desk)


def calls(env, tool, wait=0.0):
    end = time.time() + wait
    while True:
        lines = [l for l in (env.log.read_text().splitlines() if env.log.exists() else []) if l.startswith(tool)]
        if lines or time.time() >= end:
            return lines
        time.sleep(0.05)


def sess(env, state, **kw):
    return {"session_id": "a", "project": "career", "state": state, "tmux_pane": None, "claude_pid": env.pid,
            "updated_at": store.now_iso(T0 - 30), "last_hook": "Stop", **kw}


def make(env, s, *, away, muted=False):
    t = [T0]
    a = Announcer(now=lambda: t[0], sessions=lambda: [s])
    a.at_desk, a.muted = not away, muted
    return a, t


def test_restart_line_once(env):
    s = sess(env, "waiting", waiting_for=PERM)
    a, _ = make(env, s, away=True)
    assert due(a) == ["Back. Career needs your permission for Bash."]
    b, _ = make(env, s, away=True)   # second start
    assert due(b) == []


def test_restart_line_at_desk_is_a_notification(env):
    pytest.importorskip("numpy")
    a, _ = make(env, sess(env, "idle"), away=False)
    assert due(a) == []
    assert len(calls(env, "notify-send")) == 1 and "Back. Career finished." in calls(env, "notify-send")[0]


def test_held_session_becomes_one_back_line_after_restart(env):
    s = sess(env, "working")
    a, t = make(env, s, away=True)
    s["state"] = "idle"
    finish(a, t)
    t[0] += 10
    assert deliver_events(a, busy=True) == []
    b, _ = make(env, s, away=True)
    assert due(b) == ["Back. Career finished."]
    assert due(b) == []
    c, _ = make(env, s, away=True)
    assert due(c) == []


def test_restart_skips_old_dead_and_muted(env):
    old = sess(env, "idle", updated_at=store.now_iso(T0 - 700))
    assert due(make(env, old, away=True)[0]) == []
    dead = sess(env, "waiting", claude_pid=999_999_999)
    assert due(make(env, dead, away=True)[0]) == []
    assert due(make(env, sess(env, "waiting"), away=True, muted=True)[0]) == []


def finish(a, t, dt=0):
    t[0] = T0 + dt
    a.feed({"session_id": "a", "from": "working", "to": "idle", "hook": "Stop", "turn_s": 60, "detail": "done", "t": store.now_iso(t[0])})


def drive(env, *, away, muted=False, state="idle"):
    s = sess(env, "working", updated_at=store.now_iso(T0 + 100), claude_pid=None)
    a, t = make(env, s, away=away, muted=muted)
    s["state"] = state
    if state == "waiting":
        s["waiting_for"] = PERM
        a.feed({"session_id": "a", "from": "working", "to": "waiting", "hook": "Notification"})
    else:
        finish(a, t)
    t[0] = T0 + 10
    return due(a)


def test_desk_gives_one_chime_and_notification(env):
    pytest.importorskip("numpy")
    assert drive(env, away=False) == []
    assert len(calls(env, "notify-send")) == 1 and "career finished" in calls(env, "notify-send")[0].lower()
    assert len(calls(env, "pw-play", wait=2)) == 1


def test_desk_waiting_names_permission(env):
    pytest.importorskip("numpy")
    drive(env, away=False, state="waiting")  # permission_open() is true without a pane
    assert "needs your permission for Bash" in calls(env, "notify-send")[0]


def test_muted_is_silent(env):
    assert drive(env, away=False, muted=True) == []
    assert calls(env, "notify-send") == [] and calls(env, "pw-play", wait=0.3) == []


def test_away_speaks_without_cue(env):
    assert drive(env, away=True) == ["Career finished: done."]
    assert calls(env, "notify-send") == [] and calls(env, "pw-play", wait=0.3) == []
