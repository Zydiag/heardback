import wave

import pytest

np = pytest.importorskip("numpy")

from conftest import FIXTURES, energy_prob  # noqa: E402
from heardback.voice import enroll  # noqa: E402
from heardback.voice.audio_in import wav_frames  # noqa: E402


@pytest.fixture
def fake_mic(tmp_path, on_path, monkeypatch):
    """pw-record that loops cmd.wav + 1 s of silence; energy VAD instead of Silero."""
    raw = tmp_path / "loop.raw"
    raw.write_bytes(np.concatenate([*wav_frames(FIXTURES / "cmd.wav"), np.zeros(16000, np.int16)]).tobytes())
    on_path("pw-record", f'while :; do cat "{raw}" || exit; done')
    monkeypatch.setattr(enroll, "silero", lambda: energy_prob)


def test_layout_from_fake_mic(fake_mic, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert enroll.run(["yesno"], node="fake") == 0
    root = enroll.enroll_dir()
    for d in ("yes", "no"):
        files = sorted((root / d).glob("*.wav"))
        assert len(files) == 10
        with wave.open(str(files[0])) as w:
            assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2) and w.getnframes() > 8000
    assert (root.stat().st_mode & 0o777) == 0o700 and ((root / "yes").stat().st_mode & 0o777) == 0o700


def test_enroll_uses_configured_mode(monkeypatch, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[mic]\nmode = "usb"\n[mic.modes]\nusb = ["usb_*"]\n')
    monkeypatch.setenv("HEARDBACK_CONFIG", str(config))
    monkeypatch.setattr(enroll, "pactl_json", lambda *a: [{"name": "usb_headset"}])
    chosen = []
    monkeypatch.setattr(enroll, "_background", lambda node, path: chosen.append(node))
    assert enroll.run(["background"]) == 0
    assert chosen == ["usb_headset"]


def test_part_resume_skips_done(fake_mic, monkeypatch):
    d = enroll.enroll_dir() / "wake_desk"
    d.mkdir(parents=True)
    (d / "01.wav").write_bytes(b"keep")
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or "n")
    assert enroll.run(["wake_desk"], node="fake") == 0
    assert len(asked) == 1 and "Overwrite" in asked[0] and (d / "01.wav").read_bytes() == b"keep"


def test_fixed_length_clip(fake_mic):
    a = enroll.record_clip("fake", 1.0)
    assert len(a) == 16000 and a.dtype == np.int16
