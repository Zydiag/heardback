import pytest

np = pytest.importorskip("numpy")

from conftest import FIXTURES
from heardback.voice.config import Config
from heardback.voice.speaker import SpeakerCheck, build_centroid, centroid_path, cosine, load_speaker, windows
from heardback.voice.tune import _read


def test_cosine():
    assert cosine(np.array([1.0, 0.0]), np.array([2.0, 0.0])) == pytest.approx(1.0)
    assert cosine(np.array([1.0, 0.0]), np.array([0.0, 3.0])) == pytest.approx(0.0)
    assert cosine(np.array([1.0, 1.0]), np.array([-1.0, -1.0])) == pytest.approx(-1.0)


def test_windows_drop_short_tail():
    a = np.arange(16000 * 7, dtype=np.int16)          # 7 s → two 3 s windows, 1 s tail dropped
    w = windows(a)
    assert [len(x) for x in w] == [48000, 48000]
    assert len(windows(np.zeros(16000 * 8, np.int16))) == 3   # 2 s tail ≥ half a window: kept
    assert windows(np.zeros(16000, np.int16)) == []


def test_build_centroid_normalized(tmp_path):
    seen = []

    def embed(a):
        seen.append(len(a))
        return np.array([3.0, 4.0]) if len(seen) % 2 else np.array([0.0, 4.0])

    c = build_centroid([np.zeros(16000 * 6, np.int16), np.zeros(16000 * 3, np.int16)], embed=embed)
    assert seen == [48000] * 3
    assert np.linalg.norm(c) == pytest.approx(1.0)
    assert c[1] > c[0] > 0
    assert centroid_path().exists() and np.allclose(np.load(centroid_path()), c)


def test_load_speaker(tmp_path, capsys):
    assert load_speaker(Config()) is None
    on = Config(speaker_check="on", speaker_wake=0.5, speaker_utt=0.5)
    assert load_speaker(on) is None and "heardback-voice enroll" in capsys.readouterr().out   # no centroid yet
    centroid_path().parent.mkdir(parents=True)
    np.save(centroid_path(), np.array([1.0, 0.0]))
    s = load_speaker(Config(speaker_check="on", speaker_wake=0.5, speaker_utt=0.5), embed=lambda a: np.array([1.0, 1.0]))
    assert s.score(np.zeros(16000, np.int16)) == pytest.approx(0.7071, abs=1e-3)


@pytest.mark.models
def test_ecapa_same_vs_different():
    s = SpeakerCheck(None)
    cmd, other = _read(FIXTURES / "cmd.wav"), _read(FIXTURES / "other.wav")
    s.centroid = build_centroid([cmd], embed=s.embed, save=False)
    assert s.embed(cmd).shape == (192,)
    assert s.score(cmd) > s.score(other)
