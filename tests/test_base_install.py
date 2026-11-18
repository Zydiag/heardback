"""Base commands must work even when site-packages (including voice) are absent."""
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from heardback.voice.extra import default_device


@pytest.mark.parametrize("cuda", [False, True])
def test_default_device(monkeypatch, cuda):
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: cuda)))
    assert default_device() == ("cuda" if cuda else "cpu")


def test_base_cli_and_missing_extra():
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    for code, status, expected in [
        ("from jv.cli import run; raise SystemExit(run(['--help']))", 0, "usage: jv"),
        ("from heardback.voice.cli import main; raise SystemExit(main(['say', 'hello']))", 1,
         "install heardback[voice] or [voice-cpu]"),
        ("from heardback.voice.cli import main; raise SystemExit(main(['setup']))", 1,
         "install heardback[voice] or [voice-cpu]"),
        ("from heardback.voice.config import Config; assert Config().stt_device == 'cpu'", 0, ""),
        ("from heardback.cli import main; raise SystemExit(main(['doctor']))", 1, "warn voice:"),
    ]:
        result = subprocess.run([sys.executable, "-S", "-c", code], env=env, capture_output=True, text=True)
        assert result.returncode == status, result.stderr
        assert expected in result.stdout and not result.stderr
        if expected.startswith("install "):
            assert result.stdout.strip() == expected
