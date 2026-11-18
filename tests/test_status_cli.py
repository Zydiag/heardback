import json
from copy import deepcopy
from pathlib import Path

import pytest

from heardback.status import store
from jv.cli import all_verbs, catalogue
from jv.selftest import problems

FIX = str(Path(__file__).parent / "fixtures" / "transcript.ndjson")


@pytest.fixture
def sessions(state_dir, monkeypatch):
    targets = {"%1": ("0:1.0", "zsh"), "%7": ("0:7.0", "zsh"), "%2": ("0:2.0", "build-03")}
    monkeypatch.setattr(store, "alive", lambda pid: pid is not None)
    monkeypatch.setattr(store, "pane_targets", lambda socket=None: targets)

    def add(sid, project, pane, state="idle", **kw):
        rec = {"session_id": sid, "project": project, "tmux_pane": pane, "state": state, "claude_pid": 100,
               "updated_at": store.now_iso(), **kw}
        (state_dir / f"{sid}.json").write_text(json.dumps(rec))
    add("aaaa1111", "career", "%1", last_reply="Done.")
    add("bbbb2222", "career", "%7", "working", turn_started_at=store.now_iso())
    add("cccc3333", "heardback", "%2", transcript_path=FIX)
    return add


def test_list(sessions, run_jv):
    code, out, _ = run_jv("status", "list", "--json")
    assert code == 0 and out["ok"] and len(out["sessions"]) == 3
    assert "career in window 7 is working" in out["say"].lower()


def test_get_ambiguous(sessions, run_jv):
    code, out, err = run_jv("status", "get", "career", "--json")
    assert code == 1 and "career in window 1" in err and "career in window 7" in err
    assert sorted(out["candidates"]) == ["career in window 1", "career in window 7"]


def test_get_not_found(sessions, run_jv):
    code, _, err = run_jv("status", "get", "zzzzzz")
    assert code == 1 and "no session matches zzzzzz" in err


def test_get_one(sessions, run_jv):
    code, out, _ = run_jv("status", "get", "1", "--json")
    assert code == 0 and out["session"]["session_id"] == "aaaa1111" and out["say"].startswith("Career in window 1 finished")


def test_waiting_none(sessions, run_jv):
    assert run_jv("status", "waiting", "--json")[1]["say"] == "Nothing's waiting."


def test_waiting_one(sessions, run_jv):
    sessions("dddd4444", "web", None, "waiting", waiting_for="Claude needs your permission to use Bash")
    out = run_jv("status", "waiting", "--json")[1]
    assert [s["session_id"] for s in out["sessions"]] == ["dddd4444"] and "needs your permission for Bash" in out["say"]


def test_summary(sessions, run_jv):
    all_ = run_jv("status", "summary", "--json")[1]
    assert all_["text"] == all_["say"] and "Career in window 1 finished" in all_["text"]
    assert run_jv("status", "summary", "heardback", "--json")[1]["text"] == "Heardback is idle."


def test_get_by_window_name(sessions, run_jv):
    code, out, _ = run_jv("status", "get", "build", "--json")
    assert code == 0 and out["session"]["session_id"] == "cccc3333"


def test_detail(sessions, run_jv):
    code, out, _ = run_jv("status", "detail", "2", "--n", "1", "--json")
    assert code == 0 and [t["text"] for t in out["turns"]] == ["also the gate", "Gate done too."]
    assert out["say"] == "Heardback: you asked 'also the gate'. It said: Gate done too."
    code, out, _ = run_jv("status", "detail", "7", "--json")  # no transcript_path (registry-only, pre-hooks)
    assert code == 0 and out["turns"] == [] and out["say"].startswith("No detail for career")


def test_catalogue(run_jv):
    cat = run_jv("list", "--json")[1]
    v = next(v for v in cat["verbs"] if v["name"] == "status.list")
    assert v["voice"] is True and problems(catalogue(all_verbs())) == []
    assert "status.tree" not in {v["name"] for v in cat["verbs"]}


def test_load_sessions_liveness_is_history_only(state_dir):
    from jv.status import load_sessions
    (state_dir / "s.json").write_text(json.dumps({"session_id": "s", "tmux_pane": "%1", "state": "working",
                                                  "claude_pid": 999999999, "updated_at": store.now_iso()}))
    assert load_sessions() == []
    assert [(s["session_id"], s["state"]) for s in load_sessions(include_ended=True)] == [("s", "ended")]


@pytest.mark.parametrize("query,sid", [("windows two", "cccc3333"), ("window two", "cccc3333"),
                                         ("second window", "cccc3333"), ("0:2.0", "cccc3333"),
                                         ("%2", "cccc3333"), ("windows seven", "bbbb2222"),
                                         ("window one", "aaaa1111")])
def test_get_spoken_and_explicit_window_targets(sessions, run_jv, query, sid):
    code, out, _ = run_jv("status", "get", query, "--json")
    assert code == 0 and out["session"]["session_id"] == sid


def test_plural_status_phrase_uses_live_router_catalogue(sessions):
    from heardback.brain import catalogue as brain_catalogue, router

    verbs = deepcopy(all_verbs())
    next(v for v in verbs if v.name == "tmux.select").args["window"]["choices"] = lambda: []
    cat = brain_catalogue._index(catalogue(verbs))
    route = router.Router(cat).route("status of windows two")
    assert route.verb == "status.ask" and route.args == {"who": "2"}
    assert brain_catalogue.build_argv(cat[route.verb], route.args) == ["jv", "status", "ask", "--", "2"]


def test_status_route_descriptions_are_exact():
    from heardback.status.cli import VERBS

    descriptions = {v.name: v.description for v in VERBS}
    assert descriptions["status.get"] == "Say what one Claude Code session is doing from its hooks."
    assert descriptions["status.ask"] == ('Ask one Claude Code session for its own status ("status of window 3"). '
        "heardback types a fixed question and reads the answer aloud itself when the session stops; you only get 'asking'. "
        "A busy session is never typed into: the user is offered an ask when it's free. full true types plain status for the user to read.")
    assert descriptions["status.watch"] == ('Tell the user by voice when a Claude Code session finishes its current job '
        '("tell me when window 3 is done").')
    assert descriptions["status.detail"] == "Read the last request and first sentence of the reply from a session transcript."


def test_get_numeric_plural_miss_is_window_specific(sessions, run_jv):
    code, _, err = run_jv("status", "get", "windows 3", "--json")
    assert code == 1 and "no Claude session in window 3" in err


def test_default_list_filters_internal_and_ended(sessions, run_jv):
    sessions("internal1", "heardback", None, internal=True, cwd="/state/heardback/brain-cwd")
    sessions("ended1", "old-project", None, "ended")
    code, out, _ = run_jv("status", "list", "--json")
    assert code == 0 and out["count"] == 3
    assert {s["session_id"] for s in out["sessions"]} == {"aaaa1111", "bbbb2222", "cccc3333"}
    assert out["say"].startswith("3 live local Claude Code sessions.")
    assert "old-project" not in out["say"].casefold()
    code, internal, _ = run_jv("status", "internal", "--json")
    assert code == 0 and internal["count"] == 1 and internal["sessions"][0]["session_id"] == "internal1"
    code, ended, _ = run_jv("status", "get", "old-project", "--json")
    assert code == 0 and ended["session"]["state"] == "ended"


def test_summary_usage_line(sessions, run_jv, state_dir):
    from datetime import date
    u = state_dir.parent / "usage.json"
    u.write_text(json.dumps({"date": date.today().isoformat(), "turns": 149, "by_model": {}}))
    assert "Claude turns" not in run_jv("status", "summary", "--json")[1]["text"]
    u.write_text(json.dumps({"date": date.today().isoformat(), "turns": 152, "by_model": {}}))
    out = run_jv("status", "summary", "--json")[1]
    assert out["text"].endswith(" You've used 152 Claude turns today.") and out["say"] == out["text"]
    u.write_text(json.dumps({"date": "2020-01-01", "turns": 500, "by_model": {}}))
    assert "Claude turns" not in run_jv("status", "summary", "--json")[1]["text"]
