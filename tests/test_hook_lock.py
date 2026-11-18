"""The real jv-hook subprocess against sessions.lock."""
import fcntl
import json
import os
import subprocess
import sys
import time

import pytest

from heardback.status import store


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("HEARDBACK_BRAIN", raising=False)
    return tmp_path


def _hook(ev: dict) -> subprocess.Popen:
    p = subprocess.Popen([sys.executable, "-m", "heardback.status.hook"], stdin=subprocess.PIPE, env=os.environ.copy())
    p.stdin.write(json.dumps(ev).encode())
    p.stdin.close()
    return p


STOP = {"hook_event_name": "Stop", "session_id": "s1", "cwd": "/x/career", "transcript_path": "/t.jsonl",
        "last_assistant_message": "Done. More.", "background_tasks": []}
PROMPT = {**STOP, "hook_event_name": "UserPromptSubmit", "prompt": "go"}


def _lock():
    fd = os.open(store.state_dir() / "sessions.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_stop_waits_for_lock(state):
    fd = _lock()
    p = _hook(STOP)
    time.sleep(0.5)
    assert store.read_raw("s1") is None
    os.close(fd)
    assert p.wait(5) == 0
    assert store.read_raw("s1")["state"] == "idle"


def test_stop_writes_unlocked_after_a_second(state):
    fd = _lock()
    try:
        assert _hook(STOP).wait(5) == 0
        assert store.read_raw("s1")["last_reply"] == "Done."
    finally:
        os.close(fd)


def test_racing_prompt_and_stop_both_logged(state):
    fd = _lock()
    a = _hook(PROMPT)
    time.sleep(0.3)  # prompt is polling the lock; stop is still importing when it is released
    b = _hook(STOP)
    os.close(fd)
    assert a.wait(5) == b.wait(5) == 0
    assert store.read_raw("s1")["state"] == "idle"
    evs = [json.loads(l) for l in (state / "heardback" / "events.jsonl").read_text().splitlines()]
    assert [(e["from"], e["to"]) for e in evs] == [(None, "working"), ("working", "idle")]
