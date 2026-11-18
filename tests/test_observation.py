"""E4a receipts: real helpers, recorded desktop reads, fakes at subprocess/socket only."""
import argparse
import json
import subprocess
from pathlib import Path

import pytest

from jv import cli, hypr, media, sysctl, zen
from jv.verb import UNCONFIRMED
from test_zen import fake_zen  # noqa: F401

FLAGGED = {"volume", "brightness", "hypr.focus", "zen.switch", "media", "tmux.select", "tmux.new", "tmux.split", "tmux.focus",
           "tmux.close", "tmux.close_pane", "tmux.rename", "tmux.move", "zen.video", "zen.close", "zen.close-unused"}
FLAGGED |= {"hypr.workspace", "hypr.close", "hypr.move", "settings.dnd", "settings.power", "settings.output",
            "settings.mic", "settings.window", "settings.theme"}
FLAGGED |= {"bt.power", "bt.connect", "bt.disconnect", "wall.next", "wall.set"}
FLAGGED |= {"reminder.add", "reminder.cancel", "fact.remember", "fact.forget", "inbox.add", "inbox.remove"}
RECORDED = json.loads((Path(__file__).parent / "fixtures/e4a-desktop.json").read_text())


@pytest.fixture
def desktop(monkeypatch):
    calls, overrides = [], {}
    reads = {"wpctl": "volume", "brightnessctl": "brightness", "playerctl": "players"}

    def run(argv, **kwargs):
        calls.append(argv)
        key = None
        if argv[0] == "hyprctl":
            key = argv[-1] if "-j" in argv else None
        elif argv[0] in reads:
            if argv[1] == "get-volume" or argv[1:] == ["-m"] or argv[1:3] == ["-a", "metadata"]:
                key = reads[argv[0]]
        else:
            pytest.fail(f"unexpected subprocess: {argv}")
        out = overrides.get(key, RECORDED[key]["stdout"] if key else "ok\n")
        if isinstance(out, Exception):
            raise out
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(hypr, "_inst", None)
    monkeypatch.delenv("JV_DRY")   # dispatch reaches the fake subprocess, never the desktop
    monkeypatch.setattr(media, "VERIFY_S", 0)
    return calls, overrides


def test_only_the_five_verbs_opt_in():
    assert {v.name for v in cli.all_verbs() if v.observes} == FLAGGED
    verbs = [v for v in cli.all_verbs() if v.name in FLAGGED | {"local.time"}]
    assert {e["name"] for e in cli.catalogue(verbs)["verbs"] if e["observes"]} == FLAGGED


@pytest.mark.parametrize("observed", [None, False, True, 1, "true", {}])
@pytest.mark.parametrize("flagged", [False, True])
@pytest.mark.parametrize("as_json", [False, True])
def test_emit_contract(capsys, observed, flagged, as_json):
    verb = next(v for v in sysctl.VERBS if v.name == "volume") if flagged else hypr.VERBS[0]
    data = {"ok": True, "say": "Done."}
    if observed is not None:
        data["observed"] = observed
    cli._emit(argparse.Namespace(json=as_json, _verb=verb), data, data["say"])
    output = capsys.readouterr().out.strip()
    expected = UNCONFIRMED if flagged and observed is not True else "Done."
    assert (json.loads(output)["say"] if as_json else output) == expected


@pytest.mark.parametrize("argv,confirmed", [
    (["volume", "set", "30"], False), (["volume", "set", "54"], True),
    (["volume", "mute"], False), (["volume", "unmute"], True),
    (["brightness", "set", "30"], False), (["brightness", "set", "40"], True),
    (["brightness", "set", "41"], False),
    (["hypr", "focus", "kitty"], False), (["hypr", "focus", "zen"], True),
    (["media", "pause", "--target", "mpd"], True),
])
def test_recorded_readback_confirms_only_matching_state(desktop, run_jv, argv, confirmed):
    rc, data, _ = run_jv(*argv, "--json")
    assert rc == 0 and data["ok"] is True and data["observed"] is confirmed
    assert (data["say"] == UNCONFIRMED) is (not confirmed)
    if argv[0] in {"volume", "brightness"}:
        calls = desktop[0]
        assert len(calls) == 3 and calls[0] == calls[-1]  # read, act, fresh read


@pytest.mark.parametrize("verb", ["volume", "brightness"])
def test_failed_post_read_does_not_claim_the_requested_level(desktop, run_jv, monkeypatch, verb):
    run = subprocess.run

    def fail_after_write(argv, **kwargs):
        if len(desktop[0]) == 2:
            raise subprocess.TimeoutExpired(argv, 3)
        return run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_after_write)
    rc, data, _ = run_jv(verb, "set", "30", "--json")
    assert rc == 0 and data == {"ok": True, "observed": False, "say": UNCONFIRMED}


def test_real_unchanged_track_does_not_confirm_skip(desktop, run_jv):
    rc, data, _ = run_jv("media", "next", "--target", "spotify", "--json")
    assert rc == 1 and not data["ok"] and data["track_changed"] is False
    assert "couldn't confirm" in data["say"]
    assert len([a for a in desktop[0] if a[-1] == "next"]) == 1


@pytest.mark.parametrize("focused", [False, True])
def test_zen_switch_uses_browser_and_recorded_compositor_evidence(desktop, fake_zen, run_jv, focused):
    desktop[1]["activewindow"] = json.dumps(next(
        c for c in json.loads(RECORDED["clients"]["stdout"]) if c["class"] == ("zen" if focused else "kitty")))
    ops = fake_zen()
    rc, data, _ = run_jv("zen", "switch", "Old Docs", "--json")
    assert rc == 0 and data["observed"] is focused
    assert (data["say"] == UNCONFIRMED) is (not focused)
    assert len([o for o in ops if o["op"] == "activate"]) == 1


def test_dry_run_and_read_gets_are_not_failed_actions(desktop, run_jv):
    rc, data, _ = run_jv("volume", "set", "30", "--dry-run", "--json")
    assert rc == 0 and data["say"] == "would run volume" and desktop[0] == []
    for verb in ("volume", "brightness"):
        rc, data, _ = run_jv(verb, "get", "--json")
        assert rc == 0 and data["observed"] is True and data["say"] != UNCONFIRMED
