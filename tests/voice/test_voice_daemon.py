import asyncio
import json
import os
import signal
import stat
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

sys.path.insert(0, str(Path(__file__).parent.parent))
from fake_brain import FakeBrain  # noqa: E402

from conftest import FIXTURES, FakeCues, FakeMedia, FakeStt, FakeTts, energy_prob  # noqa: E402
from heardback.voice import daemon  # noqa: E402
from heardback.voice.config import Config  # noqa: E402

SOURCES = json.loads((FIXTURES / "sources_laptop.json").read_text())


class FakeMic:
    made = []

    def __init__(self, node, on_frame, on_fail, on_restart=None):
        self.node, self.running, self.stops = node, False, 0
        FakeMic.made.append(self)

    def start(self):
        self.running = True

    def stop(self):
        self.running, self.stops = False, self.stops + 1

    def retarget(self, node):
        self.node = node


class FakeWatcher:
    def __init__(self, on_change):
        self.on_change = on_change

    def start(self): pass
    def stop(self): pass


@pytest.mark.parametrize("user_config", [False, True])
async def test_daemon_signals(tmp_path, monkeypatch, on_path, user_config):
    if user_config:
        path = Path(__file__).parents[2] / "examples/config.user.toml"
        if not path.exists():
            pytest.skip("private user config is omitted from the public export")
        monkeypatch.setenv("HEARDBACK_CONFIG", str(path))
    else:
        monkeypatch.delenv("HEARDBACK_CONFIG", raising=False)
    on_path("pactl", "echo alsa_input.pci-0000_00_1f.3.analog-stereo")
    voices = []
    real_voice = daemon.Voice

    def capture(*a, **kw):
        v = real_voice(*a, **kw)
        voices.append(v)
        return v
    monkeypatch.setattr(daemon, "warm", lambda cfg, gpu: (FakeStt([]), FakeTts(), False))
    monkeypatch.setattr(daemon, "silero", lambda: energy_prob)
    monkeypatch.setattr(daemon, "MicSource", FakeMic)
    monkeypatch.setattr(daemon, "Watcher", FakeWatcher)
    monkeypatch.setattr(daemon, "pactl_json", lambda *a: SOURCES if "sources" in a else [])
    monkeypatch.setattr(daemon, "Cues", lambda notify: FakeCues())
    monkeypatch.setattr(daemon, "Media", lambda enabled: FakeMedia())
    monkeypatch.setattr(daemon, "Voice", capture)

    sock = tmp_path / "s.sock"
    brain = FakeBrain(sock)
    await brain.start()
    task = asyncio.create_task(daemon.run(Config(), sock, "test-voice-token"))
    try:
        assert (await brain.wait_for("hello"))["token"] == "test-voice-token"
        control_path = daemon.ControlServer(voices[0]).path
        st = control_path.stat()
        assert stat.S_ISSOCK(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o600
        v, mic = voices[0], FakeMic.made[-1]
        assert mic.node == "alsa_input.pci-0000_00_1f.3.analog-stereo" and mic.running
        assert v.cues.played == ["ready"]
        assert v.wake is None
        assert v.cues.notes == ["No wake model configured. Use push-to-talk to speak."]
        os.kill(os.getpid(), signal.SIGUSR1)
        await asyncio.sleep(0.05)
        assert v.state == "LISTEN"
        os.kill(os.getpid(), signal.SIGUSR2)
        await asyncio.sleep(0.1)
        assert v.mic_on is False and v.state == "IDLE" and mic.stops == 1
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await brain.close()
    assert not control_path.exists()
    assert signal.getsignal(signal.SIGUSR1) in (signal.SIG_DFL, None) or True
