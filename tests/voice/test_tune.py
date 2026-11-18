import shutil
import tomllib

import pytest

np = pytest.importorskip("numpy")

from conftest import FIXTURES  # noqa: E402
from heardback.voice import tune  # noqa: E402
from heardback.voice.enroll import enroll_dir  # noqa: E402


@pytest.fixture(autouse=True)
def user_modes(monkeypatch, tmp_path):
    config = tmp_path / "desktop.toml"
    config.write_text('[mic]\nmode = "laptop"\n[mic.modes]\nlaptop = ["alsa_input.*"]\n')
    monkeypatch.setenv("HEARDBACK_CONFIG", str(config))


def test_fires_counts_debounced():
    s = [0.9] * 10 + [0.0] * 30 + [0.9] * 3      # 2 fires: the second burst starts 3.2 s later
    assert tune.fires(s, 0.5) == 2
    assert tune.fires([0.9] * 50, 0.5) == 2        # 4 s above θ: fires at 0.08 s and again after 2 s
    assert tune.fires([0.9, 0.1] * 20, 0.5) == 0   # never 2 in a row


def test_pick_wake_threshold():
    pos = [[0.1] * 5 + [0.8] * 4 + [0.1] * 5 for _ in range(10)]
    neg = [0.0] * int(2 * 3600 / 0.08)
    for start, v in [(1000, 0.5), (20000, 0.5), (40000, 0.5), (60000, 0.7)]:
        neg[start:start + 3] = [v] * 3
    assert tune.pick_wake_threshold(pos, neg, 2.0) == (0.55, 1.0, 0.5)


def test_tune_smoke_writes_toml(tmp_path, capsys):
    root = enroll_dir()
    for part, fx in [("wake_desk", "cmd.wav"), ("wake_room", "other.wav"), ("background", "noise.wav"),
                     ("reading", "cmd.wav"), ("yes", "yes.wav"), ("no", "maybe.wav")]:
        (root / part).mkdir(parents=True)
        shutil.copy(FIXTURES / fx, root / part / "01.wav")

    def scorer(audio):   # RMS per frame: loud speech scores high
        a = np.pad(audio, (0, -len(audio) % 1280)).reshape(-1, 1280) / 32768.0
        return list(np.minimum(1.0, 10 * np.sqrt((a ** 2).mean(axis=1))))
    cfg = tmp_path / "voice.toml"
    cfg.write_text('mic = "laptop"\n')
    r = tune.tune(cfg, scorer=scorer, embed=lambda a: np.array([1.0, 0.0]))
    got = tomllib.loads(cfg.read_text())
    assert got["mic"] == "laptop" and got["wake_threshold"] == r["wake_threshold"] in tune.GRID
    assert "recall" in capsys.readouterr().out


# --- Task 25: speaker thresholds ---------------------------------------------------------------
def test_pick_speaker_threshold():
    th, frr = tune.pick_speaker_threshold([0.6] * 95 + [0.3] * 5, [0.2] * 980 + [0.45] * 20)
    assert th == 0.46 and frr == pytest.approx(0.05)


def test_tune_speaker_smoke(tmp_path, capsys):
    from heardback.voice.models import impostors_dir
    from heardback.voice.speaker import centroid_path
    root = enroll_dir()
    for part, fx, n in [("wake_desk", "cmd.wav", 2), ("wake_room", "cmd.wav", 2), ("background", "noise.wav", 1),
                        ("reading", "cmd.wav", 6), ("yes", "yes.wav", 2), ("no", "maybe.wav", 2)]:
        (root / part).mkdir(parents=True)
        for i in range(n):
            shutil.copy(FIXTURES / fx, root / part / f"{i:02}.wav")
    impostors_dir().mkdir(parents=True)
    shutil.copy(FIXTURES / "other.wav", impostors_dir() / "1089.wav")

    def embed(a):            # loudness + a constant: enough to give distinct cosines
        return np.array([np.sqrt((a.astype(np.float64) ** 2).mean()) / 3000, 1.0])
    cfg = tmp_path / "voice.toml"
    cfg.write_text('mic = "laptop"\nspeaker_check = "off"\n')
    r = tune.tune(cfg, embed=embed)
    got = tomllib.loads(cfg.read_text())
    assert got["speaker_check"] == "off" and got["speaker_wake"] == r["speaker_wake"]
    assert got["speaker_utt"] == r["speaker_utt"] and centroid_path().exists()
    assert "speaker_utt" in capsys.readouterr().out
