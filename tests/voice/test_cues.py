import pytest

np = pytest.importorskip("numpy")

from heardback.voice.cues import CUES, OUT_RATE, Cues, play_pcm, tone


def test_tone_shape():
    t = tone(880, 1320, 80)
    assert len(t) == 1920 and t.dtype == np.float32
    assert abs(t).max() <= 0.25
    assert abs(t[0]) < 1e-3 and abs(t[-1]) < 1e-2


def test_error_has_gap():
    e = CUES["error"]
    assert len(e) == int(0.2 * OUT_RATE)
    mid = len(e) // 2
    gap = int(0.04 * OUT_RATE)
    assert not e[mid - gap // 2 + 1: mid + gap // 2 - 1].any()


def test_play_pcm_pipes_raw_bytes(on_path, tmp_path, monkeypatch):
    out, args = tmp_path / "out", tmp_path / "args"
    monkeypatch.setenv("OUT", str(out))
    on_path("pw-play", f'echo "$*" > {args}; cat > "$OUT"')
    pcm = CUES["wake"]
    play_pcm(pcm)
    assert out.stat().st_size == len(pcm) * 4
    assert "--format f32" in args.read_text()


def test_notify_off_runs_nothing(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    Cues(notify=False).notify("hi")
    Cues(notify=True).notify("hi")  # missing notify-send: swallowed


def test_sticky_notify_never_expires(tmp_path, on_path):
    args = tmp_path / "args"
    on_path("notify-send", f'echo "$*" >> {args}')
    Cues(notify=True).notify("Which window?", sticky=True)
    Cues(notify=True).notify("Listening…")
    sticky, plain = args.read_text().splitlines()
    assert "-t 0" in sticky and "x-canonical-private-synchronous:heardback" in sticky and "Which window?" in sticky
    assert "-t 0" not in plain
