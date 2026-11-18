import json
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("numpy")

from conftest import FIXTURES
from heardback.voice import devices
from heardback.voice.devices import other_recorders, parse_event, pick_mic

LAPTOP = "alsa_input.pci-0000_00_1f.3.analog-stereo"
BUDS = "bluez_input.88:92:CC:F1:A1:01.0"


@pytest.fixture(autouse=True)
def user_modes(monkeypatch, tmp_path):
    config = tmp_path / "desktop.toml"
    config.write_text('[mic]\nmode = "laptop"\n[mic.modes]\nlaptop = ["alsa_input.*"]\n'
                      'buds = ["bluez_input.*", "alsa_input.*"]\n')
    monkeypatch.setenv("HEARDBACK_CONFIG", str(config))


def load(name):
    return json.loads((FIXTURES / name).read_text())


def test_pick_laptop_with_and_without_buds():
    for f in ("sources_laptop.json", "sources_buds.json"):
        assert pick_mic(load(f), "laptop", "auto") == (LAPTOP, False)


def test_pick_buds_mode():
    assert pick_mic(load("sources_buds.json"), "buds", "auto") == (BUDS, False)
    assert pick_mic(load("sources_laptop.json"), "buds", "auto") == (LAPTOP, True)
    assert pick_mic(load("sources_buds.json"), "buds", "auto", fallback=True) == (LAPTOP, True)


def test_explicit_node_missing_falls_back_to_auto():
    assert pick_mic(load("sources_buds.json"), "laptop", BUDS) == (BUDS, False)
    assert pick_mic(load("sources_laptop.json"), "laptop", BUDS) == (LAPTOP, False)
    with pytest.raises(ValueError, match="no mic for mode laptop"):
        pick_mic([s for s in load("sources_laptop.json") if s["name"] != LAPTOP], "laptop", "auto")


def test_default_mode_uses_system_source(monkeypatch):
    monkeypatch.delenv("HEARDBACK_CONFIG")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout=BUDS + "\n")

    monkeypatch.setattr(devices.subprocess, "run", run)
    assert pick_mic(load("sources_buds.json"), "default", "auto") == (BUDS, False)
    assert calls == [["pactl", "get-default-source"]]
    with pytest.raises(ValueError):
        pick_mic(load("sources_laptop.json"), "default", "auto")
    assert pick_mic(load("sources_buds.json"), "default", LAPTOP) == (LAPTOP, False)


def test_custom_mode_fallback_monitor_and_pci_preference(monkeypatch, tmp_path):
    p = tmp_path / "custom.toml"
    p.write_text('[mic]\nmode = "headset"\n[mic.modes]\nheadset = ["usb_*", "alsa_input.*"]\n')
    monkeypatch.setenv("HEARDBACK_CONFIG", str(p))
    usb = {"name": "usb_headset"}
    sources = [dict(usb, name="usb_headset.monitor"), *load("sources_laptop.json")]
    assert pick_mic([usb, *sources], "headset", "auto") == ("usb_headset", False)
    assert pick_mic(sources, "headset", "auto") == (LAPTOP, True)
    with pytest.raises(ValueError, match="unknown mic mode"):
        pick_mic(sources, "unknown", "auto")


def test_missing_pactl_or_monitor_default_is_unavailable(monkeypatch):
    monkeypatch.delenv("HEARDBACK_CONFIG")

    def missing(*args, **kwargs):
        raise FileNotFoundError("pactl")

    monkeypatch.setattr(devices.subprocess, "run", missing)
    with pytest.raises(ValueError, match="no mic"):
        pick_mic(load("sources_laptop.json"), "default", "auto")
    monkeypatch.setattr(devices.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="output.monitor\n"))
    with pytest.raises(ValueError, match="no mic"):
        pick_mic([{"name": "output.monitor"}], "default", "auto")


def test_parse_event():
    assert parse_event("Event 'new' on source #77") == "source"
    assert parse_event("Event 'remove' on source-output #12") == "source-output"
    assert parse_event("Event 'change' on card #3") == "card"
    assert parse_event("Event 'change' on client #3") is None


def test_other_recorders_ignores_monitors_and_self():
    assert other_recorders(load("source_outputs.json"), load("sources_laptop.json")) == ["Zen"]


def test_watcher_debounces_and_respawns(on_path, tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    on_path("pactl", f"echo x >> {runs}\n"
            "echo \"Event 'new' on source #1\"; echo \"Event 'change' on source #1\"\n"
            "echo \"Event 'new' on source-output #2\"; echo \"Event 'change' on client #3\"")
    monkeypatch.setattr(devices, "backoff", lambda n: 0.3)
    got = []
    w = devices.Watcher(got.append)
    w.start()
    time.sleep(1.0)
    w.stop()
    assert got[:2] == ["source", "source-output"]
    assert len(runs.read_text().split()) >= 2
