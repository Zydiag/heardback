"""Real `claude -p` run with jv-hook wired via --settings. Spends a little quota: HEARDBACK_LIVE=1 only.

HOME is the real one because the subscription OAuth login lives there (no API key, never --bare).
`--setting-sources ""` loads no user/project/local settings, so only the temp --settings file applies.
Claude still makes ~/.claude/projects/<cwd-derived>/; the fixture removes that one dir afterwards.
"""
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("HEARDBACK_LIVE") != "1", reason="live: set HEARDBACK_LIVE=1")

# The spec's block, except Stop is sync too: with "async" the -p process exits before jv-hook finishes Stop, so
# the file stayed "working" with no last_reply (seen 2026-10-30, claude 2.1.312). Interactive sessions keep running.
SYNC = {"Stop", "SessionEnd"}
EVENTS = ["SessionStart", "UserPromptSubmit", "Notification", "PostToolUse", "Stop", "StopFailure", "SessionEnd"]


def _claude(tmp_path, state, **extra):
    hook = str(Path(sys.executable).parent / "jv-hook")
    settings = tmp_path / "settings.json"
    entry = lambda e: {"type": "command", "command": hook, **({} if e in SYNC else {"async": True})}
    settings.write_text(json.dumps({"hooks": {e: [{"hooks": [entry(e)]}] for e in EVENTS}}))
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CONFIG_DIR", "HEARDBACK_BRAIN", "ANTHROPIC_API_KEY")}
    env.update(HOME=pwd.getpwuid(os.getuid()).pw_dir, XDG_STATE_HOME=str(state), **extra)  # real login, tmp store
    r = subprocess.run(["claude", "-p", "say hi", "--model", "haiku", "--no-session-persistence",
                        "--setting-sources", "", "--settings", str(settings)],
                       cwd=tmp_path, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    time.sleep(2)  # let any async hook land
    return r


@pytest.fixture(autouse=True)
def _drop_claude_project_dir(tmp_path):
    yield
    projects = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".claude" / "projects"
    d = projects / re.sub(r"[^A-Za-z0-9]", "-", str(tmp_path))
    if d.parent == projects and d.name.startswith("-tmp-") and d.is_dir():
        shutil.rmtree(d)


def test_live_hooks_write_session(tmp_path):
    state = tmp_path / "state"
    r = _claude(tmp_path, state)
    files = list((state / "heardback" / "sessions").glob("*.json"))
    events = [json.loads(l) for l in (state / "heardback" / "events.jsonl").read_text().splitlines()]
    print("reply:", r.stdout.strip(), "\nfiles:", [json.loads(f.read_text()) for f in files], "\nevents:", events)
    assert len(files) == 1
    s = json.loads(files[0].read_text())
    assert s["state"] == "ended" and s["last_reply"] and isinstance(s["claude_pid"], int)
    assert [e["to"] for e in events] == ["idle", "working", "idle", "ended"] and events[2]["hook"] == "Stop"
    assert not (state / "heardback" / "hook-errors.log").exists()


def test_live_brain_guard(tmp_path):
    state = tmp_path / "state"
    _claude(tmp_path, state, HEARDBACK_BRAIN="1")
    assert not (state / "heardback").exists()
