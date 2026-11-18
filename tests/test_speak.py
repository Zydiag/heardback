from heardback.status import speak
from heardback.status.store import now_iso

NOW = 1_800_000_000.0
PERM = "Claude needs your permission to use Bash"


def s(sid, project, state, target=None, ago=0, **kw):
    return {"session_id": sid, "project": project, "state": state, "tmux_target": target,
            "tmux_pane": "%1" if target else None, "waiting_for": None, "last_reply": None,
            "turn_started_at": now_iso(NOW - ago) if state == "working" else None,
            "updated_at": now_iso(NOW - ago), **kw}


CAREER = s("a", "career", "waiting", "0:1.0", waiting_for=PERM)
TRACKER = s("b", "webapp", "working", "0:2.0", ago=240)
HEARDBACK = s("c", "heardback", "idle", "0:6.0", ago=10, last_reply="spec written")


def test_ago():
    assert speak.ago(5) == "under a minute"
    assert speak.ago(60) == "1 minute"
    assert speak.ago(240) == "4 minutes"
    assert speak.ago(3600) == "1 hour"
    assert speak.ago(7300) == "2 hours"


def test_need_phrase():
    assert speak.need_phrase(PERM) == "your permission for Bash"
    assert speak.need_phrase("Claude needs your permission") == "your permission"  # live hook text
    assert speak.need_phrase("permission prompt") == "your permission"  # live registry waitingFor
    assert speak.need_phrase("Claude is waiting for your input") == "your input"
    assert speak.need_phrase(None) == "your input"


def test_overview_spec_example():
    assert speak.overview([HEARDBACK, TRACKER, CAREER], NOW) == (
        "3 live local Claude Code sessions. Career is waiting for your permission. "
        "Webapp is working, 4 minutes in. Heardback finished: spec written.")


def test_overview_none_and_ended():
    assert speak.overview([], NOW) == "No live local Claude Code sessions running."
    assert speak.overview([s("x", "old", "ended")], NOW) == "No live local Claude Code sessions running."


def test_overview_waiting_input_and_idle_no_reply():
    sess = [s("a", "career", "waiting", waiting_for="Claude is waiting for your input"), s("b", "notes", "idle")]
    assert speak.overview(sess, NOW) == "2 live local Claude Code sessions. Career is waiting for you. Notes is idle."


def test_overview_more_idle():
    sess = [CAREER, TRACKER] + [s(str(i), f"p{i}", "idle", ago=i) for i in range(4)]
    out = speak.overview(sess, NOW)
    assert out.startswith("6 live local Claude Code sessions.") and out.count(".") == 6
    assert "P0 is idle. P1 is idle." in out and out.endswith("and 2 more idle.")


def test_overview_more_mixed():
    sess = [s(str(i), f"w{i}", "working") for i in range(5)] + [s("z", "q", "idle")]
    assert speak.overview(sess, NOW).endswith("and 2 more.")


def test_one():
    tracker_idle = s("b", "webapp", "idle", "0:2.0", ago=120, last_reply="added the export button")
    assert speak.one(tracker_idle, [tracker_idle], NOW) == "Webapp finished 2 minutes ago: added the export button."
    bare = s("b", "notes", "idle")
    assert speak.one(bare, [bare], NOW) == "Notes is idle."
    w = s("b", "x", "working", ago=360)
    assert speak.one(w, [w], NOW) == "X is working, 6 minutes in."
    assert speak.one(CAREER, [CAREER], NOW) == "Career is waiting for your permission for Bash."
    e = s("e", "old", "ended")
    assert speak.one(e, [e], NOW) == "Old has ended."


def test_waiting(monkeypatch):
    from heardback.status import announce
    monkeypatch.setattr(announce, "pane_question", lambda _: ("Run?", {}))
    assert speak.waiting([TRACKER, HEARDBACK]) == "Nothing's waiting."
    assert speak.waiting([CAREER, TRACKER]) == "Yes, career needs your permission for Bash."
    other = s("d", "notes", "waiting", waiting_for="Claude is waiting for your input")
    assert speak.waiting([CAREER, other]) == "Yes, career needs your permission for Bash and notes needs your input."


def test_waiting_ignores_permission_without_visible_dialog(monkeypatch):
    from heardback.status import announce
    monkeypatch.setattr(announce, "pane_question", lambda _: None)
    stale = {**CAREER, "tmux_pane": "%1"}
    assert speak.waiting([stale]) == "Nothing's waiting."


def test_waiting_duplicate_names(monkeypatch):
    from heardback.status import announce
    monkeypatch.setattr(announce, "pane_question", lambda _: ("Run?", {}))
    two = [CAREER, {**CAREER, "session_id": "z", "tmux_target": "0:7.0"}]
    assert speak.waiting(two) == ("Yes, career in window 1 needs your permission for Bash"
                                  " and career in window 7 needs your permission for Bash.")


def test_announce():
    sess = [CAREER, HEARDBACK]
    assert speak.announce("waiting", CAREER, sess, None) == "Career needs your permission for Bash."
    assert speak.announce("waiting", {**CAREER, "waiting_for": "x"}, sess, None) == "Career needs your input."
    assert speak.announce("finished", HEARDBACK, sess, "spec written") == "Heardback finished: spec written."
    assert speak.announce("finished", HEARDBACK, sess, None) == "Heardback finished."
    assert speak.announce("error", HEARDBACK, sess, "stopped with an error: rate limit") == (
        "Heardback stopped with an error: rate limit.")
