import os

import pytest

from brainutil import BIN
from heardback.brain import brain, catalogue


@pytest.fixture
def env(tmp_path, monkeypatch, runtime_dir):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("FAKE_JV_LOG", str(tmp_path / "jv.log"))
    monkeypatch.setenv("FAKE_JV_SCRIPT", str(tmp_path / "jv-script.json"))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "claude.log"))
    monkeypatch.delenv("FAKE_CLAUDE_TOOLS", raising=False)
    monkeypatch.setenv("PATH", f"{BIN}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(catalogue, "_cache", {"at": None, "cat": {}})   # no catalogue leaks between tests
    monkeypatch.setattr(brain, "MIN_TURN_GAP_S", 0)   # back-to-back turns in tests; test_turn_gap sets its own
    return tmp_path
