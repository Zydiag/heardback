import pytest

np = pytest.importorskip("numpy")

from heardback.voice.wake import Wake, WakeGate, load_wake  # noqa: E402
from heardback.voice.config import Config  # noqa: E402


def fired(gate, scores, dt=0.08, boost=0.0):
    return [gate.update(s, i * dt, boost) for i, s in enumerate(scores)]


def test_gate_patience():
    assert fired(WakeGate(0.5), [0.6, 0.4, 0.6, 0.6]) == [False, False, False, True]


def test_gate_debounce():
    g = WakeGate(0.5)
    assert g.update(0.9, -0.08) is False and g.update(0.9, 0.0) is True
    assert [g.update(0.9, 0.92), g.update(0.9, 1.0)] == [False, False]
    assert g.update(0.9, 2.1) is True


def test_gate_boost():
    assert fired(WakeGate(0.5), [0.65] * 4, boost=0.2) == [False] * 4
    assert fired(WakeGate(0.5), [0.75] * 2, boost=0.2) == [False, True]


class FakeModel:
    def __init__(self):
        self.resets = 0

    def predict(self, frame):
        return {"hey_claude": 0.7}

    def reset(self):
        self.resets += 1


def test_wake_ring_and_score(tmp_path):
    w = Wake(tmp_path / "hey_claude.onnx", 0.5, model=FakeModel())
    for i in range(30):
        assert w.score(np.full(1280, i, np.int16)) == 0.7
    assert len(w.ring) == 25
    a = w.ring_audio(1.5)
    assert len(a) == 24000 and a[-1] == 29
    w.reset()
    assert w.model.resets == 1


def test_load_wake_missing(capsys):
    assert load_wake(Config()) is None
    output = capsys.readouterr().out
    assert "wake_missing" in output and "push-to-talk" in output and "SIGUSR1" in output
