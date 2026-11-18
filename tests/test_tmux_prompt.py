"""showing_prompt: bottom of the visible screen only, real dialog shapes (trace task 8a; live 15:34 rc 4)."""
from pathlib import Path

import pytest

from jv import tmux

FIX = Path(__file__).parent / "fixtures" / "tmux"
PANE = {"id": "%1", "cwd": "~/user/u/p", "cmd": "claude"}


def shows(monkeypatch, name: str) -> bool:
    seen = []
    def fake(*args, **k):
        seen.append(args)
        return (FIX / name).read_text()
    monkeypatch.setattr(tmux, "tmux", fake)
    r = tmux.showing_prompt(PANE, [])
    assert all("-S" not in a for a in seen)   # visible screen only, no history
    return r


@pytest.mark.parametrize("name", ["idle_claude.txt", "idle_quoting.txt",
                                  "idle_quoting_lines.txt"])   # real: a reply with the dialog's lines on their own
def test_idle_pane_is_not_a_prompt(monkeypatch, name):
    assert not shows(monkeypatch, name)


@pytest.mark.parametrize("name", ["permission_dialog.txt", "session_survey.txt",
                                  "permission_dialog_touch.txt", "trust_dialog.txt"])   # real captures, 2026-10-31
def test_dialog_at_the_bottom_is_a_prompt(monkeypatch, name):
    assert shows(monkeypatch, name)


def test_dialog_scrolled_above_the_bottom_is_not(monkeypatch):
    dialog = (FIX / "permission_dialog.txt").read_text()
    monkeypatch.setattr(tmux, "tmux", lambda *a, **k: dialog + "\n".join(f"● line {i}" for i in range(12)) + "\n")
    assert not tmux.showing_prompt(PANE, [])


def test_trust_dialog_refuses_type_and_answer(monkeypatch):   # its default is "No, exit": keys would quit claude
    from jv.verb import JvError
    screen = (FIX / "trust_dialog.txt").read_text()
    monkeypatch.setattr(tmux, "tmux", lambda *a, **k: screen)
    monkeypatch.setattr(tmux, "resolve", lambda t: {**PANE, "window_index": "5", "alternate": False, "in_mode": False})
    monkeypatch.setattr(tmux, "load_sessions", lambda include_ended=False: [
        {"session_id": "live", "tmux_pane": PANE["id"], "state": "idle"}])
    assert tmux.trusting(PANE)
    for fn, a in [(tmux._type_check, {"target": "5", "text": "list the files", "enter": True}),
                  (tmux._type, {"target": "5", "text": "list the files"}),
                  (tmux._answer_check, {"target": "5", "choice": "1"}), (tmux._answer, {"target": "5", "choice": "2"})]:
        with pytest.raises(JvError) as e:
            fn(a)
        assert e.value.code == 4 and "trust this folder" in e.value.msg


def test_permission_dialog_is_not_trust(monkeypatch):
    monkeypatch.setattr(tmux, "tmux", lambda *a, **k: (FIX / "permission_dialog.txt").read_text())
    assert not tmux.trusting(PANE)


def ask(monkeypatch, name: str):
    monkeypatch.setattr(tmux, "tmux", lambda *a, **k: (FIX / name).read_text())
    return tmux.dialog("%1")


def test_dialog_bash_permission(monkeypatch):
    d = ask(monkeypatch, "permission_dialog.txt")
    assert d["kind"] == "permission" and [n for n, _ in d["options"]] == [1, 2, 3]
    assert tmux.ask_line(d) == ("bash command: uv run pytest -q tests/test_tmux.py? 1 yes, "
                                "2 yes and don't ask again, 3 no and tell claude")


@pytest.mark.parametrize("long", [False, True])
def test_answer_readback_includes_bounded_dialog_question(monkeypatch, long):
    from jv.verb import READBACK_MAX
    capture = (FIX / "permission_dialog.txt").read_text()
    if long:
        capture = capture.replace("uv run pytest -q tests/test_tmux.py", "touch " + "some-file " * 60)
    monkeypatch.setattr(tmux, "tmux", lambda *a, **k: capture)
    monkeypatch.setattr(tmux, "resolve", lambda t: {**PANE, "window_index": "3", "started": ""})
    monkeypatch.setattr(tmux, "load_sessions", lambda include_ended=False: [
        {"session_id": "live", "tmux_pane": "%1", "state": "idle"}])
    sentence = tmux._answer_check({"target": "3", "choice": "1"})
    assert "bash command:" in sentence and ("touch" if long else "uv run pytest") in sentence
    assert "'Yes'" in sentence and "window 3" in sentence and len(sentence) <= READBACK_MAX


def test_dialog_skips_tip_and_dashed_rules(monkeypatch):
    d = ask(monkeypatch, "permission_dialog_touch.txt")
    assert d["question"] == "bash command: Create empty file x?"
    assert [n for n, _ in d["options"]] == [1, 2, 3, 4] and tmux.ask_line(d).endswith("4 no")


def test_dialog_trust_and_survey_and_idle(monkeypatch):
    assert ask(monkeypatch, "trust_dialog.txt")["kind"] == "trust"
    assert ask(monkeypatch, "session_survey.txt")["options"][0] == (1, "Bad")
    for name in ["idle_claude.txt", "idle_quoting.txt", "idle_quoting_lines.txt"]:
        assert ask(monkeypatch, name) is None


def test_prompt_verb_says_one_line(monkeypatch):
    monkeypatch.setattr(tmux, "resolve", lambda t: {**PANE, "window_index": "3", "target": "0:3.0"})
    monkeypatch.setattr(tmux, "tmux", lambda *a, **k: (FIX / "permission_dialog.txt").read_text())
    assert tmux._prompt({"target": "3"})["say"].startswith("Window 3 asks: bash command: uv run")
    monkeypatch.setattr(tmux, "tmux", lambda *a, **k: (FIX / "trust_dialog.txt").read_text())
    assert "on screen" in tmux._prompt({"target": "3"})["say"]
    monkeypatch.setattr(tmux, "tmux", lambda *a, **k: (FIX / "idle_claude.txt").read_text())
    assert tmux._prompt({"target": "3"})["say"] == "Window 3 isn't asking anything."
