import asyncio
import time

import pytest

np = pytest.importorskip("numpy")

from heardback.voice.tts import Tts, cached, load_kokoro, louder


@pytest.fixture
def player(on_path, tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("OUT", str(out))
    on_path("pw-play", 'cat >> "$OUT"')
    return out


def recorder(delay=0.0):
    calls = []

    def synth(s):
        calls.append(s)
        time.sleep(delay)
        return np.full(2400, 0.1, np.float32)
    return synth, calls


async def test_say_writes_all_sentences_in_order(player):
    synth, calls = recorder()
    t = Tts("bm_george", synth=synth)
    await t.say("One. Two.")
    assert t.speaking
    await t.finish()
    assert calls == ["One.", "Two."]
    assert player.stat().st_size == 2 * 2400 * 4
    assert not t.speaking and t.first_audio_at is not None


@pytest.mark.parametrize(("text", "expected"), [
    ("closed window 4", ["closed window 4."]),
    ("Done.", ["Done."]),
    ("Done!", ["Done!"]),
    ("Done?", ["Done?"]),
    ("Wait...", ["Wait..."]),
    ("Wait…", ["Wait…"]),
    ('He said "Done."', ['He said "Done."']),
    ("He said ‘Done!’", ["He said ‘Done!’"]),
    ("(Done.)", ["(Done.)"]),
    ("[Done?]", ["[Done?]"]),
    ('(“Done!”)', ['(“Done!”)']),
    ("Done。", ["Done。"]),
    ("closed window 4   ", ["closed window 4."]),
    ("One. Two! Three", ["One.", "Two!", "Three."]),
    ("", []),
    ("   ", []),
])
async def test_say_adds_only_missing_sentence_ends(player, text, expected):
    synth, calls = recorder()
    t = Tts("bm_george", synth=synth)
    await t.say(text)
    await t.finish()
    assert calls == expected
    assert player.exists() == bool(expected)


async def test_say_caches_short_reply_with_added_stop(player):
    synth, calls = recorder()
    t = Tts("bm_george", synth=cached(synth))
    for text in ("Done", "Done.", "Done   "):
        await t.say(text)
        await t.finish()
    assert calls == ["Done."]
    assert player.stat().st_size == 3 * 2400 * 4


async def test_finish_without_say_is_noop(player):
    t = Tts("bm_george", synth=recorder()[0])
    await t.finish()
    assert not player.exists()


async def test_stop_kills_player_and_drops_queue(player):
    synth, calls = recorder(0.2)
    t = Tts("bm_george", synth=synth)
    await t.say("A. B. C.")
    for _ in range(100):
        if player.exists() and player.stat().st_size:
            break
        await asyncio.sleep(0.02)
    proc = t._proc
    t.stop()
    await asyncio.sleep(0.5)
    assert proc.returncode is not None
    assert "C." not in calls and not t.speaking
    # a new turn works after stop
    await t.say("D.")
    await t.finish()
    assert calls[-1] == "D."


def test_cached_short_replies():
    n = []
    f = cached(lambda s: n.append(s) or np.zeros(1, np.float32))
    f("Done."); f("Done.")
    assert n == ["Done."]
    f("this has four words"); f("this has four words")
    assert n.count("this has four words") == 2


@pytest.mark.parametrize("playing", [False, True], ids=["before-writer-starts", "player-already-running"])
async def test_say_immediately_after_stop_plays_new_queue(player, playing):
    synth, calls = recorder()
    t = Tts("bm_george", synth=synth)
    await t.say("Old.")
    if playing:
        for _ in range(100):
            if player.exists() and player.stat().st_size:
                break
            await asyncio.sleep(0.01)
        assert t._proc is not None
    t.stop()
    await t.say("New.")
    await t.finish()
    assert calls[-1] == "New."
    assert player.stat().st_size == (2 if playing else 1) * 2400 * 4
    assert t.player_returncode == 0 and not t.stopped


@pytest.mark.models
def test_kokoro_real():
    a = load_kokoro("bm_george", "cuda")("Ready.")
    assert a.dtype == np.float32 and len(a) > 2400


def test_louder_scales_and_clips():
    f = louder(lambda s: np.array([0.1, -0.3, 0.5, -0.9], np.float32), 2.5)
    out = f("x")
    assert out.dtype == np.float32
    assert np.allclose(out, [0.25, -0.75, 1.0, -1.0])


async def test_first_audio_stamped_before_drain(on_path):
    # pw-play that reads nothing for 1 s: drain() blocks on ~1 MB, the stamp must not.
    on_path("pw-play", "sleep 1; cat > /dev/null")
    t = Tts("bm_george", synth=lambda s: np.full(250_000, 0.1, np.float32))
    t0 = time.monotonic()
    await t.say("One.")
    await t.finish()
    assert time.monotonic() - t0 >= 0.9
    assert t.first_audio_at - t0 < 0.5


async def test_say_speaks_leading_bang(player):
    synth, calls = recorder()
    t = Tts("bm_george", synth=synth)
    await t.say('Should I type "!ls" and press enter? Done!')
    await t.finish()
    assert calls == ['Should I type "exclamation mark ls" and press enter?', "Done!"]
