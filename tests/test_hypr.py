import json
import os
import shutil

import pytest

from jv import hypr, tmux

REAL_RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR")  # read at import, before conftest pins it
from jv.verb import JvError

INST = json.dumps([{"instance": "old", "time": 100, "pid": 1}, {"instance": "new", "time": 200, "pid": 2}])
CLIENTS = [
    {"address": "0xaaa", "class": "kitty", "title": "tmux", "pid": 50, "workspace": {"id": 1}, "focusHistoryID": 1},
    {"address": "0xbbb", "class": "zen", "title": "WhatsApp — Zen Browser", "pid": 60, "workspace": {"id": 2},
     "focusHistoryID": 0},
    {"address": "0xccc", "class": "org.foo.Bar", "title": "bar", "pid": 70, "workspace": {"id": 4},
     "focusHistoryID": 2},
]


def _fake(monkeypatch, inst=INST, clients=CLIENTS, active="0xaaa"):
    calls = []

    def run(argv):
        calls.append(argv)
        if "instances" in argv:
            return inst
        if "clients" in argv:
            return json.dumps(clients)
        if "workspaces" in argv:
            return json.dumps([{"id": 1, "windows": 1}, {"id": 2, "windows": 1}])
        if "activewindow" in argv:
            return json.dumps({"address": active})
        raise AssertionError(f"unexpected hyprctl call {argv}")
    monkeypatch.setattr(hypr, "_run", run)
    monkeypatch.setattr(hypr, "_inst", None)
    return calls


@pytest.fixture
def fake_hypr(monkeypatch):
    return _fake(monkeypatch)


@pytest.fixture
def fake_hypr_empty(monkeypatch):
    return _fake(monkeypatch, inst="[]")


@pytest.fixture
def apps(tmp_path, monkeypatch):
    d = tmp_path / "applications"; d.mkdir()
    (d / "org.foo.Bar.desktop").write_text("[Desktop Entry]\nName=Foo Bar\nExec=foo\n")
    (d / "zen.desktop").write_text("[Desktop Entry]\nName=Zen Browser\nExec=zen\n")
    (d / "hidden.desktop").write_text("[Desktop Entry]\nName=Hidden Thing\nNoDisplay=true\n")
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path)); monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "none"))


def test_instance_picks_newest(fake_hypr):
    assert hypr.instance() == "new"
    assert hypr.hyprctl("clients", json_out=True)[0]["address"] == "0xaaa"
    assert ["hyprctl", "-i", "new", "-j", "clients"] in fake_hypr


def test_no_instance(fake_hypr_empty):
    with pytest.raises(JvError) as e:
        hypr.instance()
    assert e.value.msg == "hyprland not running"


def test_workspace_dispatch_lua(fake_hypr, run_jv):
    code, _, err = run_jv("hypr", "workspace", "3")
    assert code == 0 and "hl.dsp.focus({workspace = 3})" in err


def test_workspace_range(run_jv):
    assert run_jv("hypr", "workspace", "11")[0] == 2


def test_list(fake_hypr, run_jv):
    code, out, _ = run_jv("hypr", "list", "--json")
    assert code == 0 and out["say"] == "3 windows" and out["clients"][1] == {
        "ws": 2, "class": "zen", "title": "WhatsApp — Zen Browser", "address": "0xbbb"}


def test_desktop_apps_skips_hidden(apps):
    assert hypr.desktop_apps() == {"Foo Bar": "org.foo.Bar", "Zen Browser": "zen"}


def test_launch_only_desktop_ids(fake_hypr, apps, run_jv):
    code, _, err = run_jv("hypr", "launch", "foo")
    assert code == 0 and 'hl.dsp.exec_cmd("gtk-launch org.foo.Bar")' in err
    assert run_jv("hypr", "launch", "rm -rf")[0] == 1


def test_focus_by_query(fake_hypr, run_jv):
    code, out, err = run_jv("hypr", "focus", "whatsapp", "--json")
    assert code == 0 and out["address"] == "0xbbb" and 'address:0xbbb' in err


def test_move(fake_hypr, run_jv):
    code, out, err = run_jv("hypr", "move", "active", "4", "--json")
    assert code == 0 and out["say"] == "I couldn't confirm that." and out["observed"] is False
    assert 'hl.dsp.window.move({workspace = 4, window = "address:0xaaa", follow = false})' in err


def test_open_running_focuses(fake_hypr, apps, run_jv):
    code, out, err = run_jv("hypr", "open", "foo", "--json")
    assert code == 0 and out["say"] == "focused Foo Bar" and "address:0xccc" in err and "gtk-launch" not in err


def test_open_not_running_launches(monkeypatch, apps, run_jv):
    _fake(monkeypatch, clients=[])
    code, out, err = run_jv("hypr", "open", "foo", "--json")
    assert code == 0 and out["say"] == "launching Foo Bar" and "gtk-launch org.foo.Bar" in err
    assert "workspace" not in err


def test_open_zen_goes_home_first(monkeypatch, apps, run_jv):
    _fake(monkeypatch, clients=[])
    code, _, err = run_jv("hypr", "open", "zen")
    assert code == 0
    assert err.index("hl.dsp.focus({workspace = 2})") < err.index("gtk-launch zen")


def test_bad_address_never_reaches_lua():
    with pytest.raises(JvError):
        hypr.focus_window('0x1"}) os.execute("x')
    with pytest.raises(JvError):
        hypr.move_window("0xZZ", 3)


def test_focus_fallback_cycles(monkeypatch, capsys):
    actives = iter(["0xaaa", "0xaaa", "0xccc"])
    _fake(monkeypatch)
    real = hypr._run
    monkeypatch.setattr(hypr, "_run", lambda argv: json.dumps({"address": next(actives)})
                        if "activewindow" in argv else real(argv))
    monkeypatch.setattr(hypr, "FOCUS_WINDOW", None)
    hypr.focus_window("0xccc")
    err = capsys.readouterr().err
    assert err.splitlines()[0] == "hl.dsp.focus({workspace = 4})"
    assert err.count("hl.dsp.window.cycle_next()") == 2


def test_move_fallback(monkeypatch, capsys):
    _fake(monkeypatch, active="0xbbb")
    monkeypatch.setattr(hypr, "MOVE_WINDOW", None)
    hypr.move_window("0xccc", 5)
    assert capsys.readouterr().err.splitlines() == [
        'hl.dsp.focus({window = "address:0xccc"})', "hl.dsp.window.move({workspace = 5})",
        'hl.dsp.focus({window = "address:0xbbb"})']


def test_select_focuses_kitty(tmux_server, monkeypatch, run_jv):
    _fake(monkeypatch)
    real = tmux.tmux
    monkeypatch.setattr(tmux, "tmux", lambda *a, **k: "123\n" if a[0] == "list-clients" else real(*a, **k))
    monkeypatch.setattr(tmux, "_ppid", {123: 50, 50: 1}.get)
    code, out, err = run_jv("tmux", "select", "0", "--json")
    assert code == 0 and "focused" not in out and 'address:0xaaa' in err


def test_select_hypr_failure_still_ok(tmux_server, monkeypatch, run_jv):
    _fake(monkeypatch, inst="[]")
    real = tmux.tmux
    monkeypatch.setattr(tmux, "tmux", lambda *a, **k: "123\n" if a[0] == "list-clients" else real(*a, **k))
    monkeypatch.setattr(tmux, "_ppid", {123: 50, 50: 1}.get)
    code, out, _ = run_jv("tmux", "select", "0", "--json")
    assert code == 0 and out["focused"] is False


@pytest.mark.skipif(not shutil.which("hyprctl") or not os.environ.get("WAYLAND_DISPLAY"), reason="no hyprland")
def test_live_list_readonly(run_jv, monkeypatch):
    # hyprctl finds the Hyprland socket under the real runtime dir; `hyprctl -j clients` is read-only.
    if REAL_RUNTIME_DIR:
        monkeypatch.setenv("XDG_RUNTIME_DIR", REAL_RUNTIME_DIR)
    assert run_jv("hypr", "list", "--json")[0] == 0


def test_focus_fallback_gives_up(monkeypatch, capsys):
    _fake(monkeypatch)   # active stays 0xaaa
    monkeypatch.setattr(hypr, "FOCUS_WINDOW", None)
    with pytest.raises(JvError, match="couldn't focus window"):
        hypr.focus_window("0xccc")
    assert capsys.readouterr().err.count("cycle_next") == hypr.CYCLE_MAX


def test_desktop_id_no_leading_dash(monkeypatch):
    monkeypatch.setattr(hypr, "desktop_apps", lambda: {"Evil": "-evil"})
    with pytest.raises(JvError, match="bad desktop id"):
        hypr._app("Evil")


@pytest.mark.parametrize("call, msg", [(lambda: tmux.tmux("send-keys", "secret text"), "tmux timed out"),
                                       (lambda: hypr._run(["hyprctl", "secret"]), "hyprctl timed out")])
def test_subprocess_timeout_hides_argv(monkeypatch, call, msg):
    import subprocess

    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(JvError) as e:
        call()
    assert e.value.code == 1 and e.value.msg == msg and "secret" not in e.value.msg


REAL_APPS = ["Htop", "btop++", "Zen Browser", "Firefox", "kitty", "Alacritty", "Stremio", "Telegram",
             "Mornye (SpotiFLAC)", "OBS Studio", "Obsidian", "Mail Reader", "Micro"]   # from this box's desktop_apps()


@pytest.mark.parametrize("q,want", [("zen", "Zen Browser"), ("firefox", "Firefox"), ("kitty", "kitty"),
                                    ("htop", "Htop"), ("obs", "OBS Studio"), ("obs studio", "OBS Studio"), ("firfox", "Firefox")])
def test_app_real_names_resolve(monkeypatch, q, want):
    monkeypatch.setattr(hypr, "desktop_apps", lambda: {n: "x" for n in REAL_APPS})
    assert hypr._app(q)[0] == want


@pytest.mark.parametrize("q", ["whatsapp", "spotify", "steam", "gmail"])
def test_app_weak_guess_never_launches(monkeypatch, q):
    monkeypatch.setattr(hypr, "desktop_apps", lambda: {n: "x" for n in REAL_APPS})
    with pytest.raises(JvError) as e:
        hypr._app(q)
    assert e.value.code == 1 and e.value.extra["closest"]
    assert e.value.msg == f"no installed app named {q!r}; for a website use zen_open"


def test_open_whatsapp_cli_no_launch(fake_hypr, apps, run_jv):
    code, out, err = run_jv("hypr", "open", "whatsapp")
    assert code == 1 and "for a website use zen_open" in out + err and "exec_cmd" not in err



def test_close_refuses_a_tab_query_before_matching_any_window(monkeypatch):
    monkeypatch.setattr(hypr, "clients", lambda: pytest.fail("must not look for a window"))
    with pytest.raises(JvError) as e:
        hypr._close_check({"query": "zen tabs i havent used"})
    assert e.value.code == 2
