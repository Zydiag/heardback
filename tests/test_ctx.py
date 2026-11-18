"""jv ctx: clipboard / selection via a fake wl-paste (never the real clipboard), window via fake hyprctl, tab via fake zen."""
import json
import subprocess

import pytest

from jv import ctx, hypr, tmux
from test_hypr import _fake
from test_zen import fake_zen, fake_hypr  # noqa: F401


@pytest.fixture
def clip(monkeypatch):
    """Fake wl-paste: {"clipboard": text, "selection": text}; None = nothing copied (rc 1)."""
    data, calls = {"clipboard": None, "selection": None}, []

    def run(argv, **kw):
        assert argv[0] == "wl-paste", argv
        calls.append(argv)
        t = data["selection" if "--primary" in argv else "clipboard"]
        return subprocess.CompletedProcess(argv, 1 if t is None else 0, (t or "").encode(), b"Nothing is copied")
    monkeypatch.setattr(ctx.subprocess, "run", run)
    monkeypatch.setattr(hypr, "hyprctl", lambda *a, json_out=False: {"address": "0xzen", "class": "zen"})
    monkeypatch.setattr(tmux, "_tmux_foreground", lambda: False)
    data["calls"] = calls
    return data


def test_clipboard(clip, run_jv):
    clip["clipboard"] = "uv run pytest -q"
    code, out, _ = run_jv("ctx", "--clipboard", "--json")
    assert code == 0 and out["text"] == "uv run pytest -q" and out["source"] == "clipboard"
    assert clip["calls"] == [["wl-paste", "--no-newline", "--type", "text"]]


def test_selection_is_default(clip, run_jv):
    clip["selection"] = "selected words"
    code, out, _ = run_jv("ctx", "--json")
    assert code == 0 and out["text"] == "selected words" and out["source"] == "selection"
    assert "--primary" in clip["calls"][0]
    assert out["source_revision"].startswith("selection:") and "selected words" not in out["source_revision"]


def test_terminal_selection_refused_without_reading_primary(clip, monkeypatch, run_jv):
    monkeypatch.setattr(hypr, "hyprctl", lambda *a, json_out=False: {"address": "0xterm", "class": "kitty"})
    monkeypatch.setattr(tmux, "_tmux_foreground", lambda: True)
    monkeypatch.setattr(tmux, "resolve", lambda target: {"cmd": "nvim"})
    code, out, _ = run_jv("ctx", "--selection", "--json")
    assert code == 1 and '"+y" or "+Y"' in out["error"]
    assert clip["calls"] == []


@pytest.mark.parametrize("flag,data,say", [("--clipboard", None, "the clipboard is empty"),
                                            ("--clipboard", "  \n", "the clipboard is empty"),
                                            ("--selection", None, "nothing is selected")])
def test_empty_says_so(clip, run_jv, flag, data, say):
    clip[flag[2:]] = data
    code, out, _ = run_jv("ctx", flag, "--json")
    assert code == 1 and out == {"ok": False, "error": say, "say": say}


def test_cap(clip, run_jv):
    clip["clipboard"] = "x" * (ctx.CAP + 50)
    out = run_jv("ctx", "--clipboard", "--json")[1]
    assert len(out["text"]) == ctx.CAP and out["truncated"] is True


def test_no_wl_paste(monkeypatch, run_jv):
    def run(argv, **kw):
        raise FileNotFoundError(argv[0])
    monkeypatch.setattr(ctx.subprocess, "run", run)
    code, _, err = run_jv("ctx", "--clipboard")
    assert code == 1 and "wl-paste not installed" in err


def test_one_source_only(clip, run_jv):
    assert run_jv("ctx", "--clipboard", "--tab")[0] == 2


def test_window(monkeypatch, run_jv):
    _fake(monkeypatch)
    real = hypr._run
    monkeypatch.setattr(hypr, "_run", lambda argv: json.dumps({"address": "0xaaa", "class": "kitty", "title": "tmux"})
                        if "activewindow" in argv else real(argv))
    out = run_jv("ctx", "--window", "--json")[1]
    assert out["text"] == "kitty: tmux" and out["source"] == "window"


def test_tab(fake_zen, run_jv):
    fake_zen()
    out = run_jv("ctx", "--tab", "--json")[1]
    assert out["title"] == "Inbox" and out["url"] == "https://mail.example.com/" and out["source"] == "tab"
