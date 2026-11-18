import math
import time
import wave

import pytest

np = pytest.importorskip("numpy")

from conftest import FIXTURES
from heardback.voice import audio_in
from heardback.voice.audio_in import FRAME, MicSource, backoff, wav_frames


def wait_until(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_wav_frames_pads_and_counts():
    with wave.open(str(FIXTURES / "cmd.wav")) as w:
        n = w.getnframes()
    frames = list(wav_frames(FIXTURES / "cmd.wav"))
    assert len(frames) == math.ceil(n / 1280)
    assert all(len(f) == 1280 and f.dtype == np.int16 for f in frames)


def test_wav_frames_rejects_stereo(tmp_path):
    p = tmp_path / "s.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100)
        w.writeframes(b"\0" * 4000)
    with pytest.raises(ValueError, match="ffmpeg"):
        list(wav_frames(p))


def test_backoff():
    assert [backoff(n) for n in range(7)] == [1, 2, 4, 8, 16, 30, 30]


@pytest.fixture
def mic(on_path, tmp_path):
    """mic(script) -> (MicSource, frames, fails, restarts, spawn-count file); stops it after the test."""
    made = []

    def make(script, node="n1"):
        spawns = tmp_path / "spawns"
        on_path("pw-record", f'echo "$*" >> {spawns}\n{script}')
        frames, fails, restarts = [], [], []
        m = MicSource(node, frames.append, fails.append, lambda: restarts.append(1))
        m._sleep = lambda s: None
        made.append(m)
        m.start()
        return m, frames, fails, restarts, spawns
    yield make
    for m in made:
        m.stop()


def spawns(f):
    return len(f.read_text().splitlines()) if f.exists() else 0


def test_frames_from_stream(mic):
    m, frames, *_ = mic("head -c 25600 /dev/zero; exec sleep 5")
    assert wait_until(lambda: len(frames) >= 10, 1.0)
    assert all(len(f) == FRAME for f in frames) and m.running


def test_respawn_on_exit(mic):
    m, frames, fails, restarts, sp = mic("head -c 2560 /dev/zero")
    assert wait_until(lambda: spawns(sp) >= 2, 2.0)
    assert restarts and frames


def test_watchdog_on_stall(mic, monkeypatch):
    monkeypatch.setattr(audio_in, "WATCHDOG_S", 0.5)
    m, _, _, _, sp = mic("exec sleep 30")
    assert spawns(sp) <= 1
    assert wait_until(lambda: spawns(sp) >= 2, 2.0)


def test_on_fail_after_three(mic):
    m, _, fails, *_ = mic("exit 1")
    assert wait_until(lambda: 3 in fails, 2.0)


def test_retarget_restarts_with_new_node(mic):
    m, _, _, _, sp = mic("exec sleep 30")
    assert wait_until(lambda: spawns(sp) == 1, 1.0)
    m.retarget("x")
    assert wait_until(lambda: spawns(sp) == 2, 1.0)
    assert "--target x" in sp.read_text().splitlines()[-1]


def test_stop_is_prompt(mic):
    m, *_ = mic("exec sleep 30")
    time.sleep(0.2)
    t0 = time.monotonic()
    m.stop()
    assert time.monotonic() - t0 < 1.0 and not m.running


def test_stop_start_preserves_growing_backoff_and_clears_stop_wake(monkeypatch):
    m = MicSource("missing", lambda frame: None, lambda n: None)
    waits = []

    def missing(*args, **kwargs):
        raise OSError("no microphone")

    def wait(delay):
        assert not m._wake.is_set()
        waits.append(delay)
        if len(waits) % 2 == 0:
            m._stopping = True

    monkeypatch.setattr(audio_in.subprocess, "Popen", missing)
    monkeypatch.setattr(m._wake, "wait", wait)
    for _ in range(2):
        m.start()
        m._thread.join(timeout=1)
        assert not m.running
        m.stop()
    assert waits == [2, 4, 8, 16]


@pytest.mark.parametrize("healthy", [False, True], ids=["brief-frame-burst", "healthy-stream"])
def test_backoff_resets_only_after_healthy_period(monkeypatch, healthy):
    from types import SimpleNamespace

    m = MicSource("mic", lambda frame: None, lambda n: None)
    m._fails = 3
    waits = []
    clock = iter([0, audio_in.HEALTHY_S if healthy else 0.1])
    chunks = iter([bytes(FRAME * 2), b""])
    proc = SimpleNamespace(stdout=SimpleNamespace(fileno=lambda: 1, close=lambda: None),
                           kill=lambda: None, wait=lambda: None)

    def sleep(delay):
        waits.append(delay)
        m._stopping = True

    monkeypatch.setattr(audio_in.subprocess, "Popen", lambda *a, **kw: proc)
    monkeypatch.setattr(audio_in.select, "select", lambda *a: ([proc.stdout], [], []))
    monkeypatch.setattr(audio_in.os, "read", lambda *a: next(chunks))
    monkeypatch.setattr(audio_in.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(m, "_sleep", sleep)
    m._run()
    assert waits == ([2] if healthy else [16])
