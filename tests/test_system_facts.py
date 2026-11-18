import subprocess

import pytest

from jv import system_facts as facts


@pytest.mark.parametrize("verb,argv,out,say,rc", [
    ("battery", ["upower", "-i", "/org/freedesktop/UPower/devices/DisplayDevice"], "percentage: 86%\nstate: discharging\n", "Battery is 86 percent, discharging.", 0),
    ("battery", ["upower", "-i", "/org/freedesktop/UPower/devices/DisplayDevice"], "present: no\n", "This computer has no battery.", 0),
    ("disk", ["df", "-B1", "/"], "Filesystem 1B-blocks Used Available Use% Mounted on\n/dev/x 1000 250 750 25% /\n", "Root disk is 25 percent used.", 0),
    ("memory", ["free", "-b"], "Mem: 1000 250 500 20 250 700\n", "Memory is 25 percent used.", 0),
    ("temperature", ["sensors"], "Package id 0:  +70.0°C  (high = +100.0°C, crit = +100.0°C)\nCore 0: +62.0°C\nin0: 0.00 V\ncpu_fan: 2700 RPM\ngpu_fan: 2800 RPM\n", "The hottest reported temperature is 70 degrees Celsius.", 0),
    ("gpu", ["nvidia-smi", "--query-gpu=name,temperature.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"], "RTX Test, 57, 100, 8000\n", "RTX Test is 57 degrees Celsius, using 100 of 8000 megabytes.", 0),
    ("updates", ["checkupdates"], "one 1.1 -> 1.2\ntwo 2.1 -> 2.2\n", "2 updates are available.", 0),
    ("updates", ["checkupdates"], "", "No updates are available.", 2),
    ("tailscale", ["tailscale", "status", "--json"], '{"BackendState":"Running"}', "Tailscale is running.", 0),
    ("online", ["nmcli", "-t", "-f", "CONNECTIVITY", "general"], "full\n", "You are online.", 0),
])
def test_fact_parsers_and_fixed_argv(monkeypatch, run_jv, verb, argv, out, say, rc):
    seen = []
    def fake(args, **kwargs):
        seen.append(args)
        return subprocess.CompletedProcess(args, rc, out, "")
    monkeypatch.setattr(facts.subprocess, "run", fake)
    code, result, _ = run_jv("system", verb, "--json")
    assert code == 0 and result["say"] == say
    assert seen == [argv]


@pytest.mark.parametrize("verb,argv", [
    ("battery", ["upower", "-i", "/org/freedesktop/UPower/devices/DisplayDevice"]),
    ("disk", ["df", "-B1", "/"]),
    ("memory", ["free", "-b"]), ("temperature", ["sensors"]),
    ("gpu", ["nvidia-smi", "--query-gpu=name,temperature.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"]),
    ("updates", ["checkupdates"]), ("tailscale", ["tailscale", "status", "--json"]),
    ("online", ["nmcli", "-t", "-f", "CONNECTIVITY", "general"]),
])
@pytest.mark.parametrize("failure", ["missing", "nonzero", "empty"])
def test_fact_failures_are_spoken_without_traceback(monkeypatch, run_jv, verb, argv, failure):
    def fake(args, **kwargs):
        assert args == argv
        if failure == "missing":
            raise FileNotFoundError
        return subprocess.CompletedProcess(args, 1 if failure == "nonzero" else 0, "" if failure == "empty" else "bad", "failed")
    monkeypatch.setattr(facts.subprocess, "run", fake)
    code, out, _ = run_jv("system", verb, "--json")
    assert code == 1 and out["ok"] is False and out["say"]


def test_online_odd_output_is_plain_failure(monkeypatch, run_jv):
    monkeypatch.setattr(facts.subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0, "unexpected", ""))
    code, out, _ = run_jv("system", "online", "--json")
    assert code == 1 and out["say"] == "I couldn't read network."


@pytest.mark.parametrize("raw", ["fan1: 3200 RPM\n", "in0: 12.0 V\npower1: 120.0 W\n", "temp1_input: 61.000\n"])
def test_non_temperature_readings_are_not_celsius(monkeypatch, run_jv, raw):
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, raw, ""))
    code, result, _ = run_jv("system", "temperature", "--json")
    assert code == 1 and result["say"] == "I couldn't read temperature."


def test_sensor_temperature_ignores_fan_speed_and_alarm_limits(monkeypatch, run_jv):
    raw = "fan1: 3200 RPM\nPackage id 0: +54.0°C (high = +100.0°C)\ntemp1: -5.0°C\n"
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, raw, ""))
    assert run_jv("system", "temperature", "--json")[1]["say"] == "The hottest reported temperature is 54 degrees Celsius."


@pytest.mark.parametrize("raw", ['{"BackendState":[]}', '{"BackendState":{}}', '{"BackendState":null}', '[]', 'null', '{}', '{'])
def test_tailscale_invalid_shapes_are_spoken(monkeypatch, run_jv, raw):
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, raw, ""))
    code, result, err = run_jv("system", "tailscale", "--json")
    assert code == 1 and result["say"] == "I couldn't read Tailscale."
    assert "Traceback" not in err

