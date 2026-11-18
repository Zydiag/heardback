"""tmuxtype: "this window" = the active pane of the active window (fake panes(), no tmux server)."""
import pytest

from jv import tmux


def _pane(w, pane, active, wactive, name="w"):
    return {"id": f"%{w}{pane}", "target": f"0:{w}.{pane}", "window_index": str(w), "window_name": name,
            "active": active, "window_active": wactive, "cmd": "zsh", "cwd": "/x", "title": "", "in_mode": False,
            "alternate": False, "left": 0, "top": 0, "right": 10, "bottom": 10}


PANES = [_pane(0, 0, True, False, "first"), _pane(1, 0, False, True, "claude"), _pane(1, 1, True, True, "claude"),
         _pane(2, 0, True, False, "web")]


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    monkeypatch.setattr(tmux, "panes", lambda: PANES)
    monkeypatch.setattr(tmux, "load_sessions", lambda include_ended=False: [])


@pytest.mark.parametrize("t", ["this", "current", "here", "active", " This ", "CURRENT"])
def test_here_words_resolve_to_active_pane(t):
    assert tmux.resolve(t)["target"] == "0:1.1"


def test_list_marks_active_and_says_where():
    out = tmux._list({})
    assert [r["target"] for r in out["panes"] if r.get("active")] == ["0:1.1"]
    assert out["say"] == "3 windows, you're on 1"


def test_type_check_on_current(monkeypatch):
    monkeypatch.setattr(tmux, "bottom", lambda pid, n=10: ["$"])
    assert tmux._type_check({"target": "current", "text": "hi", "enter": True}) == \
        "Run 'hi' in window 1?"


@pytest.mark.parametrize("t,want", [("this window", "0:1.1"), ("the current window", "0:1.1"), ("window 2", "0:2.0"),
                                    ("window two", "0:2.0"), ("Window One", "0:1.1"), ("first window", "0:0.0"),
                                    ("the second window", "0:1.1"), ("third", "0:2.0"), ("tmux window 0", "0:0.0"),
                                    ("first", "0:0.0")])   # "first" is also a window name here: the name wins
def test_spoken_targets(t, want):
    assert tmux.resolve(t)["target"] == want


def test_ordinal_by_position_and_out_of_range(monkeypatch):   # base-index 1, as on the laptop
    ps = [_pane(1, 1, True, True, "claude"), _pane(2, 1, True, False, "zsh")]
    monkeypatch.setattr(tmux, "panes", lambda: ps)
    assert tmux.resolve("first window")["target"] == "0:1.1"
    with pytest.raises(tmux.JvError) as e:
        tmux.resolve("fifth window")
    assert e.value.code == 1


@pytest.mark.parametrize("text,want", [("hi", None), ("", None), ("a\nb", None)])
def test_enter_on_claude_pane_needs_no_readback(monkeypatch, text, want):
    monkeypatch.setattr(tmux, "prompt_draft", lambda pid: False)
    monkeypatch.setattr(tmux, "bottom", lambda pid, n=10: ["$"])
    ps = [dict(p, cmd="claude") if p["id"] == "%11" else p for p in PANES]
    monkeypatch.setattr(tmux, "panes", lambda: ps)
    monkeypatch.setattr(tmux, "load_sessions", lambda include_ended=False: [
        {"session_id": "live", "tmux_pane": "%11", "state": "idle"}])
    assert tmux._type_check({"target": "current", "text": text, "enter": True}) is want


def test_empty_enter_on_shell_reads_back_press_enter(monkeypatch):
    monkeypatch.setattr(tmux, "bottom", lambda pid, n=10: ["$"])
    assert tmux._type_check({"target": "current", "text": "", "enter": True}) == "Press enter in window 1?"


def test_alternate_screen_still_asks_without_enter(monkeypatch):
    monkeypatch.setattr(tmux, "bottom", lambda pid, n=10: ["$"])
    ps = [dict(p, alternate=True, cmd="vim") if p["id"] == "%11" else p for p in PANES]
    monkeypatch.setattr(tmux, "panes", lambda: ps)
    assert tmux._type_check({"target": "current", "text": "x"}) == "Type 'x' into vim in window 1?"


# --- fix2 P2: spoken enter endings; tmux.clear ---
@pytest.mark.parametrize("said,text", [("ls enter", "ls"), ("ls and press enter", "ls"), ("ls press enter.", "ls"),
                                       ("what's next and send it", "what's next"), ("ls, send it", "ls"),
                                       ("fix it and submit", "fix it"), ("Hi Enter", "Hi")])
def test_dictated_enter_tail(said, text):
    assert tmux._dictated({"target": "1", "text": said}) == {"target": "1", "text": text, "enter": True}


@pytest.mark.parametrize("said", ["enter", "press enter key now", "send it to me", "ls"])
def test_dictated_leaves_other_text(said):
    assert tmux._dictated({"text": said}) == {"text": said}


def test_dictated_enter_on_shell_pane_keeps_read_back(monkeypatch):
    monkeypatch.setattr(tmux, "bottom", lambda pid, n=10: ["$"])
    assert tmux._type_check({"target": "2", "text": "rm -rf x and press enter"}) == "Run 'rm -rf x' in window 2?"


def test_clear_sends_ctrl_u_to_current_or_named(monkeypatch):
    sent = []
    monkeypatch.setattr(tmux, "bottom", lambda pid, n=10: ["$"])
    monkeypatch.setattr(tmux, "tmux", lambda *a, **k: sent.append(a))
    assert tmux._clear({})["say"] == "sent clear-line keys to window 1"
    tmux._clear({"target": "2"})
    assert sent == [("send-keys", "-t", "%11", "C-u"), ("send-keys", "-t", "%20", "C-u")]


# --- fix2 P4: tmux.close; exit/quit into a Claude pane asks ---
def _claude_win1(monkeypatch):
    monkeypatch.setattr(tmux, "load_sessions", lambda include_ended=False: [
        {"tmux_pane": "%10", "state": "idle"}])   # window 1, pane 0


def test_close_read_back_names_windows_and_claude(monkeypatch):
    _claude_win1(monkeypatch)
    monkeypatch.setattr(tmux, "_tmux_foreground", lambda: True)
    assert tmux._close_check({"windows": "0, 1 and 2"}) == \
        "Close windows 0 first, 1 claude and 2 web? 1 is a Claude session."
    assert tmux._close_check({"windows": "web"}) == "Close window 2 web?"
    assert tmux._close_check({"windows": "two & this"}) == \
        "Close windows 2 web and 1 claude? 1 is a Claude session."


def test_close_kills_by_pane_id(monkeypatch):
    sent = []
    monkeypatch.setattr(tmux, "tmux", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(tmux, "_window_pane_inventory", lambda: {"@1": {"%00"}, "@2": {"%20"}})
    monkeypatch.setattr(tmux, "_pane_inventory", lambda: {"%10"})
    assert tmux._close({"windows": "0 2 2"})["say"] == "closed window 0. closed window 2"
    assert sent == [("kill-window", "-t", "%00"), ("kill-window", "-t", "%20")]


def test_close_partial_failure_preserves_accepted_and_unattempted_targets(monkeypatch):
    sent = []

    def fake_tmux(*args, **kwargs):
        if args[0] == "kill-window":
            sent.append(args)
            if args[-1] == "%11":
                raise tmux.JvError(1, "tmux: injected failure")
        return ""

    monkeypatch.setattr(tmux, "tmux", fake_tmux)
    monkeypatch.setattr(tmux, "_window_pane_inventory", lambda: {"@0": {"%00"}, "@1": {"%11"}, "@2": {"%20"}})
    with pytest.raises(tmux.JvError) as exc:
        tmux._close({"windows": "0, 1, 2"})
    assert sent == [("kill-window", "-t", "%00"), ("kill-window", "-t", "%11")]
    assert exc.value.extra["close_sent"] == ["0"]
    assert exc.value.extra["not_attempted"] == ["window 2"]


def test_close_with_unavailable_inventory_does_not_claim_closure(monkeypatch):
    monkeypatch.setattr(tmux, "tmux", lambda *a, **k: "")
    monkeypatch.setattr(tmux, "_window_pane_inventory", lambda: {"@2": {"%20"}})
    monkeypatch.setattr(tmux, "_pane_inventory", lambda: None)
    out = tmux._close({"windows": "2"})
    assert out["closed"] == []
    assert "could not be checked" in out["say"]


def test_implicit_close_targets_fail_closed_without_foreground(monkeypatch):
    monkeypatch.setattr(tmux, "_tmux_foreground", lambda: False)
    sent = []
    monkeypatch.setattr(tmux, "tmux", lambda *a, **k: sent.append(a))
    with pytest.raises(tmux.JvError, match="Which tmux pane"):
        tmux._close_pane_target({"target": "this pane"})
    with pytest.raises(tmux.JvError, match="Which tmux window"):
        tmux._close_targets({"windows": "the current window"})
    assert sent == []


def test_explicit_and_exact_name_close_targets_work_without_foreground(monkeypatch):
    monkeypatch.setattr(tmux, "_tmux_foreground", lambda: False)
    assert tmux._close_pane_target({"target": "%20"})[0]["id"] == "%20"
    assert tmux._close_pane_target({"target": "left pane", "window": "2"})[0]["id"] == "%20"
    ps = [_pane(0, 0, True, True, "current"), *PANES[1:]]
    monkeypatch.setattr(tmux, "panes", lambda: ps)
    assert tmux._close_pane_target({"target": "current"})[0]["id"] == "%00"


@pytest.mark.parametrize("text", ["exit", "Quit", "/exit", "/quit", "/clear", " exit "])
def test_enter_ending_session_in_claude_pane_asks(monkeypatch, text):
    monkeypatch.setattr(tmux, "prompt_draft", lambda pid: False)
    _claude_win1(monkeypatch)
    monkeypatch.setattr(tmux, "bottom", lambda pid, n=10: ["❯"])
    monkeypatch.setattr(tmux, "screen", lambda pid: "❯")
    monkeypatch.setattr(tmux, "_session", lambda p, s: None)
    monkeypatch.setattr(tmux, "is_claude", lambda p, s: True)
    q = tmux._type_check({"target": "1", "text": text, "enter": True})
    assert q == f"Send '{text.strip()}' to the Claude session in window 1?"
    assert tmux._type_check({"target": "1", "text": "fix the tests", "enter": True}) is None
    assert tmux._type_check({"target": "1", "text": "exit"}) is None   # no enter: just text on the line
