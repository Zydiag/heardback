"""Focused lifecycle contracts with only tmux, session-store, and clock boundaries faked."""
from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from heardback.status import store, watch
from jv import hypr, tmux
from jv.verb import JvError
from test_tmux import desktop_config  # noqa: F401


NOW = 1_800_000_000.0
_REAL_NOW_ISO = store.now_iso
PANE = {
    "id": "%1", "target": "0:1.0", "window_index": "1", "window_name": "work",
    "active": True, "window_active": True, "cmd": "claude", "cwd": "/tmp/work",
    "title": "✳ Claude Code", "started": str(NOW - 20), "in_mode": False, "alternate": False,
    "left": 0, "top": 0, "right": 100, "bottom": 40,
}


def stamp(value: float) -> str:
    return _REAL_NOW_ISO(value)


def session(sid="s1", pane_id="%1", state="working", **kw):
    return {
        "session_id": sid, "tmux_pane": pane_id, "tmux_target": "0:1.0", "state": state,
        "source": "hook", "started_at": stamp(NOW - 20), "updated_at": stamp(NOW - 10),
        "last_hook": None, "last_prompt": None, "claude_pid": None, **kw,
    }


@pytest.fixture
def fixed_clock(monkeypatch):
    monkeypatch.setattr(tmux, "time", SimpleNamespace(time=lambda: NOW, sleep=lambda _seconds: None))
    monkeypatch.setattr(store, "now_iso", lambda value=None: stamp(NOW if value is None else value))
    return stamp(NOW)


@pytest.mark.parametrize("focused", [False, True])
def test_new_keeps_returned_pane_and_target_when_active_window_changes(monkeypatch, fixed_clock, focused):
    calls, active = [], {"target": "0:1.0"}
    current = session("new-session", pane_id="%42", state="idle")

    def fake_tmux(*args, input=None):
        calls.append(args)
        if args[0] == "new-window":
            # The user changes focus after tmux returns the created identity.
            active["target"] = "0:99.0"
            return "%42\t0:42.0\n"
        if args[0] == "set-option":
            assert args[args.index("-t") + 1] == "%42"
            return ""
        raise AssertionError(f"unexpected tmux read/action: {args}")

    monkeypatch.setattr(tmux, "tmux", fake_tmux)
    def focus():
        assert calls[0][0] == "new-window"
        calls.append(("focus-kitty",))
        return focused
    monkeypatch.setattr(tmux, "_focus_kitty", focus)
    monkeypatch.setattr(tmux, "_new_dir", lambda _a: Path("/tmp/work"))
    monkeypatch.setattr(tmux, "_readiness", lambda pane_id: "ready" if pane_id == "%42" else "pending")
    monkeypatch.setattr(tmux, "panes", lambda: [{"id": "%42", "window_index": "42", "window_active": True}])
    monkeypatch.setattr(tmux, "load_sessions", lambda include_ended=False: [current])
    monkeypatch.setattr(store, "list_sessions", lambda **_kw: [current])

    out = tmux._new({"run": "claude", "name": "work"})

    assert out["target"] == "0:42.0" and active["target"] == "0:99.0"
    assert out["say"] == "opened work; Claude is ready in window 42"
    assert [c[0] for c in calls] == ["new-window", "set-option", "focus-kitty"]


@pytest.mark.parametrize("failure", [None, "clients", "list-clients", "os", "key", "focus"])
def test_new_focus_is_best_effort_and_watch_still_arms(monkeypatch, fixed_clock, failure):
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        if "new-window" in argv:
            out = "%42\t0:42.0\n"
        elif "set-option" in argv:
            out = ""
        elif "list-clients" in argv:
            if failure == "list-clients":
                return SimpleNamespace(returncode=1, stdout="", stderr="client vanished")
            out = "50\n"
        else:
            pytest.fail(str(argv))
        return SimpleNamespace(returncode=0, stdout=out, stderr="")
    def hypr_run(argv):
        calls.append(argv)
        if "clients" in argv:
            if failure == "clients":
                raise JvError(1, "hyprctl unavailable")
            if failure == "os":
                raise OSError("socket unavailable")
            return json.dumps([{"class": "kitty", "pid": 50,
                                **({} if failure == "key" else {"address": "0xaaa"})}])
        assert "dispatch" in argv
        if failure == "focus":
            raise JvError(1, "window vanished")
        return "ok"
    monkeypatch.setattr(tmux.subprocess, "run", run)
    monkeypatch.setattr(hypr, "_run", hypr_run)
    monkeypatch.setattr(hypr, "_inst", "test")
    monkeypatch.setenv("JV_DRY", "0")
    monkeypatch.setenv("HEARDBACK_TURN", "create")
    monkeypatch.setattr(tmux, "_new_dir", lambda _a: Path("/tmp/work"))
    monkeypatch.setattr(tmux, "_readiness", lambda _pane: "pending")
    monkeypatch.setattr(tmux, "panes", lambda: [{"id": "%42"}])

    out = tmux._new({"run": "claude", "name": "work", "prompt": "hi"})

    assert out["target"] == "0:42.0" and "opened work" in out["say"]
    assert "no watch" not in out["say"]
    assert watch.take("%42", stamp(NOW + 1), now=NOW + 1)["turn"] == "create"
    assert sum("new-window" in call for call in calls) == 1
    assert any("dispatch" in call for call in calls) == (failure is None or failure == "focus")
    assert tmux._focus_kitty() is (failure is None)


@pytest.mark.parametrize(("operation", "result"), [
    ("new", "%77\tbad-target"),
    ("split", "not-a-pane"),
])
def test_malformed_create_identity_never_marks_a_guessed_pane(monkeypatch, operation, result):
    calls = []
    monkeypatch.setattr(tmux, "tmux", lambda *args, **_kwargs: calls.append(args) or result)
    monkeypatch.setattr(tmux, "_new_dir", lambda _a: Path("/tmp/work"))
    monkeypatch.setattr(tmux, "resolve", lambda _target: dict(PANE))

    if operation == "new":
        out = tmux._new({"run": "claude", "name": "work"})
    else:
        out = tmux._split({"window": "1", "dir": "vertical", "run": "claude"})

    assert out["target"] is None and "malformed" in out["say"]
    assert all(call[0] != "set-option" for call in calls)


@pytest.mark.parametrize(("case", "want"), [
    ("missing", "starting"),
    ("wrong_pane", "starting"),
    ("ended", "starting"),
    ("recent_start", "starting"),
    ("trust", "waiting for folder trust on screen"),
    ("permission", "waiting for input"),
    ("idle", "ready"),
    ("working", "working"),
])
def test_readiness_requires_current_live_identity_and_clear_startup_screen(monkeypatch, fixed_clock, case, want):
    pane = dict(PANE, id="%9", started=str(NOW - 20))
    current = session("live", pane_id="%9", state="idle")
    if case == "missing":
        records = []
    elif case == "wrong_pane":
        records = [session("other-pane", pane_id="%8", state="idle")]
    elif case == "ended":
        records = [session("ended", pane_id="%9", state="ended")]
    elif case == "recent_start":
        pane["started"] = str(NOW - 1)
        records = [current]
    elif case == "working":
        records = [dict(current, state="working")]
    else:
        records = [current]

    monkeypatch.setattr(tmux, "resolve", lambda target: dict(pane) if target == "%9" else pytest.fail(target))
    monkeypatch.setattr(tmux, "load_sessions", lambda include_ended=False: records)
    if case == "trust":
        monkeypatch.setattr(tmux, "bottom", lambda _pane_id, n=10: ["❯ Yes, I trust this folder"])
    elif case == "permission":
        monkeypatch.setattr(tmux, "bottom", lambda _pane_id, n=10: [
            "────────────────────────", "Do you want to proceed?", "❯ 1. Yes", "Esc to cancel",
        ])
    else:
        monkeypatch.setattr(tmux, "bottom", lambda _pane_id, n=10: [])

    assert tmux._readiness("%9") == want


def _type_setup(monkeypatch, before, after=None):
    pane = dict(PANE)
    current = [before]
    keys = []
    monkeypatch.setattr(tmux, "resolve", lambda _target: dict(pane))
    monkeypatch.setattr(tmux, "load_sessions", lambda include_ended=False: [before])
    monkeypatch.setattr(store, "list_sessions", lambda **_kw: list(current))
    monkeypatch.setattr(tmux, "bottom", lambda _pane_id, n=10: [])
    monkeypatch.setattr(tmux, "SUBMIT_POLLS", 4)

    def fake_tmux(*args, **_kwargs):
        if args[0] == "send-keys":
            keys.append(args)
            if args[-1] == "Enter" and after is not None:
                current[:] = after if isinstance(after, list) else [after]
            return ""
        raise AssertionError(f"unexpected tmux action: {args}")

    monkeypatch.setattr(tmux, "tmux", fake_tmux)
    return keys


def test_type_without_enter_and_enter_to_working_claude_have_distinct_results(monkeypatch, fixed_clock):
    before = session(state="working")
    keys = _type_setup(monkeypatch, before)
    typed = tmux._type({"target": "%1", "text": "later"})
    assert typed["queued"] is False
    assert typed["say"] == "typed into window 1 without pressing Enter"
    assert all(args[-1] != "Enter" for args in keys)

    submitted = tmux._type({"target": "%1", "text": "later", "enter": True})
    assert submitted["queued"] is True
    assert submitted["say"] == "queued for window 1"
    assert any(args[-1] == "Enter" for args in keys)


@pytest.mark.parametrize("case", ["fresh_match", "stale_hook", "wrong_identity", "wrong_prefix"])
def test_submit_is_observed_only_with_fresh_matching_hook_and_identity(monkeypatch, fixed_clock, case):
    text = "inspect the current change"
    before = session("same", state="working")
    after = session("same", state="working", last_hook="UserPromptSubmit", last_prompt=text,
                    updated_at=stamp(NOW + 1))
    if case == "stale_hook":
        after["updated_at"] = stamp(NOW - 1)
    elif case == "wrong_identity":
        after["session_id"] = "replacement"
    elif case == "wrong_prefix":
        after["last_prompt"] = "different prompt"
    _type_setup(monkeypatch, before, after)

    out = tmux._type({"target": "%1", "text": text, "enter": True})

    if case == "fresh_match":
        assert out["say"] == "Claude in window 1 accepted the submitted request"
    else:
        assert out["queued"] is True and out["say"] == "queued for window 1"


def test_clear_observes_old_and_new_session_identities(monkeypatch, fixed_clock):
    before = session("old", state="working")
    old_ended = session("old", state="ended", end_reason="clear", updated_at=stamp(NOW + 1))
    replacement = session("new", state="idle", started_at=stamp(NOW + 1), updated_at=stamp(NOW + 1))
    _type_setup(monkeypatch, before, [old_ended, replacement])

    out = tmux._type({"target": "%1", "text": "/clear", "enter": True})

    assert out["say"] == "sent /clear in window 1; a new session identity was observed"


def test_clear_with_only_old_identity_ended_keeps_replacement_pending(monkeypatch, fixed_clock):
    before = session("old", state="working")
    old_ended = session("old", state="ended", end_reason="clear", updated_at=stamp(NOW + 1))
    _type_setup(monkeypatch, before, [old_ended])

    out = tmux._type({"target": "%1", "text": "/clear", "enter": True})

    assert out["say"] == "sent /clear in window 1; the old session ended, but its replacement identity is not yet observed"


@pytest.mark.parametrize("end_reason", ["exit", "gone"])
def test_exit_is_reported_only_once_the_session_ended(monkeypatch, fixed_clock, end_reason):
    before = session("old", state="working")
    ended = session("old", state="ended", end_reason=end_reason, last_hook="SessionEnd",
                    updated_at=stamp(NOW + 1))
    keys = _type_setup(monkeypatch, before, [ended])

    out = tmux._type({"target": "%1", "text": "exit", "enter": True})

    assert out["say"] == "the Claude session in window 1 has ended" and any(args[-1] == "Enter" for args in keys)


def test_stale_claude_looking_pane_refuses_all_key_paths_without_recent_history(monkeypatch, fixed_clock):
    pane = dict(PANE, cmd="zsh", title="✳ Claude Code", started="")
    sent = []
    monkeypatch.setattr(tmux, "resolve", lambda _target: dict(pane))
    monkeypatch.setattr(tmux, "load_sessions", lambda include_ended=False: [])  # expired history is absent
    monkeypatch.setattr(tmux, "bottom", lambda _pane_id, n=10: [])
    monkeypatch.setattr(tmux, "tmux", lambda *args, **_kw: sent.append(args) or "")

    calls = [
        (tmux._type_check, {"target": "%1", "text": "hello"}),
        (tmux._type, {"target": "%1", "text": "hello"}),
        (tmux._clear, {"target": "%1"}),
        (tmux._answer_check, {"target": "%1", "choice": "1"}),
        (tmux._answer, {"target": "%1", "choice": "1"}),
    ]
    for operation, args in calls:
        with pytest.raises(tmux.JvError) as error:
            operation(args)
        assert error.value.code == 4 and "can't confirm a live Claude session" in error.value.msg
    assert sent == []


def test_folder_trust_screen_still_refuses_clear_and_answer(monkeypatch, fixed_clock):
    live = session("live", state="idle")
    monkeypatch.setattr(tmux, "resolve", lambda _target: dict(PANE))
    monkeypatch.setattr(tmux, "load_sessions", lambda include_ended=False: [live])
    monkeypatch.setattr(tmux, "bottom", lambda _pane_id, n=10: ["❯ Yes, I trust this folder"])
    sent = []
    monkeypatch.setattr(tmux, "tmux", lambda *args, **_kw: sent.append(args) or "")

    for operation, args in [(tmux._clear, {"target": "%1"}),
                            (tmux._answer_check, {"target": "%1", "choice": "1"}),
                            (tmux._answer, {"target": "%1", "choice": "1"})]:
        with pytest.raises(tmux.JvError) as error:
            operation(args)
        assert error.value.code == 4
        assert "trust this folder" in error.value.msg or "showing a prompt" in error.value.msg
    assert sent == []


def _two_panes():
    return [dict(PANE), dict(PANE, id="%2", target="0:1.1", active=False, left=50)]


@pytest.mark.parametrize(("inventory", "pane_observed"), [({"%2"}, True), ({"%1", "%2"}, False), (None, False)])
def test_close_pane_claims_closure_only_when_the_pane_is_gone(monkeypatch, fixed_clock, inventory, pane_observed):
    monkeypatch.setattr(tmux, "panes", _two_panes)
    monkeypatch.setattr(tmux, "_pane_inventory", lambda: inventory)
    calls = []
    monkeypatch.setattr(tmux, "tmux", lambda *args, **_kw: calls.append(args) or "")

    out = tmux._close_pane({"target": "%1"})

    assert calls == [("kill-pane", "-t", "%1")]
    assert (out["closed"] is not None) is pane_observed
    assert ("not confirmed" not in out["say"]) is pane_observed


@pytest.mark.parametrize(("inventory_fn", "raw"), [
    (tmux._pane_inventory, ""),
    (tmux._pane_inventory, "not-a-pane"),
    (tmux._pane_inventory, "%1\n%1"),
    (tmux._window_pane_inventory, ""),
    (tmux._window_pane_inventory, "%1\t@1\nmalformed"),
    (tmux._window_pane_inventory, "%1\t@1\n%1\t@1"),
])
def test_incomplete_pane_inventories_remain_unknown(monkeypatch, inventory_fn, raw):
    monkeypatch.setattr(tmux, "tmux", lambda *_args, **_kw: raw)
    assert inventory_fn() is None


@pytest.mark.parametrize("inventory_fn", [tmux._pane_inventory, tmux._window_pane_inventory])
def test_failed_pane_inventory_remains_unknown(monkeypatch, inventory_fn):
    def fail(*_args, **_kwargs):
        raise tmux.JvError(1, "tmux unavailable")

    monkeypatch.setattr(tmux, "tmux", fail)
    assert inventory_fn() is None


def test_submit_waits_for_a_late_hook(monkeypatch, fixed_clock):
    """The hook lands 30-60 ms after Enter (live 10:23:10): the check must wait for it, and stay quiet if it never comes."""
    before = session("same", state="idle")
    after = session("same", state="working", last_hook="UserPromptSubmit", last_prompt="", updated_at=stamp(NOW + 1))
    _type_setup(monkeypatch, before)
    reads = iter([None, None, after])
    real = tmux._session_for_pane
    monkeypatch.setattr(tmux, "_session_for_pane", lambda pane_id, **kw: next(reads, after) or real(pane_id, **kw))
    out = tmux._type({"target": "%1", "text": "", "enter": True})
    assert out["say"] == "Claude in window 1 accepted the submitted request"

    monkeypatch.setattr(tmux, "_session_for_pane", real)
    _type_setup(monkeypatch, before)
    out = tmux._type({"target": "%1", "text": "", "enter": True})
    assert out["say"] == "typed into window 1 and pressed Enter"
