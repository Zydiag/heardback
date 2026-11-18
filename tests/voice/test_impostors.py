import tarfile

import pytest

np = pytest.importorskip("numpy")
sf = pytest.importorskip("soundfile")

from heardback.voice import models  # noqa: E402
from heardback.voice.config import data_dir  # noqa: E402


def test_impostors_from_tiny_tar(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "IMPOSTOR_SPEAKERS", 2)
    src = tmp_path / "src"
    for spk in ("101", "202", "303"):
        d = src / "LibriSpeech" / "test-clean" / spk / "1"
        d.mkdir(parents=True)
        for u in range(2):
            sf.write(d / f"{spk}-1-{u:04}.flac", np.full(8000, 100, np.int16), 16000)
    tgz = tmp_path / "t.tar.gz"
    with tarfile.open(tgz, "w:gz") as t:
        t.add(src / "LibriSpeech", "LibriSpeech")
    assert models.setup_impostors(tgz.as_uri()) == 2
    wavs = sorted(models.impostors_dir().glob("*.wav"))
    assert [w.name for w in wavs] == ["101.wav", "202.wav"]
    a, sr = sf.read(wavs[0], dtype="int16")
    assert sr == 16000 and len(a) == 16000
    assert not list(data_dir().rglob("*.tar.gz")) and not list(data_dir().rglob("*.flac"))
