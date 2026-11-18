import asyncio
import inspect
import os
from pathlib import Path

import pytest

# Captured before the root conftest points HOME at a tmp dir: `-m models` tests read the real models.
REAL_MODELS = Path(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")) / "heardback" / "models"
FIXTURES = Path(__file__).parent.parent / "fixtures" / "voice"


def pytest_collection_modifyitems(config, items):
    if os.environ.get("HEARDBACK_MODELS") == "1":
        return
    skip = pytest.mark.skip(reason="loads real models; set HEARDBACK_MODELS=1")
    for it in items:
        if "models" in it.keywords:
            it.add_marker(skip)


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    """Run `async def` tests in a fresh event loop (no pytest-asyncio dependency)."""
    if inspect.iscoroutinefunction(pyfuncitem.obj):
        args = {k: pyfuncitem.funcargs[k] for k in pyfuncitem._fixtureinfo.argnames}
        asyncio.run(asyncio.wait_for(pyfuncitem.obj(**args), 60))
        return True


@pytest.fixture(autouse=True)
def xdg(tmp_path, monkeypatch, request):
    """XDG data dir in tmp (config/state/runtime already are, root conftest). Models tests see the real models."""
    data = tmp_path / "data"
    (data / "heardback").mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    if "models" in request.keywords:
        (data / "heardback" / "models").symlink_to(REAL_MODELS)
        monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    return data


def fake_bin(tmp_path, name, script) -> Path:
    """Write an executable shell script `name`; returns its dir (prepend to PATH)."""
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    f = d / name
    f.write_text("#!/bin/sh\n" + script + "\n")
    f.chmod(0o755)
    return d


@pytest.fixture
def on_path(tmp_path, monkeypatch):
    """on_path(name, script): put a fake binary first on PATH."""
    def put(name, script):
        d = fake_bin(tmp_path, name, script)
        monkeypatch.setenv("PATH", f"{d}:{os.environ['PATH']}")
        return d
    return put


# --- shared fakes for the loop (plan 04 Task 10) -------------------------------------------
class FakeStt:
    def __init__(self, script):
        self.script, self.calls = list(script), 0

    def transcribe(self, audio):
        self.calls += 1
        return self.script.pop(0) if self.script else ""


class FakeTts:
    def __init__(self):
        self.said, self.stopped, self.speaking, self.first_audio_at = [], 0, False, None
        self.finish_delay = 0.0

    async def say(self, text):
        self.said.append(text)
        self.speaking = True

    async def finish(self):
        if self.finish_delay:
            await asyncio.sleep(self.finish_delay)
        self.speaking = False

    def stop(self):
        self.stopped += 1
        self.speaking = False


class FakeCues:
    def __init__(self):
        self.played, self.notes, self.sticky = [], [], []

    def play(self, name):
        self.played.append(name)

    def notify(self, text, sticky=False):
        (self.sticky if sticky else self.notes).append(text)


class FakeMedia:
    def __init__(self):
        self.calls = []

    def pause(self, tid=None):
        self.calls.append("pause")

    def resume(self, tid=None):
        self.calls.append("resume")

    def keep(self):
        self.calls.append("keep")


def energy_prob(chunk) -> float:
    import numpy as np
    return 1.0 if np.sqrt(np.mean((chunk / 32768.0) ** 2)) > 0.01 else 0.0


def push_wav(voice, path, tail_s=5.0):
    import numpy as np
    from heardback.voice.audio_in import FRAME, wav_frames
    for f in wav_frames(path):
        voice.feed(f)
    for _ in range(int(tail_s * 16000 / FRAME)):
        voice.feed(np.zeros(FRAME, np.int16))


def push_zeros(voice, seconds):
    import numpy as np
    from heardback.voice.audio_in import FRAME
    for _ in range(int(seconds * 16000 / FRAME)):
        voice.feed(np.zeros(FRAME, np.int16))
