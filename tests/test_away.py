import json
import subprocess
import time

import pytest

from heardback.status import away, store

T0 = 1_800_000_000.0


@pytest.fixture
def pgrep(monkeypatch):
    calls, rc = [], [1]

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, rc[0])
    monkeypatch.setattr(away.subprocess, "run", run)
    return calls, rc


def test_flag_is_away(pgrep, runtime_dir):
    assert str(away.flag_path()) == f"{runtime_dir}/heardback-away"
    away.flag_path().touch()
    assert away.is_away()


def test_not_away(pgrep, runtime_dir):
    calls, _ = pgrep
    assert not away.is_away() and calls == [["pgrep", "-x", "hyprlock"]]


@pytest.mark.parametrize("exc", [FileNotFoundError("pgrep"), subprocess.TimeoutExpired("pgrep", 1)])
def test_pgrep_fails_not_away(monkeypatch, runtime_dir, exc):
    seen = {}

    def run(argv, **kw):
        seen.update(kw)
        raise exc
    monkeypatch.setattr(away.subprocess, "run", run)
    assert not away.is_away() and seen["timeout"] == 1


def test_locked_is_away(pgrep, runtime_dir):
    pgrep[1][0] = 0
    assert away.is_away()


def test_mute_minutes(state_dir):
    until = away.mute(5, T0)
    assert json.loads((store.state_dir() / "mute.json").read_text()) == {"muted_until": until}
    assert store.epoch(until) == T0 + 300
    assert away.is_muted(T0 + 4 * 60) and not away.is_muted(T0 + 6 * 60)
    assert (store.state_dir() / "mute.json").stat().st_mode & 0o777 == 0o600


def test_mute_forever_and_unmute(state_dir):
    assert not away.is_muted(T0)
    assert away.mute(None, T0) == "forever" and away.is_muted(T0 + 10 ** 7)
    away.unmute()
    assert not away.is_muted(T0)
    away.unmute()  # twice is fine


def test_bad_mute_file(state_dir):
    for bad in ("{", "[]", '{"muted_until": 5}', '{"muted_until": "soon"}'):
        (store.state_dir() / "mute.json").write_text(bad)
        assert not away.is_muted(T0)


def test_mute_verb(state_dir, run_jv):
    code, out, _ = run_jv("status", "mute", "5", "--json")
    assert code == 0 and out["say"] == "Muted for 5 minutes." and away.is_muted(time.time() + 60)
    assert run_jv("status", "mute", "1", "--json")[1]["say"] == "Muted for 1 minute."
    code, out, _ = run_jv("status", "mute", "--json")
    assert code == 0 and out["muted_until"] == "forever" and out["say"] == "Muted."
    code, out, _ = run_jv("status", "mute", "off", "--json")
    assert code == 0 and out["say"] == "Unmuted." and out["muted_until"] is None and not away.is_muted(T0)
    code, out, _ = run_jv("status", "unmute", "--json")
    assert code == 0 and out["say"] == "Unmuted."


@pytest.mark.parametrize("bad", ["soon", "-5", "0", "1.5", "999999"])
def test_mute_bad_until(state_dir, run_jv, bad):
    assert run_jv("status", "mute", bad)[0] == 2
    assert not (store.state_dir() / "mute.json").exists()
