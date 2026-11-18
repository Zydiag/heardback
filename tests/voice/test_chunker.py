import pytest

np = pytest.importorskip("numpy")

from heardback.voice.chunker import Chunker

RATE = 16000
FRAME = 1280


def scripted(fn):
    n = [0]

    def prob(chunk):
        t = n[0] * 512 / RATE
        n[0] += 1
        return fn(t)

    return prob


def frames(seconds, start=0):
    count = round(seconds * RATE / FRAME)
    return [((np.arange(FRAME, dtype=np.int64) + start + i * FRAME) % 30000 + 1).astype(np.int16)
            for i in range(count)]


def test_silence_closes_at_0_6s():
    c = Chunker(scripted(lambda t: 1.0 if 0.2 <= t < 1.0 else 0.0))
    chunks = []
    for f in frames(2):
        chunks.extend(c.feed(f))
    assert len(chunks) == 1 and chunks[0].reason == "silence"
    assert abs(len(chunks[0].audio) / RATE - 1.4) <= 512 / RATE
    assert chunks[0].voiced_s == 0.8


def test_soft_cap_uses_short_silence_after_8s():
    c = Chunker(scripted(lambda t: 1.0 if t < 8.2 else 0.0))
    out = []
    for f in frames(9.5):
        out.extend(c.feed(f))
    assert len(out) == 1 and out[0].reason == "soft_cap"
    assert abs(len(out[0].audio) / RATE - 8.45) <= 512 / RATE


def test_hard_cap_cuts_at_lowest_vad_in_last_2s():
    def prob(t):
        if t < 18.0:
            return 0.9
        return 0.1 if 19.1 <= t < 19.3 else 0.6

    c = Chunker(scripted(prob))
    out, source = [], []
    for f in frames(21):
        source.extend(f)
        out.extend(c.feed(f))
    tail = c.flush()
    if tail:
        out.append(tail)
    assert out[0].reason == "hard_cap"
    assert 19.0 <= len(out[0].audio) / RATE <= 19.4
    assert out[0].voiced_s == 597 * 512 / RATE
    assert out[0].speech_end_s == 597 * 512 / RATE
    assert np.array_equal(np.concatenate([x.audio for x in out]), np.asarray(source))


def identity_stream(unit_count, low_ranges=()):
    units = [np.full(512, i + 1, np.int16) for i in range(unit_count)]
    audio = np.concatenate(units)
    visits = []

    def prob(unit):
        ident = int(unit[0]) - 1
        visits.append(ident)
        return 0.01 if any(a <= ident < b for a, b in low_ranges) else 0.9

    return audio, units, visits, prob


def feed_1280(chunker, audio):
    out = []
    for i in range(0, len(audio), FRAME):
        out.extend(chunker.feed(audio[i:i + FRAME]))
    tail = chunker.flush()
    if tail:
        out.append(tail)
    return out


def test_hard_cap_keeps_scored_remainder_and_scores_each_source_unit_once():
    source, units, visits, prob = identity_stream(657, ((600, 605),))
    chunks = feed_1280(Chunker(prob), source)
    assert len(chunks) >= 2
    assert np.array_equal(np.concatenate([c.audio for c in chunks]), source)
    assert visits == list(range(657))
    assert chunks[0].voiced_s == 600 * 512 / RATE
    assert chunks[0].speech_end_s == 600 * 512 / RATE
    assert chunks[-1].speech_end_s == 657 * 512 / RATE


def test_hard_cap_searches_full_final_two_seconds_for_unique_minimum():
    dip = round(18.4 * RATE / 512)
    source, _, visits, prob = identity_stream(657, ((dip, dip + 1),))
    chunks = feed_1280(Chunker(prob), source)
    assert np.array_equal(np.concatenate([c.audio for c in chunks]), source)
    assert len(chunks[0].audio) == (dip + 1) * 512
    assert chunks[0].speech_end_s == dip * 512 / RATE
    assert visits == list(range(657))


def test_long_speech_without_pauses_keeps_flowing():
    c = Chunker(scripted(lambda t: 1.0))
    source, out = [], []
    for f in frames(60):
        source.extend(f)
        out.extend(c.feed(f))
    tail = c.flush()
    if tail:
        out.append(tail)
    assert len(out) >= 3
    assert [x.seq for x in out] == list(range(len(out)))
    assert np.array_equal(np.concatenate([x.audio for x in out]), np.asarray(source))
    assert all(x.voiced_s == len(x.audio) / RATE for x in out)


def test_frame_remainder_survives_chunk_close():
    c = Chunker(scripted(lambda t: 1.0 if t < 1.0 or t >= 1.664 else 0.0))
    out = []
    for f in frames(2.5):
        out.extend(c.feed(f))
    tail = c.flush()
    if tail:
        out.append(tail)
    assert len(out) == 2
    source = np.concatenate(frames(2.5))
    assert np.array_equal(out[0].audio, source[:51 * 512])
    assert np.array_equal(out[1].audio, source[52 * 512:])
    assert out[1].speech_end_s > out[0].speech_end_s


def test_flush_keeps_trailing_speech():
    c = Chunker(scripted(lambda t: 1.0))
    out = []
    for f in frames(1):
        out.extend(c.feed(f))
    chunk = c.flush()
    assert chunk and chunk.reason == "stop" and chunk.voiced_s >= 0.3
    assert chunk.seq == len(out)
    assert np.array_equal(chunk.audio, np.concatenate(frames(1)))


def test_flush_with_no_speech_returns_none():
    c = Chunker(scripted(lambda t: 0.0))
    for f in frames(1):
        assert c.feed(f) == []
    assert c.flush() is None


def test_short_voiced_chunk_dropped_under_0_3s():
    c = Chunker(scripted(lambda t: 1.0 if 0.2 <= t < 0.4 else 0.0))
    out = []
    for f in frames(2):
        out.extend(c.feed(f))
    assert out == []


def test_seq_is_gapless_after_a_drop():
    c = Chunker(scripted(lambda t: 1.0 if (0.2 <= t < 0.4) or (1.0 <= t < 1.6) or (2.5 <= t < 3.2) else 0.0))
    out = []
    for f in frames(4):
        out.extend(c.feed(f))
    assert len(out) == 2 and [x.seq for x in out] == [0, 1]


def test_no_timeout_when_silent_for_an_hour():
    c = Chunker(scripted(lambda _t: 0.0))
    zero = np.zeros(FRAME, np.int16)
    for _ in range(3600 * RATE // FRAME):
        assert c.feed(zero) == []
    assert c.flush() is None


def test_speech_is_still_scored_after_an_hour_of_silence():
    n = [0]

    def prob(_unit):
        n[0] += 1
        return 0.0 if n[0] <= 3600 * RATE // 512 else 1.0

    c = Chunker(prob)
    zero = np.zeros(FRAME, np.int16)
    for _ in range(3600 * RATE // FRAME):
        c.feed(zero)
    before = n[0]
    for _ in range(20):
        c.feed(np.ones(FRAME, np.int16))
    assert n[0] > before and c._ep.status == "speech"


def test_flush_twice_has_no_empty_chunk_or_sequence_gap():
    c = Chunker(scripted(lambda t: 1.0 if t < 1.0 else 0.0))
    out = []
    for f in frames(1.5):
        out.extend(c.feed(f))
    first = c.flush()
    assert c.flush() is None
    if first:
        out.append(first)
    assert [x.seq for x in out] == list(range(len(out)))


def test_speech_end_time_is_stream_time_across_chunks():
    c = Chunker(scripted(lambda _t: 1.0), hard_cap_s=1.0)
    out = []
    for f in frames(2.5):
        out.extend(c.feed(f))
    tail = c.flush()
    if tail:
        out.append(tail)
    assert len(out) >= 2
    assert all(a.speech_end_s < b.speech_end_s for a, b in zip(out, out[1:]))
    assert abs(out[-1].speech_end_s - 2.464) <= 512 / RATE


def test_timestamp_tracks_two_separated_utterances_in_stream_time():
    c = Chunker(scripted(lambda t: 1.0 if (0 <= t < 1) or (2 <= t < 3) else 0.0))
    out = []
    for f in frames(4):
        out.extend(c.feed(f))
    assert len(out) == 2
    assert abs(out[0].speech_end_s - 1.024) <= 512 / RATE
    assert abs(out[1].speech_end_s - 3.008) <= 512 / RATE
    assert out[0].speech_end_s == 32 * 512 / RATE
    assert round(out[1].speech_end_s * RATE / 512) == 94


def test_flush_drops_rejected_burst_duration():
    c = Chunker(scripted(lambda t: 1.0 if (0 <= t < 0.064) or (0.32 <= t < 0.576) else 0.0))
    for f in frames(1.0):
        c.feed(f)
    assert c.flush() is None


def test_hard_cap_drops_short_prefix_but_retains_remainder():
    unit_count = 37
    source = np.concatenate([np.full(512, i + 1, np.int16) for i in range(unit_count)])
    visits = []

    def prob(unit):
        i = int(unit[0]) - 1
        visits.append(i)
        return 0.01 if i == 5 else 0.9

    c = Chunker(prob, hard_cap_s=0.64, min_burst_s=0.032, min_voiced_s=0.3)
    out = feed_1280(c, source)
    assert len(out) >= 2 and out[0].seq == 0 and out[0].reason == "hard_cap"
    assert np.array_equal(out[0].audio, source[6 * 512:26 * 512])
    assert out[0].voiced_s == 20 * 512 / RATE
    assert np.array_equal(np.concatenate([x.audio for x in out]), source[6 * 512:])
    assert visits == list(range(unit_count))


def test_flush_immediately_after_automatic_close_does_not_repeat_or_gap_seq():
    c = Chunker(scripted(lambda t: 1.0 if t < 0.5 else 0.0))
    out = []
    for f in frames(1.5):
        out.extend(c.feed(f))
    assert len(out) == 1
    assert c.flush() is None
    assert out[0].seq == 0


def test_silero_state_not_reset_between_chunks():
    class Prob:
        def __init__(self):
            self.resets = 0
            self.visits = 0

        def reset(self):
            self.resets += 1

        def __call__(self, _chunk):
            self.visits += 1
            return 1.0

    p = Prob()
    c = Chunker(p, hard_cap_s=1)
    out = []
    for f in frames(2.5):
        out.extend(c.feed(f))
    tail = c.flush()
    if tail:
        out.append(tail)
    assert p.resets == 0
    assert len(out) >= 2 and p.visits == 77
