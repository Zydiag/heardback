import json
import subprocess

import pytest

from jv import calculator, hypr, settings, system_facts


@pytest.fixture(autouse=True)
def isolated_hypr(monkeypatch):
    monkeypatch.delenv("JV_DRY", raising=False)
    monkeypatch.setattr(hypr, "_inst", "test-instance")


class Runner:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return subprocess.CompletedProcess(argv, *reply)


@pytest.mark.parametrize("verb,args,expected,reply,say", [
    ("settings.dnd", {"state": "on"}, [["swaync-client", "-dn"]], [(0, "true\n", "")], "Do not disturb is on."),
    ("settings.dnd", {"state": "off"}, [["swaync-client", "-df"]], [(0, "false\n", "")], "Do not disturb is off."),
    ("settings.night", {"state": "on"}, [["hyprctl", "-i", "test-instance", "hyprsunset", "temperature", "4500"], ["hyprctl", "-i", "test-instance", "hyprsunset", "temperature"]], [(0, "ok", ""), (0, "4500\n", "")], "Night light is on at 4500 kelvin."),
    ("settings.night", {"state": "off"}, [["hyprctl", "-i", "test-instance", "hyprsunset", "identity"]], [(0, "ok", "")], "Night light off; I couldn't confirm its state."),
    ("settings.night", {"state": "warmer"}, [["hyprctl", "-i", "test-instance", "hyprsunset", "temperature"], ["hyprctl", "-i", "test-instance", "hyprsunset", "temperature", "3000"], ["hyprctl", "-i", "test-instance", "hyprsunset", "temperature"]], [(0, "3500\n", ""), (0, "ok", ""), (0, "3000\n", "")], "Night light is warmer at 3000 kelvin."),
    ("settings.power", {"profile": "power-saver"}, [["powerprofilesctl", "set", "power-saver"], ["powerprofilesctl", "get"]], [(0, "", ""), (0, "power-saver\n", "")], "Power profile is power-saver."),
    ("settings.output", {"name": "Headphones"}, [["wpctl", "status"], ["wpctl", "set-default", "52"], ["wpctl", "status"]], [(0, "Audio\n ├─ Sinks:\n │  * 42. Speakers [vol: 0.50]\n │    52. Headphones [vol: 1.00]\n", ""), (0, "", ""), (0, "Audio\n ├─ Sinks:\n │    42. Speakers [vol: 0.50]\n │  * 52. Headphones [vol: 1.00]\n", "")], "Audio output is Headphones."),
    ("settings.mic", {"state": "mute"}, [["wpctl", "get-volume", "@DEFAULT_AUDIO_SOURCE@"], ["wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", "1"], ["wpctl", "get-volume", "@DEFAULT_AUDIO_SOURCE@"]], [(0, "Volume: 1.00\n", ""), (0, "", ""), (0, "Volume: 1.00 [MUTED]\n", "")], "Microphone is muted."),
    ("settings.window", {"action": "fullscreen"}, [["hyprctl", "-i", "test-instance", "-j", "activewindow"], ["hyprctl", "-i", "test-instance", "dispatch", 'hl.dsp.window.fullscreen({mode = "fullscreen", action = "set", window = "address:0x1"})'], ["hyprctl", "-i", "test-instance", "-j", "activewindow"]], [(0, '{"address":"0x1","fullscreen":0,"floating":false}', ""), (0, "ok", ""), (0, '{"address":"0x1","fullscreen":2,"floating":false}', "")], "Window is fullscreen."),
    ("settings.window", {"action": "float"}, [["hyprctl", "-i", "test-instance", "-j", "activewindow"], ["hyprctl", "-i", "test-instance", "dispatch", 'hl.dsp.window.float({action = "set", window = "address:0x1"})'], ["hyprctl", "-i", "test-instance", "-j", "activewindow"]], [(0, '{"address":"0x1","fullscreen":0,"floating":false}', ""), (0, "ok", ""), (0, '{"address":"0x1","fullscreen":0,"floating":true}', "")], "Window is floating."),
    ("settings.theme", {"scheme": "dark"}, [["gsettings", "set", "org.gnome.desktop.interface", "color-scheme", "prefer-dark"], ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"]], [(0, "", ""), (0, "'prefer-dark'\n", "")], "Color scheme is dark."),
])
def test_settings_exact_commands_and_readback(monkeypatch, run_jv, verb, args, expected, reply, say):
    runner = Runner(reply)
    monkeypatch.setattr(settings.subprocess, "run", runner)
    code, out, _ = run_jv(*verb.split("."), *args.values(), "--json")
    assert code == 0 and out["say"] == say
    assert [call[0] for call in runner.calls] == expected
    assert all(call[1]["capture_output"] and call[1]["text"] and not call[1].get("shell") for call in runner.calls)


@pytest.mark.parametrize("verb", settings.VERBS + system_facts.VERBS + calculator.VERBS, ids=lambda v: v.name)
def test_dry_run_never_calls_program(monkeypatch, run_jv, verb):
    def forbidden(*args, **kwargs):
        pytest.fail("external command ran")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    props = verb.params.get("properties", {})
    args = [props[key].get("enum", ["2 + 2"])[0] for key in verb.argv]
    assert run_jv(*verb.name.split("."), *args, "--dry-run", "--json")[0] == 0


@pytest.mark.parametrize("reply", [FileNotFoundError(), subprocess.CompletedProcess([], 1, "", "failed"), subprocess.CompletedProcess([], 0, "odd", "")])
def test_settings_command_failures_are_one_line(monkeypatch, run_jv, reply):
    monkeypatch.setattr(settings.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(reply) if isinstance(reply, Exception) else reply)
    code, out, _ = run_jv("settings", "dnd", "state", "--json")
    assert code == 1 and out["ok"] is False and out.get("say")
    assert "\n" not in out["say"]


@pytest.mark.parametrize("output", ["", "Sinks:\n  4. Headphones [vol: 0.50]\n  5. Headphones [vol: 1.00]\n"])
def test_output_requires_one_named_sink(monkeypatch, run_jv, output):
    monkeypatch.setattr(settings.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, output, ""))
    code, out, _ = run_jv("settings", "output", "Headphones", "--json")
    assert code == 1 and out["say"] == "I couldn't find one matching audio output."


def test_dnd_already_on_and_theme_key_missing(monkeypatch, run_jv):
    runner = Runner([(0, "true\n", "")])
    monkeypatch.setattr(settings.subprocess, "run", runner)
    assert run_jv("settings", "dnd", "on", "--json")[1]["say"] == "Do not disturb is on."
    runner = Runner([(1, "", "No such key 'color-scheme'")])
    monkeypatch.setattr(settings.subprocess, "run", runner)
    assert run_jv("settings", "theme", "dark", "--json")[0] == 1


# Trimmed wpctl capture; second sink and video rows exercise section boundaries.
AUDIO = """Audio
 ├─ Sinks:
 │  *   67. Built-in Audio Analog Stereo        [vol: 0.70]
 │      52. Headphones                         [vol: 1.00 MUTED]
 │
 ├─ Sources:
 │  *   68. Built-in Audio Analog Stereo        [vol: 0.80]
 └─ Streams:
        87. pw-record
Video
 ├─ Sinks:
 │      90. Video Sink
 └─ Sources:
 │      80. ASUS FHD webcam (V4L2)
"""


def test_list_outputs_reads_only_audio_sinks(monkeypatch, run_jv):
    runner = Runner([(0, AUDIO, "")])
    monkeypatch.setattr(subprocess, "run", runner)
    code, result, _ = run_jv("settings", "outputs", "--json")
    assert code == 0 and result["say"] == "Audio outputs are Built-in Audio Analog Stereo, Headphones."
    assert [argv for argv, _ in runner.calls] == [["wpctl", "status"]]


def test_get_power_profile_is_read_only(monkeypatch, run_jv):
    runner = Runner([(0, "balanced\n", "")])
    monkeypatch.setattr(subprocess, "run", runner)
    code, result, _ = run_jv("settings", "power_get", "--json")
    assert code == 0 and result["say"] == "Power profile is balanced."
    assert [argv for argv, _ in runner.calls] == [["powerprofilesctl", "get"]]


@pytest.mark.parametrize("verb", ["power_get", "outputs"])
@pytest.mark.parametrize("failure", [FileNotFoundError(), (1, "", "failed"), (0, "", ""), (0, "unexpected", "")])
def test_read_settings_fail_with_plain_sentence(monkeypatch, run_jv, verb, failure):
    monkeypatch.setattr(subprocess, "run", Runner([failure]))
    code, result, err = run_jv("settings", verb, "--json")
    assert code == 1 and result["say"] and "Traceback" not in err


@pytest.mark.parametrize("before,after,wanted", [(3000, 2500, 2500), (3500, 3000, 3000), (1100, 1000, 1000), (1000, 1000, 1000)])
def test_warmer_always_lowers_temperature_to_minimum(monkeypatch, run_jv, before, after, wanted):
    runner = Runner([(0, str(before), ""), (0, "ok", ""), (0, str(after), "")])
    monkeypatch.setattr(subprocess, "run", runner)
    code, result, _ = run_jv("settings", "night", "warmer", "--json")
    assert code == 0 and f"{after} kelvin" in result["say"]
    assert runner.calls[1][0] == ["hyprctl", "-i", "test-instance", "hyprsunset", "temperature", str(wanted)]
    assert ("at its warmest" in result["say"]) == (after == 1000)


@pytest.mark.parametrize("raw", ["", "odd", "999", "20001"])
def test_invalid_night_temperature_does_not_mutate(monkeypatch, run_jv, raw):
    runner = Runner([(0, raw, "")])
    monkeypatch.setattr(subprocess, "run", runner)
    code, result, _ = run_jv("settings", "night", "warmer", "--json")
    assert code == 1 and result["say"] == "I couldn't confirm night light."
    assert len(runner.calls) == 1


@pytest.mark.parametrize("before", [0, 1, 2])
def test_fullscreen_numeric_modes_and_repeated_request(monkeypatch, run_jv, before):
    window = {"address": "0x55b164343e10", "fullscreen": before, "floating": False}
    done = {**window, "fullscreen": 2}
    replies = [(0, json.dumps(window), "")]
    if before != 2:
        replies += [(0, "ok", ""), (0, json.dumps(done), "")]
    replies += [(0, json.dumps(done), "")]
    runner = Runner(replies)
    monkeypatch.setattr(subprocess, "run", runner)
    assert run_jv("settings", "window", "fullscreen", "--json")[0] == 0
    assert run_jv("settings", "window", "fullscreen", "--json")[1]["say"] == "Window is already fullscreen."
    dispatches = [argv for argv, _ in runner.calls if "dispatch" in argv]
    assert len(dispatches) == (0 if before == 2 else 1)
    if dispatches:
        assert dispatches[0][-1] == 'hl.dsp.window.fullscreen({mode = "fullscreen", action = "set", window = "address:0x55b164343e10"})'


@pytest.mark.parametrize("raw", ["", "{", "[]", "null", "{}", '{"address":7}', '{"address":"0x1","fullscreen":true}', '{"address":"0x1","fullscreen":[]}', '{"address":"0x1;bad","fullscreen":0}'])
@pytest.mark.parametrize("phase", ["before", "after"])
def test_invalid_window_output_is_spoken(monkeypatch, run_jv, raw, phase):
    replies = [(0, raw, "")]
    if phase == "after":
        replies = [(0, '{"address":"0x1","fullscreen":0}', ""), (0, "ok", ""), *replies]
    runner = Runner(replies)
    monkeypatch.setattr(subprocess, "run", runner)
    code, result, err = run_jv("settings", "window", "fullscreen", "--json")
    assert code == 1 and result["say"] and "Traceback" not in err
    assert len(runner.calls) == (1 if phase == "before" else 3)


@pytest.mark.parametrize("verb,arg,replies", [
    ("night", "on", [(0, "ok", ""), (0, "3000", "")]),
    ("power", "performance", [(0, "", ""), (0, "balanced", "")]),
    ("output", "Headphones", [(0, AUDIO, ""), (0, "", ""), (0, AUDIO, "")]),
    ("mic", "mute", [(0, "Volume: 1.00", ""), (0, "", ""), (0, "Volume: 1.00", "")]),
    ("window", "fullscreen", [(0, '{"address":"0x1","fullscreen":0}', ""), (0, "ok", ""), (0, '{"address":"0x1","fullscreen":1}', "")]),
    ("window", "fullscreen", [(0, '{"address":"0x1","fullscreen":0}', ""), (0, "ok", ""), (0, '{"address":"0x2","fullscreen":2}', "")]),
    ("window", "float", [(0, '{"address":"0x1","floating":false}', ""), (0, "ok", ""), (0, '{"address":"0x1","floating":false}', "")]),
])
def test_mismatched_readback_is_spoken_failure(monkeypatch, run_jv, verb, arg, replies):
    monkeypatch.setattr(subprocess, "run", Runner(replies))
    code, result, err = run_jv("settings", verb, arg, "--json")
    assert code == 1 and "couldn't confirm" in result["say"] and "Traceback" not in err


def test_hypr_uses_newest_instance_and_ignores_stale_environment(monkeypatch, run_jv):
    monkeypatch.setattr(hypr, "_inst", None)
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "stale")
    runner = Runner([(0, '[{"instance":"old","time":1},{"instance":"new","time":2}]', ""),
                     (0, '{"address":"0x1","floating":true}', "")])
    monkeypatch.setattr(subprocess, "run", runner)
    assert run_jv("settings", "window", "float", "--json")[0] == 0
    assert [argv for argv, _ in runner.calls] == [["hyprctl", "-j", "instances"], ["hyprctl", "-i", "new", "-j", "activewindow"]]


@pytest.mark.parametrize("verb,arg", [("window", "fullscreen"), ("night", "on")])
@pytest.mark.parametrize("failure", [FileNotFoundError(), subprocess.TimeoutExpired("hyprctl", 10), (1, "", "failed")])
def test_hypr_failures_are_spoken(monkeypatch, run_jv, verb, arg, failure):
    monkeypatch.setattr(subprocess, "run", Runner([failure]))
    code, result, err = run_jv("settings", verb, arg, "--json")
    assert code == 1 and result["say"] and "Traceback" not in err

