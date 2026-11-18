"""jv volume / brightness / bt (sysctl): fake wpctl, brightnessctl, bluetoothctl, rfkill first on PATH. Never the real ones."""
import json
import os
import sys
import textwrap

import pytest

from jv import sysctl

FAKE = textwrap.dedent('''\
    import json, os, sys
    st_path = os.environ["FAKE_SYS"]
    st = json.load(open(st_path))
    tool, args = os.path.basename(sys.argv[0]), sys.argv[1:]
    st["log"].append([tool, *args])
    if tool == "bluetoothctl" and args[:1] == ["power"]:
        open(os.environ["FAKE_ENTERED"], "a").write("power\\n")
    out, rc = "", 0
    bt = st["bt"]
    if tool == "wpctl":
        if args[0] == "get-volume":
            out = f"Volume: {st['vol']:.2f}" + (" [MUTED]" if st["muted"] else "")
        elif args[0] == "set-volume":
            st["vol"] = float(args[2])
        elif args[0] == "set-mute":
            st["muted"] = args[2] == "1"
        elif args[0] == "inspect":
            s = st["sink"]
            out = (f'  * node.description = "{s[1]}"\\n' + (f'    api.bluez5.address = "{s[0]}"\\n' if s[0] else ""))
    elif tool == "brightnessctl":
        if args == ["-m"]:
            out = f"intel_backlight,backlight,{st['bri'] * 4},{st['bri']}%,400"
        elif args[:2] == ["-q", "set"]:
            st["bri"] = int(args[2].rstrip("%"))
    elif tool == "rfkill":
        if not bt.get("hard_blocked"):
            bt["blocked"] = False
    elif tool == "bluetoothctl":
        cmd = args[0]
        if cmd == "devices":
            ds = bt["paired"] if args[1] == "Paired" else [d for d in bt["paired"] if d[0] in bt["conn"]]
            out = "".join(f"Device {m} {n}\\n" for m, n in ds)
        elif cmd == "show":
            bt["show_calls"] = bt.get("show_calls", 0) + 1
            if bt.get("show_fail") or bt.get("show_fail_after", 999) < bt["show_calls"]:
                rc = 1
            if bt.get("power_pending"):
                bt["power_pending"]["shows"] -= 1
                if bt["power_pending"]["shows"] <= 0:
                    bt["on"] = bt["power_pending"]["on"]
                    if not bt["on"]:
                        bt["conn"] = []
                        if st["sink"][0]:
                            st["sink"] = ["", "Built-in Audio"]
                    del bt["power_pending"]
            out = "\\tPowered: " + ("yes" if bt["on"] else "no") + "\\n\\tPowerState: " + (
                "on" if bt["on"] else "off-blocked" if bt["blocked"] or bt.get("hard_blocked") else "off")
        elif cmd == "power":
            spec = bt.get("power_script", []).pop(0) if bt.get("power_script") else None
            if spec:
                out, rc = spec.get("out", ""), spec.get("rc", 0)
                if "on" in spec:
                    bt["power_pending"] = {"on": spec["on"], "shows": spec.get("shows", 1)}
            elif bt.get("power_timeout"):
                import time; time.sleep(30)
            elif args[1] == "on" and (bt["blocked"] or bt.get("hard_blocked")):
                out, rc = "Failed to set power on: org.bluez.Error.Blocked", 1
            else:
                bt["on"] = args[1] == "on"
                out = f"Changing power {args[1]} succeeded"
                if not bt["on"]:
                    bt["conn"] = []
                    if st["sink"][0]:
                        st["sink"] = ["", "Built-in Audio"]
        elif cmd == "connect":
            if args[1] in bt["fail"]:
                out, rc = "Failed to connect: org.bluez.Error.Failed", 1
            elif args[1] in bt.get("connect_after_fail", []):
                bt["conn"].append(args[1]); out, rc = "Failed after connection", 1
            elif args[1] in bt["hang"]:
                import time; time.sleep(30)
            else:
                bt["conn"].append(args[1]); out = "Connection successful"
        elif cmd == "disconnect":
            bt["conn"].remove(args[1])
            if args[1] in bt.get("disconnect_after_fail", []):
                out, rc = "Failed after disconnect", 1
            else:
                out = "Successful disconnected"
            if st["sink"][0] == args[1]:
                st["sink"] = ["", "Built-in Audio"]
    json.dump(st, open(st_path, "w"))
    print(out)
    sys.exit(rc)
''')

BUDS, JBL, H5, H3 = "88:92:CC:F1:A1:01", "90:F2:60:1E:07:F2", "CD:A1:27:22:35:61", "12:34:00:EF:2E:6E"
PAIRED = [["3A:D5:F5:A6:6D:5A", "Dubstep Pop 1200/1210"], [JBL, "JBL Go 3"], [H5, "Hydra 10 5.0"],
          [H3, "Hydra 10 3.0"], [BUDS, "OnePlus Buds 4"]]


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """fake(**state) -> read(): the fakes' state incl. "log" (argv of every call)."""
    config = tmp_path / "config.toml"
    config.write_text('[devices.aliases]\nbuds = "buds"\nearbuds = "buds"\n'
                      'headphones = "buds"\nearphones = "buds"\nspeaker = "jbl"\n')
    monkeypatch.setenv("HEARDBACK_CONFIG", str(config))
    bin_, path = tmp_path / "bin", tmp_path / "state.json"
    bin_.mkdir()
    for tool in ("wpctl", "brightnessctl", "bluetoothctl", "rfkill"):
        (bin_ / tool).write_text(f"#!{sys.executable}\n{FAKE}")
        (bin_ / tool).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SYS", str(path))
    entered = tmp_path / "entered"
    monkeypatch.setenv("FAKE_ENTERED", str(entered))
    monkeypatch.setattr(sysctl.time, "sleep", lambda s: None)

    def start(**kw):
        st = {"vol": 0.4, "muted": False, "bri": 50, "sink": ["", "Built-in Audio"], "log": [],
              "bt": {"on": True, "blocked": False, "paired": PAIRED, "conn": [], "fail": [], "hang": []}}
        st["bt"] |= kw.pop("bt", {})
        path.write_text(json.dumps(st | kw))
        return lambda: json.loads(path.read_text())
    return start


def _writes(st, tool):
    return [c[1:] for c in st["log"] if c[0] == tool and c[1] not in ("get-volume", "-m", "inspect", "devices", "show")]


# --- volume -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("argv,vol,say", [
    (["up"], 0.5, "volume 50"), (["down"], 0.3, "volume 30"), (["up", "5"], 0.45, "volume 45"),
    (["set", "70"], 0.7, "volume 70"), (["set", "150"], 1.0, "volume 100"), (["down", "90"], 0.0, "volume 0"),
    (["get"], 0.4, "volume 40"),
])
def test_volume(fake, run_jv, argv, vol, say):
    read = fake()
    code, out, _ = run_jv("volume", *argv, "--json")
    assert code == 0 and out["say"] == say and read()["vol"] == pytest.approx(vol)


def test_volume_up_caps_at_100(fake, run_jv):
    read = fake(vol=0.98)
    assert run_jv("volume", "up", "--json")[1]["say"] == "volume 100" and read()["vol"] == 1.0


def test_mute_unmute(fake, run_jv):
    read = fake()
    assert run_jv("volume", "mute", "--json")[1]["say"] == "muted" and read()["muted"]
    assert run_jv("volume", "get", "--json")[1]["say"] == "volume 40, muted"
    assert run_jv("volume", "unmute", "--json")[1]["say"] == "volume 40" and not read()["muted"]
    assert _writes(read(), "wpctl") == [["set-mute", "@DEFAULT_AUDIO_SINK@", "1"],
                                         ["set-mute", "@DEFAULT_AUDIO_SINK@", "0"]]


def test_volume_set_needs_level(fake, run_jv):
    read = fake()
    code, out, _ = run_jv("volume", "set", "--json")
    assert code == 2 and not _writes(read(), "wpctl")


# --- brightness -------------------------------------------------------------------------------------
@pytest.mark.parametrize("argv,bri,say", [
    (["up"], 60, "brightness 60"), (["down"], 40, "brightness 40"), (["set", "0"], 5, "brightness 5"),
    (["set", "120"], 100, "brightness 100"), (["get"], 50, "brightness 50"),
])
def test_brightness(fake, run_jv, argv, bri, say):
    read = fake()
    code, out, _ = run_jv("brightness", *argv, "--json")
    assert code == 0 and out["say"] == say and read()["bri"] == bri


def test_brightness_down_floors_at_5(fake, run_jv):
    read = fake(bri=10)
    run_jv("brightness", "down", "--json")
    assert read()["bri"] == 5 and _writes(read(), "brightnessctl") == [["-q", "set", "5%"]]


# --- bluetooth --------------------------------------------------------------------------------------
@pytest.mark.parametrize("said,mac", [("my buds", BUDS), ("buds", BUDS), ("speaker", JBL), ("jbl", JBL),
                                      ("OnePlus Buds", BUDS), ("the JBL Go 3", JBL)])
def test_connect_by_nickname(fake, run_jv, said, mac):
    read = fake()
    code, out, _ = run_jv("bt", "connect", said, "--json")
    assert code == 0 and read()["bt"]["conn"] == [mac] and out["say"].endswith("connected.")


def test_connect_ambiguous_says_choices(fake, run_jv):
    read = fake()
    code, out, _ = run_jv("bt", "connect", "hydra", "--json")
    assert code == 2 and out["say"] == "Which one: Hydra 10 5.0 or Hydra 10 3.0?" and not read()["bt"]["conn"]


def test_connect_unknown_and_failures(fake, run_jv):
    fake(bt={"fail": [JBL]})
    assert run_jv("bt", "connect", "wifi", "--json")[1]["say"] == "No paired Bluetooth device called wifi."
    assert run_jv("bt", "connect", "speaker", "--json")[1]["say"] == "I couldn't connect JBL Go 3. Is it on and nearby?"


def test_connect_timeout(fake, run_jv, monkeypatch):
    monkeypatch.setattr(sysctl, "CONNECT_TIMEOUT", 0.5)
    fake(bt={"hang": [JBL]})
    assert run_jv("bt", "connect", "jbl", "--json")[1]["say"] == "I couldn't reach JBL Go 3. Is it on?"


def test_connect_nonzero_but_device_connected(fake, run_jv):
    read = fake(bt={"connect_after_fail": [JBL]})
    code, out, _ = run_jv("bt", "connect", "jbl", "--json")
    assert code == 0 and out["say"] == "JBL Go 3 connected." and read()["bt"]["conn"] == [JBL]


def test_connect_when_off_or_already(fake, run_jv):
    read = fake(bt={"on": False})
    assert run_jv("bt", "connect", "buds", "--json")[1]["say"] == "Bluetooth is off. Say bluetooth on first."
    fake(bt={"conn": [BUDS]})
    assert run_jv("bt", "connect", "buds", "--json")[1]["say"] == "OnePlus Buds 4 is already connected."
    assert not _writes(read(), "bluetoothctl")


def test_disconnect_default_sink_says_where_audio_goes(fake, run_jv):
    read = fake(sink=[BUDS, "OnePlus Buds 4"], bt={"conn": [BUDS]})
    code, out, _ = run_jv("bt", "disconnect", "buds", "--json")   # no read-back: undo by voice
    assert code == 0 and out["say"] == "OnePlus Buds 4 disconnected. Audio is on Built-in Audio now."
    assert _writes(read(), "bluetoothctl") == [["disconnect", BUDS]]


def test_disconnect_other_device_is_quiet_about_audio(fake, run_jv):
    fake(bt={"conn": [JBL]})
    assert run_jv("bt", "disconnect", "jbl", "--json")[1]["say"] == "JBL Go 3 disconnected."


def test_disconnect_nonzero_but_device_disconnected(fake, run_jv):
    read = fake(bt={"conn": [JBL], "disconnect_after_fail": [JBL]})
    code, out, _ = run_jv("bt", "disconnect", "jbl", "--json")
    assert code == 0 and out["say"] == "JBL Go 3 disconnected." and not read()["bt"]["conn"]


def test_power_off_with_bt_sink(fake, run_jv):
    read = fake(sink=[BUDS, "OnePlus Buds 4"], bt={"conn": [BUDS]})
    code, out, _ = run_jv("bt", "power", "off", "--json")
    assert code == 0 and out["say"] == "Bluetooth off. Audio is on Built-in Audio now." and not read()["bt"]["on"]


def test_power_on_unblocks_rfkill(fake, run_jv):
    read = fake(bt={"on": False, "blocked": True})
    assert run_jv("bt", "power", "on", "--json")[1]["say"] == "Bluetooth on."
    assert read()["bt"]["on"] and ["rfkill", "unblock", "bluetooth"] in read()["log"]


def test_power_on_busy_but_readback_arrives(fake, run_jv, monkeypatch):
    monkeypatch.setattr(sysctl, "POWER_POLL_SECONDS", 0.5)
    monkeypatch.setattr(sysctl, "POWER_POLL_INTERVAL", 0.001)
    read = fake(bt={"on": False, "blocked": True, "power_script": [
        {"out": "org.bluez.Error.Busy", "rc": 1, "on": True, "shows": 3}]})
    code, out, _ = run_jv("bt", "power", "on", "--json")
    assert code == 0 and out["say"] == "Bluetooth on."
    assert len([c for c in read()["log"] if c[:2] == ["bluetoothctl", "power"]]) == 1


def test_power_on_busy_stays_off_retries_once(fake, run_jv, monkeypatch):
    monkeypatch.setattr(sysctl, "POWER_POLL_SECONDS", 0)
    read = fake(bt={"on": False, "blocked": True, "power_script": [
        {"out": "org.bluez.Error.Busy", "rc": 1}, {"out": "org.bluez.Error.Busy", "rc": 1}]})
    code, out, _ = run_jv("bt", "power", "on", "--json")
    assert code != 0 and "busy" in out["say"].lower()
    assert len([c for c in read()["log"] if c[:2] == ["bluetoothctl", "power"]]) == 2


def test_power_busy_retry_can_succeed(fake, run_jv, monkeypatch):
    monkeypatch.setattr(sysctl, "POWER_POLL_SECONDS", 0)
    read = fake(bt={"on": False, "blocked": True, "power_script": [
        {"out": "org.bluez.Error.Busy", "rc": 1}, {"out": "Changing power on succeeded", "on": True}]})
    code, out, _ = run_jv("bt", "power", "on", "--json")
    assert code == 0 and out["say"] == "Bluetooth on."
    assert len([c for c in read()["log"] if c[:2] == ["bluetoothctl", "power"]]) == 2


def test_power_rc_zero_without_state_change_fails(fake, run_jv, monkeypatch):
    monkeypatch.setattr(sysctl, "POWER_POLL_SECONDS", 0)
    fake(bt={"on": False, "power_script": [{"out": "succeeded"}]})
    code, out, _ = run_jv("bt", "power", "on", "--json")
    assert code != 0 and "couldn't confirm" in out["say"]


def test_power_hard_blocked_reports_blocked(fake, run_jv, monkeypatch):
    monkeypatch.setattr(sysctl, "POWER_POLL_SECONDS", 0)
    read = fake(bt={"on": False, "blocked": True, "hard_blocked": True})
    code, out, _ = run_jv("bt", "power", "on", "--json")
    assert code != 0 and "blocked" in out["say"].lower()
    assert read()["bt"]["hard_blocked"]


@pytest.mark.parametrize("state", ["on", "off"])
def test_power_already_in_state_does_not_write(fake, run_jv, state):
    read = fake(bt={"on": state == "on"})
    assert run_jv("bt", "power", state, "--json")[1]["say"] == f"Bluetooth is already {state}."
    assert not [c for c in read()["log"] if c[:2] == ["bluetoothctl", "power"]]


def test_power_off_nonzero_but_readback_off_preserves_audio(fake, run_jv, monkeypatch):
    monkeypatch.setattr(sysctl, "POWER_POLL_SECONDS", 0)
    read = fake(sink=[BUDS, "OnePlus Buds 4"], bt={"on": True, "conn": [BUDS],
                "power_script": [{"out": "Busy", "rc": 1, "on": False}]})
    code, out, _ = run_jv("bt", "power", "off", "--json")
    assert code == 0 and out["say"] == "Bluetooth off. Audio is on Built-in Audio now."
    assert not read()["bt"]["on"]


def test_power_timeout_and_show_failure_is_honest(fake, run_jv, monkeypatch):
    monkeypatch.setattr(sysctl, "POWER_POLL_SECONDS", 0)
    monkeypatch.setattr(sysctl, "POWER_COMMAND_TIMEOUT", 0.2)
    read = fake(bt={"on": False, "power_timeout": True, "show_fail_after": 1})
    code, out, _ = run_jv("bt", "power", "on", "--json")
    assert code != 0 and "couldn't confirm" in out["say"]
    assert open(os.environ["FAKE_ENTERED"]).read() == "power\n"


def _cp(stdout="", rc=0):
    import subprocess
    return subprocess.CompletedProcess([], rc, stdout, "")


def test_review_missing_initial_powered_is_unknown(monkeypatch):
    calls = []
    def bt(*a, **kw):
        calls.append(a)
        return _cp("succeeded") if a[0] == "power" else _cp("No default controller available")
    monkeypatch.setattr(sysctl, "_bt", bt)
    monkeypatch.setattr(sysctl, "_sink", lambda: ("", "Built-in Audio"))
    with pytest.raises(sysctl.JvError):
        sysctl._power({"state": "off"})
    assert not any(call[0] == "power" for call in calls)


def test_review_missing_final_powered_is_unknown(monkeypatch):
    calls = [0]
    def bt(*a, **kw):
        if a[0] == "power": return _cp("succeeded")
        calls[0] += 1
        return _cp("Powered: yes" if calls[0] == 1 else "No default controller available")
    monkeypatch.setattr(sysctl, "_bt", bt)
    monkeypatch.setattr(sysctl, "_sink", lambda: ("", "Built-in Audio"))
    monkeypatch.setattr(sysctl, "POWER_POLL_SECONDS", 0)
    with pytest.raises(sysctl.JvError):
        sysctl._power({"state": "off"})


def test_review_connect_success_requires_readback(monkeypatch):
    monkeypatch.setattr(sysctl, "device", lambda n: (JBL, "JBL Go 3"))
    monkeypatch.setattr(sysctl, "_powered", lambda: True)
    monkeypatch.setattr(sysctl, "_devices", lambda which, **kw: [])
    monkeypatch.setattr(sysctl, "_bt", lambda *a, **kw: _cp("Connection successful"))
    monkeypatch.setattr(sysctl, "BT_DEVICE_POLL_SECONDS", 0)
    with pytest.raises(sysctl.JvError):
        sysctl._connect({"name": "jbl"})


def test_review_disconnect_success_requires_readback(monkeypatch):
    monkeypatch.setattr(sysctl, "device", lambda n: (JBL, "JBL Go 3"))
    monkeypatch.setattr(sysctl, "_devices", lambda which, **kw: [(JBL, "JBL Go 3")])
    monkeypatch.setattr(sysctl, "_sink", lambda: ("", "Built-in Audio"))
    monkeypatch.setattr(sysctl, "_bt", lambda *a, **kw: _cp("Successful disconnected"))
    monkeypatch.setattr(sysctl, "BT_DEVICE_POLL_SECONDS", 0)
    with pytest.raises(sysctl.JvError):
        sysctl._disconnect({"name": "jbl"})


def test_review_failed_device_read_is_unknown(monkeypatch):
    monkeypatch.setattr(sysctl, "device", lambda n: (JBL, "JBL Go 3"))
    monkeypatch.setattr(sysctl, "_sink", lambda: ("", "Built-in Audio"))
    reads = [0]
    def devices(which, **kw):
        reads[0] += 1
        if reads[0] == 1: return [(JBL, "JBL Go 3")]
        raise sysctl.JvError(1, "devices read failed")
    monkeypatch.setattr(sysctl, "_devices", devices)
    monkeypatch.setattr(sysctl, "_bt", lambda *a, **kw: _cp("Failed", 1))
    with pytest.raises(sysctl.JvError):
        sysctl._disconnect({"name": "jbl"})


def test_review_devices_nonzero_is_not_empty_success(monkeypatch):
    monkeypatch.setattr(sysctl, "_bt", lambda *a, **kw: _cp("Failed to read devices", 1))
    with pytest.raises(sysctl.JvError):
        sysctl._devices("Connected")


def test_review_connect_timeout_reads_state_after_timeout(monkeypatch):
    monkeypatch.setattr(sysctl, "device", lambda n: (JBL, "JBL Go 3"))
    monkeypatch.setattr(sysctl, "_powered", lambda: True)
    connected = [False]
    monkeypatch.setattr(sysctl, "_devices", lambda which, **kw:
                        [(JBL, "JBL Go 3")] if connected[0] else [])
    def bt(*a, **kw):
        connected[0] = True
        raise sysctl.JvError(1, "bluetoothctl timed out")
    monkeypatch.setattr(sysctl, "_bt", bt)
    assert sysctl._connect({"name": "jbl"})["say"] == "JBL Go 3 connected."


def test_review_disconnect_timeout_reads_state_after_timeout(monkeypatch):
    monkeypatch.setattr(sysctl, "device", lambda n: (JBL, "JBL Go 3"))
    connected = [True]
    monkeypatch.setattr(sysctl, "_devices", lambda which, **kw:
                        [(JBL, "JBL Go 3")] if connected[0] else [])
    monkeypatch.setattr(sysctl, "_sink", lambda: ("", "Built-in Audio"))
    def bt(*a, **kw):
        connected[0] = False
        raise sysctl.JvError(1, "bluetoothctl timed out")
    monkeypatch.setattr(sysctl, "_bt", bt)
    assert sysctl._disconnect({"name": "jbl"})["say"] == "JBL Go 3 disconnected."


def test_review_power_timeout_reads_state_after_timeout(monkeypatch):
    on = [False]
    def bt(*a, **kw):
        if a[0] == "power":
            on[0] = True
            raise sysctl.JvError(1, "bluetoothctl timed out")
        return _cp("Powered: yes" if on[0] else "Powered: no")
    monkeypatch.setattr(sysctl, "_bt", bt)
    monkeypatch.setattr(sysctl, "POWER_POLL_SECONDS", 0)
    assert sysctl._power({"state": "on"})["say"] == "Bluetooth on."


def test_review_busy_with_unreadable_state_is_unconfirmed(monkeypatch):
    shows = [0]
    def bt(*a, **kw):
        if a[0] == "power": return _cp("org.bluez.Error.Busy", 1)
        shows[0] += 1
        return _cp("Powered: no" if shows[0] == 1 else "show failed", 0 if shows[0] == 1 else 1)
    monkeypatch.setattr(sysctl, "_bt", bt)
    monkeypatch.setattr(sysctl, "POWER_POLL_SECONDS", 0)
    with pytest.raises(sysctl.JvError) as error:
        sysctl._power({"state": "on"})
    assert "couldn't confirm" in error.value.extra["say"]


def test_review_confirmed_power_off_survives_audio_failure(monkeypatch):
    shows = [0]
    def bt(*a, **kw):
        if a[0] == "power": return _cp("succeeded")
        shows[0] += 1
        return _cp("Powered: yes" if shows[0] == 1 else "Powered: no")
    monkeypatch.setattr(sysctl, "_bt", bt)
    monkeypatch.setattr(sysctl, "_sink", lambda: (BUDS, "OnePlus Buds 4"))
    monkeypatch.setattr(sysctl, "_audio_moved", lambda macs: (_ for _ in ()).throw(
                        sysctl.JvError(1, "wpctl timed out")))
    monkeypatch.setattr(sysctl, "POWER_POLL_SECONDS", 0)
    assert sysctl._power({"state": "off"})["powered"] is False


def test_review_failed_power_off_read_cannot_confirm_target(monkeypatch):
    calls = [0]
    def bt(*a, **kw):
        if a[0] == "power": return _cp("Busy", 1)
        calls[0] += 1
        if calls[0] == 1: return _cp("Powered: yes")
        return _cp("Powered: no", 1)
    monkeypatch.setattr(sysctl, "_bt", bt)
    monkeypatch.setattr(sysctl, "_sink", lambda: ("", "Built-in Audio"))
    monkeypatch.setattr(sysctl, "POWER_POLL_SECONDS", 0)
    with pytest.raises(sysctl.JvError):
        sysctl._power({"state": "off"})


@pytest.fixture
def bt_clock(monkeypatch):
    """Subprocess fake that consumes its timeout, including 1 ms of cleanup."""
    import subprocess
    from types import SimpleNamespace

    clock = SimpleNamespace(now=0.0, calls=[], handler=None)

    def run(argv, *, timeout, **kwargs):
        clock.calls.append((tuple(argv), clock.now, timeout))
        delay, stdout, rc = clock.handler(tuple(argv))
        clock.now += min(delay, timeout) + 0.001
        if delay >= timeout:
            raise subprocess.TimeoutExpired(argv, timeout)
        return _cp(stdout, rc)

    monkeypatch.setattr(sysctl.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(sysctl.time, "sleep", lambda s: setattr(clock, "now", clock.now + s))
    monkeypatch.setattr(sysctl.subprocess, "run", run)
    return clock


@pytest.mark.parametrize("operation", ["connect", "disconnect", "power-on", "power-off"])
def test_timed_timeout_uses_reserved_readback(bt_clock, operation):
    clock, changed = bt_clock, [False]

    def handle(argv):
        tool, verb, *args = argv
        if tool == "rfkill":
            return 0.7, "", 0
        if tool == "wpctl":
            return (0.7 if operation == "power-off" else 0.01), 'node.description = "Built-in Audio"', 0
        if verb == "devices" and args == ["Paired"]:
            return (0.6 if operation == "disconnect" else 0.01), f"Device {JBL} JBL Go 3", 0
        if verb == "show":
            on = (not changed[0]) if operation == "power-off" else (
                changed[0] if operation == "power-on" else True)
            out = "Powered: " + ("yes" if on else "no")
            if operation == "power-on" and not changed[0]:
                out += "\nPowerState: off-blocked"
            return (2.9 if operation.startswith("power") and not changed[0] else 0.01), out, 0
        if verb == "devices" and args == ["Connected"]:
            connected = changed[0] if operation == "connect" else not changed[0]
            return (0.6 if operation == "disconnect" and not changed[0] else 0.01), (
                f"Device {JBL} JBL Go 3" if connected else ""), 0
        if verb in ("connect", "disconnect", "power"):
            changed[0] = True
            return 20.0, "", 0
        raise AssertionError(argv)

    clock.handler = handle
    if operation.startswith("power-"):
        assert sysctl._power({"state": operation[6:]})["say"] == f"Bluetooth {operation[6:]}."
    else:
        fn = sysctl._connect if operation == "connect" else sysctl._disconnect
        assert fn({"name": "jbl"})["say"] == f"JBL Go 3 {operation}ed."
    assert clock.now < 8.1


def test_timed_success_near_command_timeout_still_reads(bt_clock):
    clock, connected = bt_clock, [False]

    def handle(argv):
        if argv[1:3] == ("devices", "Paired"):
            return 0.01, f"Device {JBL} JBL Go 3", 0
        if argv[1] == "show":
            return 0.01, "Powered: yes", 0
        if argv[1] == "devices":
            return 0.01, f"Device {JBL} JBL Go 3" if connected[0] else "", 0
        assert argv[1] == "connect"
        connected[0] = True
        return clock.calls[-1][2] - 0.0001, "Connection successful", 0

    clock.handler = handle
    assert sysctl._connect({"name": "jbl"})["say"] == "JBL Go 3 connected."
    assert clock.now < 8.1


def test_timed_confirmed_disconnect_survives_audio_timeout(bt_clock):
    clock, disconnected = bt_clock, [False]

    def handle(argv):
        if argv[0] == "wpctl":
            if not disconnected[0]:
                return 0.9, f'api.bluez5.address = "{JBL}"\nnode.description = "JBL Go 3"', 0
            return 2.0, 'node.description = "Built-in Audio"', 0
        if argv[1:3] == ("devices", "Paired"):
            return 0.01, f"Device {JBL} JBL Go 3", 0
        if argv[1] == "devices":
            return (0.9 if disconnected[0] else 0.01), (
                "" if disconnected[0] else f"Device {JBL} JBL Go 3"), 0
        assert argv[1] == "disconnect"
        disconnected[0] = True
        return 4.9, "Successful disconnected", 0

    clock.handler = handle
    assert sysctl._disconnect({"name": "jbl"})["say"] == "JBL Go 3 disconnected."
    assert clock.now < 8.1


@pytest.mark.parametrize("operation", ["connect", "disconnect", "power-on", "power-off", "status"])
def test_timed_operations_keep_total_budget(bt_clock, operation):
    clock, changed = bt_clock, [False]

    def handle(argv):
        if argv[0] == "wpctl":
            return 20.0, "", 0
        if argv[1:3] == ("devices", "Paired"):
            return 2.0, f"Device {JBL} JBL Go 3", 0
        if argv[1] == "show":
            on = operation != "power-on" or changed[0]
            return 2.0, "Powered: " + ("yes" if on else "no"), 0
        if argv[1] == "devices":
            return 2.0, f"Device {JBL} JBL Go 3" if operation == "disconnect" else "", 0
        assert argv[1] in ("connect", "disconnect", "power")
        changed[0] = True
        return 20.0, "", 0

    clock.handler = handle
    try:
        if operation.startswith("power-"):
            sysctl._power({"state": operation[6:]})
        elif operation == "status":
            sysctl._status({})
        else:
            (sysctl._connect if operation == "connect" else sysctl._disconnect)({"name": "jbl"})
    except sysctl.JvError:
        pass
    assert clock.now < 8.1
    assert sysctl._BT_DEADLINE.get() is None
    assert all(0 < timeout <= 8 - started + 0.00001 for _, started, timeout in clock.calls)


def test_timed_slow_power_keeps_caller_margin(bt_clock):
    clock, on = bt_clock, [False]

    def handle(argv):
        if argv[1] == "power":
            on[0] = True
            return 4.9, "succeeded", 0
        return 2.9, "Powered: yes" if on[0] else "Powered: no", 0

    clock.handler = handle
    with pytest.raises(sysctl.JvError) as error:
        sysctl._power({"state": "on"})
    assert "couldn't confirm" in error.value.extra["say"]
    assert clock.now < 8.1
    assert any(argv[1] == "power" and timeout <= 2.5 for argv, _, timeout in clock.calls)


def test_status(fake, run_jv):
    fake(bt={"conn": [BUDS, JBL]})
    assert run_jv("bt", "status", "buds", "--json")[1]["say"] == "OnePlus Buds 4 is connected."
    assert run_jv("bt", "status", "--json")[1]["say"] == "JBL Go 3 and OnePlus Buds 4 are connected."
    fake()
    assert run_jv("bt", "status", "speaker", "--json")[1]["say"] == "JBL Go 3 is not connected."
    assert run_jv("bt", "status", "--json")[1]["say"] == "Nothing is connected."
    fake(bt={"on": False})
    assert run_jv("bt", "status", "--json")[1]["say"] == "Bluetooth is off."


def test_never_pairs_trusts_removes_or_scans():
    src = open(sysctl.__file__).read()
    assert not any(f'"{w}"' in src for w in ("pair", "trust", "remove", "scan"))
