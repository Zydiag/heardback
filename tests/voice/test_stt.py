import json
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
faster_whisper = pytest.importorskip("faster_whisper")

from conftest import FIXTURES
from heardback.voice import stt as stt_mod
from heardback.voice.audio_in import wav_frames
from heardback.voice.config import Config
from heardback.voice.stt import Stt, load_catalogue
from heardback.voice.text import hotwords


class FakeWhisper:
    made = []

    def __init__(self, path, device, compute_type):
        FakeWhisper.made.append((device, compute_type))
        if device == "cuda":
            raise RuntimeError("CUDA failed")

    def transcribe(self, audio, **kw):
        assert audio.dtype == np.float32 and kw["language"] == "en" and kw["vad_filter"] is False
        return iter([SimpleNamespace(text=" switch to window 2", no_speech_prob=0.0, avg_logprob=-0.1,
                                     compression_ratio=1.1)]), None


def test_fallback_chain(monkeypatch):
    FakeWhisper.made = []
    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeWhisper)
    monkeypatch.setattr(stt_mod, "snapshot", lambda repo: f"/m/{repo}")
    s = Stt(Config(stt_device="cuda"))
    assert s.device == "cpu" and s.fell_back
    assert FakeWhisper.made == [("cuda", "float16"), ("cuda", "int8_float16"), ("cpu", "int8")]
    assert s.transcribe(np.zeros(16000, np.int16)) == "switch to window 2"


def test_cpu_config_starts_on_cpu(monkeypatch):
    FakeWhisper.made = []
    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeWhisper)
    monkeypatch.setattr(stt_mod, "snapshot", lambda repo: f"/m/{repo}")
    s = Stt(Config(stt_device="cpu"))
    assert FakeWhisper.made == [("cpu", "int8")] and not s.fell_back


def test_dictation_transcription_uses_literal_cleanup(monkeypatch):
    from types import SimpleNamespace

    s = Stt.__new__(Stt)
    segments = [SimpleNamespace(text=" hey Claude ", no_speech_prob=0.0, avg_logprob=0.0, compression_ratio=1.0)]
    monkeypatch.setattr(s, "_segments", lambda _audio: (segments, None))
    assert s.transcribe_dictation(np.zeros(1, np.int16)) == "hey Claude"
    assert s.transcribe(np.zeros(1, np.int16)) == ""


def test_load_catalogue(on_path):
    on_path("jv", 'echo \'{"version":1,"verbs":[{"name":"tmux.switch"}]}\'')
    assert load_catalogue()["verbs"][0]["name"] == "tmux.switch"
    on_path("jv", "exit 1")
    assert load_catalogue() is None


@pytest.mark.models
def test_cmd_wav_cpu():
    audio = np.concatenate(list(wav_frames(FIXTURES / "cmd.wav")))
    t = Stt(Config(stt_device="cpu"), hotwords(None)).transcribe(audio).lower()
    # distil-small.en on this synthetic voice hears "tmux" as "timox"/"t-mox" even with hotwords
    # (turbo int8 on CPU gets it but takes 2.8 s): reported in build-04.
    assert "switch" in t and "window" in t
