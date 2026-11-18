import asyncio
import sys
from pathlib import Path

import pytest

pytest.importorskip("numpy")

sys.path.insert(0, str(Path(__file__).parent.parent))
from fake_brain import FakeBrain  # noqa: E402

from conftest import FIXTURES  # noqa: E402
from heardback.voice.cli import main  # noqa: E402


async def answer_done(brain, msg):
    await brain.send({"type": "say", "text": "Done.", "final": True})
    await brain.send({"type": "done"})


@pytest.mark.models
async def test_run_file_cmd(tmp_path, capsys):
    p = tmp_path / "s.sock"
    brain = FakeBrain(p)
    brain.on_utterance = answer_done
    await brain.start()
    rc = await asyncio.to_thread(main, ["run-file", str(FIXTURES / "cmd.wav"), "--sock", str(p)])
    await brain.close()
    assert rc == 0
    assert "tmux" in brain.of("utterance")[0]["text"].lower()
    assert "say: Done." in capsys.readouterr().out


def test_run_file_wake_needs_model(capsys):
    pytest.importorskip("numpy")
    rc = main(["run-file", str(FIXTURES / "cmd.wav"), "--wake", "--sock", "/nonexistent.sock"])
    assert rc == 1 and "no wake model" in capsys.readouterr().out


@pytest.mark.parametrize("argv, want", [([], 2.5), (["--volume", "1.2"], 1.2)])
def test_say_volume_passes_through(xdg, monkeypatch, argv, want):
    seen = {}

    class FakeTts:
        def __init__(self, voice, device, *, volume):
            seen["volume"] = volume

        async def say(self, text): pass

        async def finish(self): pass
    monkeypatch.setattr("heardback.voice.tts.Tts", FakeTts)
    assert main(["say", *argv, "hi"]) == 0
    assert seen["volume"] == want


def test_dictation_cli_prints_only_bounded_metadata(monkeypatch, capsys):
    from heardback.voice import control

    monkeypatch.setattr(control, "request", lambda payload: {
        "ok": True, "session_id": "a" * 32, "phase": "review", "chars": 12,
        "text": "private transcript", "operation": payload["action"],
    })
    assert main(["dictation", "status"]) == 0
    out = capsys.readouterr().out
    assert '"phase": "review"' in out and '"chars": 12' in out
    assert "private transcript" not in out and "text" not in out


@pytest.mark.parametrize("action", ["finish", "commit"])
def test_dictation_cli_timeout_warns_against_blind_retry(monkeypatch, capsys, action):
    from heardback.voice import control

    def timeout(_payload):
        raise TimeoutError("control_timeout")

    monkeypatch.setattr(control, "request", timeout)
    argv = ["dictation", action, "--session", "a" * 32]
    if action == "commit":
        argv.extend(["--revision", "7"])
    assert main(argv) == 2
    out = capsys.readouterr().out
    assert "timed out" in out and "status" in out and "do not retry blindly" in out


def test_dictation_cli_maps_unknown_server_error(monkeypatch, capsys):
    from heardback.voice import control

    monkeypatch.setattr(control, "request", lambda _payload: {"ok": False, "error": "transcript leaked"})
    assert main(["dictation", "status"]) == 1
    assert capsys.readouterr().out.strip() == "dictation: control_error"
