import json

import pytest

np = pytest.importorskip("numpy")

from conftest import FIXTURES
from heardback.voice.audio_in import FRAME, wav_frames
from heardback.voice.endpoint import Endpointer, silero

CHUNK_S = 512 / 16000
ZERO = np.zeros(FRAME, np.int16)


def scripted(fn):
    """prob callable driven by stream time of the chunk start: fn(t) -> prob."""
    n = [0]

    def prob(chunk):
        assert len(chunk) == 512
        t = n[0] * CHUNK_S
        n[0] += 1
        return fn(t)
    return prob


def run(ep, seconds):
    """Feed zero frames; return (status, stream time when it first left waiting/speech)."""
    for i in range(int(seconds * 16000 / FRAME) + 1):
        s = ep.feed(ZERO)
        if s in ("done", "timeout"):
            return s, (i + 1) * FRAME / 16000
    return s, None


def test_burst_under_min_ignored():
    ep = Endpointer(scripted(lambda t: 1.0 if t < 5 * CHUNK_S else 0.0))
    s, t = run(ep, 6)
    assert s == "timeout" and abs(t - 4.0) <= 0.1


def test_done_after_end_silence():
    ep = Endpointer(scripted(lambda t: 1.0 if 1.0 <= t < 3.0 else 0.0))
    s, t = run(ep, 6)
    assert s == "done" and ep.reason == "silence" and abs(t - 3.7) <= 0.1
    assert abs(len(ep.audio) / 16000 - 3.0) <= 0.1
    assert abs(ep.speech_end_s - 3.0) <= CHUNK_S
    assert ep.feed(ZERO) == "done"


def test_cap_at_max_s():
    ep = Endpointer(scripted(lambda t: 1.0))
    s, t = run(ep, 20)
    assert s == "done" and ep.reason == "max_duration" and abs(t - 15.0) <= 0.1


def test_frame_remainder():
    seen = []
    ep = Endpointer(lambda c: seen.append(len(c)) or 0.0, wait_s=100)
    for _ in range(7):
        ep.feed(ZERO)
    assert set(seen) == {512} and sum(seen) == (7 * FRAME) // 512 * 512


def test_take_rest_returns_samples_skipped_after_done_in_order():
    ep = Endpointer(lambda _c: 1.0, min_burst_s=0.032, max_s=0.064, preroll_s=0)
    frame = np.arange(FRAME, dtype=np.int16)
    assert ep.feed(frame) == "done"
    assert np.array_equal(ep.take_rest(), frame[1024:])
    assert ep.take_rest().size == 0


def test_take_rest_timeout_returns_whole_and_partial_unseen_units_once():
    ep = Endpointer(lambda _c: 0.0, wait_s=0.032)
    frame = np.arange(FRAME, dtype=np.int16)
    assert ep.feed(frame) == "timeout"
    assert np.array_equal(ep.take_rest(), frame[512:])


def test_waiting_then_speech_status():
    ep = Endpointer(scripted(lambda t: 1.0 if t >= 0.2 else 0.0))
    states = [ep.feed(ZERO) for _ in range(10)]
    assert states[0] == "waiting" and states[-1] == "speech"


def test_wait_none_never_times_out():
    ep = Endpointer(scripted(lambda _t: 0.0), wait_s=None)
    for _ in range(3600 * 16000 // FRAME):
        assert ep.feed(ZERO) == "waiting"


def test_probs_contains_only_units_since_speech_start():
    ep = Endpointer(scripted(lambda t: 1.0 if t >= 0.32 else 0.0), wait_s=None)
    for _ in range(8):
        ep.feed(ZERO)
    assert ep.status == "speech"
    assert len(ep.probs) == 10 and all(p >= 0.5 for p in ep.probs)


def test_silent_prob_history_stays_empty():
    ep = Endpointer(scripted(lambda _t: 0.0), wait_s=None)
    for _ in range(1000):
        ep.feed(ZERO)
    assert ep.probs == []


def test_default_arguments_match_explicit_defaults():
    fn = lambda t: 1.0 if 0.4 <= t < 1.8 else 0.0
    default = Endpointer(scripted(fn))
    explicit = Endpointer(scripted(fn), end_silence=0.7, wait_s=4.0, max_s=15.0,
                          min_burst_s=0.25, preroll_s=0.3, threshold=0.5,
                          reset=True, soft_after_s=None, soft_silence=0.25)
    for _ in range(100):
        a, b = default.feed(ZERO), explicit.feed(ZERO)
        assert a == b
        if a in ("done", "timeout"):
            break
    assert default.reason == explicit.reason
    assert np.array_equal(default.audio, explicit.audio)


@pytest.mark.models
def test_silero_fixture_end():
    meta = json.loads((FIXTURES / "fixtures.json").read_text())
    ep = Endpointer(silero())
    s = None
    for f in list(wav_frames(FIXTURES / "cmd.wav")) + [ZERO] * 40:
        s = ep.feed(f)
        if s in ("done", "timeout"):
            break
    assert s == "done"
    assert abs(ep.speech_end_s - meta["cmd.wav"]["speech_end_s"]) <= 0.1


@pytest.mark.models
def test_silero_noise_times_out():
    ep = Endpointer(silero())
    frames = list(wav_frames(FIXTURES / "noise.wav")) + [ZERO] * 40
    for i, f in enumerate(frames):
        s = ep.feed(f)
        if s in ("done", "timeout"):
            break
    assert s == "timeout" and abs((i + 1) * FRAME / 16000 - 4.0) <= 0.1


@pytest.mark.parametrize("speech_s, end_at", [(1.0, 1.7), (3.0, 4.2)])
def test_short_speech_ends_on_short_silence(speech_s, end_at):
    # User, 2026-11-17: 1.2 s feels slow after a quick command, right for long sentences.
    ep = Endpointer(scripted(lambda t: 1.0 if t < speech_s else 0.0), end_silence=1.2, short_silence=0.7)
    s, t = run(ep, 8)
    assert s == "done" and ep.reason == "silence" and abs(t - end_at) <= 0.1
