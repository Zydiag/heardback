import json
import socket

import pytest

from jv import cli, desktop


@pytest.mark.parametrize("argv,reason", [
    (["hypr", "list"], "hyprctl"),
    (["wall", "random"], "ml4w-wallpaper"),
    (["zen", "list"], "Zen browser"),
    (["settings", "night", "on"], "hyprctl"),
])
def test_absent_module_is_unavailable_and_hidden(monkeypatch, capsys, argv, reason):
    monkeypatch.setattr(desktop.shutil, "which", lambda name: None)
    assert cli.run([*argv, "--json"]) == 1
    out, err = capsys.readouterr()
    data = json.loads(out)
    assert data["status"] == "not available on this desktop" and reason in data["reason"]
    assert "Traceback" not in err and reason in err
    assert cli.run(["list", "--json"]) == 0
    cat = json.loads(capsys.readouterr().out)
    names = {v["name"] for v in cat["verbs"]}
    assert not any(n.startswith(("hypr.", "wall.")) for n in names)
    assert not any(n.startswith("zen.") and n != "zen.install" for n in names)
    assert {"tmux.list", "calculate", "zen.install"} <= names


@pytest.mark.parametrize("binary", ["zen-browser", "zen"])
def test_closed_browser_keeps_verbs_and_connection_error(monkeypatch, capsys, binary):
    monkeypatch.setattr(desktop.shutil, "which", lambda name: f"/bin/{name}" if name == binary else None)
    def disconnected(*args):
        raise FileNotFoundError
    monkeypatch.setattr(socket.socket, "connect", disconnected)
    assert desktop.unavailable("zen.list") is None
    assert cli.run(["list", "--json"]) == 0
    assert "zen.open" in {v["name"] for v in json.loads(capsys.readouterr().out)["verbs"]}
    assert cli.run(["zen", "list", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["say"] == "Zen isn't connected."


def test_missing_kitty_keeps_tmux(tmux_server, monkeypatch, capsys):
    monkeypatch.setattr(desktop.shutil, "which", lambda name: None)
    assert cli.run(["tmux", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
