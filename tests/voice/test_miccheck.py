import pytest

pytest.importorskip("numpy")

from heardback.voice.miccheck import PROMPTS, wer
from heardback.voice import miccheck


def test_wer():
    assert wer("switch to window 3", "switch to window 3") == 0
    assert wer("a b c d", "a x c") == 0.5
    assert wer("close the tab", "") == 1.0
    assert wer("Switch to window 3.", "switch to window 3") == 0


def test_prompts():
    assert len(PROMPTS) == 20 and len(set(PROMPTS)) == 20


def test_custom_mode_never_benchmarks_a_fallback(monkeypatch, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[mic]\nmode = "usb"\n[mic.modes]\nusb = ["usb_*", "builtin"]\n')
    monkeypatch.setenv("HEARDBACK_CONFIG", str(config))
    monkeypatch.setattr(miccheck, "pactl_json", lambda *a: [{"name": "usb_headset"}, {"name": "builtin"}])
    assert miccheck.pick_node("usb") == "usb_headset"
    monkeypatch.setattr(miccheck, "pactl_json", lambda *a: [{"name": "builtin"}])
    assert miccheck.pick_node("usb") is None
