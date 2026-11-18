import asyncio
import json
import os
import pytest

from heardback.status import announce
from heardback.status.announce import Announcer, Tail
from event_voice import due

T0 = 1_800_000_000.0


@pytest.fixture(autouse=True)
def isolated_away(monkeypatch):
    from heardback.status import away
    monkeypatch.setattr(away, "is_away", lambda: away.flag_path().exists())


def sess(sid, project, state, **kw):
    return {"session_id": sid, "project": project, "state": state, "tmux_pane": None, **kw}


class World:
    def __init__(self, *sessions):
        self.t, self.away, self.muted, self.s = [T0], True, False, {s["session_id"]: s for s in sessions}
        self.busy = False
        self.ann = Announcer(now=lambda: self.t[0], sessions=lambda: list(self.s.values()))

    def at(self, dt):
        self.t[0] = T0 + dt
        return due(self.ann, muted=self.muted, at_desk=not self.away, busy=self.busy)

    def feed(self, dt, sid, frm, to, hook, **kw):
        self.t[0] = T0 + dt
        self.s[sid]["state"] = to
        self.ann.feed({"session_id": sid, "from": frm, "to": to, "hook": hook, **kw})


PERM = "Claude needs your permission to use Bash"


def test_finished_needs_30s():
    w = World(sess("a", "webapp", "working"))
    w.feed(0, "a", "working", "idle", "Stop", turn_s=29, detail="added the export button")
    assert w.at(10) == []
    w.feed(20, "a", "working", "idle", "Stop", turn_s=30, detail="added the export button")
    assert w.at(25) == ["Webapp finished: added the export button."]


def test_idle_prompt_silent():
    w = World(sess("a", "heardback", "working"))
    w.feed(0, "a", "working", "idle", "Notification")
    assert w.at(10) == []


def test_internal_and_ended_sessions_are_not_announced():
    internal = sess("internal", "heardback", "waiting", internal=True)
    ended = sess("ended", "old-project", "ended")
    w = World(internal, ended)
    w.ann.feed({"session_id": "internal", "from": "working", "to": "waiting", "hook": "Notification"})
    w.ann.feed({"session_id": "ended", "from": "working", "to": "waiting", "hook": "Notification"})
    assert w.at(10) == []


def test_error_announced():
    w = World(sess("a", "heardback", "working"))
    w.feed(0, "a", "working", "idle", "StopFailure", turn_s=3, detail="stopped with an error: rate limit")
    assert w.at(5) == ["Heardback stopped with an error: rate limit."]


def test_stop_after_permission_announces_finished():
    w = World(sess("a", "heardback", "waiting"))
    w.feed(0, "a", "waiting", "idle", "Stop", turn_s=3, detail="Denied.")
    assert w.at(5) == ["Heardback finished: Denied."]


def test_pending_announcements_survive_restart_for_multiple_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(announce.store, "state_dir", lambda **_: tmp_path)
    sessions = [sess("a", "career", "waiting"), sess("b", "heardback", "waiting")]
    now = [T0]
    first = Announcer(now=lambda: now[0], sessions=lambda: sessions)
    first.feed({"session_id": "a", "from": "working", "to": "waiting", "hook": "Notification"})
    first.feed({"session_id": "b", "from": "working", "to": "waiting", "hook": "Notification"})
    now[0] += 5
    restarted = Announcer(now=lambda: now[0], sessions=lambda: sessions)
    assert due(restarted) == ["Two sessions need you: career and heardback."]


@pytest.mark.parametrize("contents", ["", "not json", "[]"])
def test_corrupt_or_empty_pending_file_is_ignored(tmp_path, monkeypatch, contents):
    monkeypatch.setattr(announce.store, "state_dir", lambda **_: tmp_path)
    (tmp_path / "pending-announcements.json").write_text(contents)
    assert Announcer(sessions=lambda: []).pending == {}


def test_stale_pending_announcement_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(announce.store, "state_dir", lambda **_: tmp_path)
    event = {"session_id": "a", "to": "idle", "hook": "Stop", "detail": "done"}
    (tmp_path / "pending-announcements.json").write_text(
        json.dumps({"a": ["finished", event, T0 - 601]}))
    assert Announcer(now=lambda: T0, sessions=lambda: []).pending == {}


def test_not_away_drops():
    pytest.importorskip("numpy")
    w = World(sess("a", "heardback", "idle", waiting_for=PERM))
    w.away = False
    w.feed(0, "a", "working", "waiting", "Notification")
    assert w.at(5) == []
    w.away = True
    assert w.at(10) == [] and w.ann.pending == {}


def test_muted_drops():
    w = World(sess("a", "heardback", "idle", waiting_for=PERM))
    w.muted = True
    w.feed(0, "a", "working", "waiting", "Notification")
    assert w.at(5) == [] and w.ann.pending == {}


def test_waiting_single():
    w = World(sess("a", "career", "idle", waiting_for=PERM))
    w.feed(0, "a", "working", "waiting", "Notification")
    assert w.at(5) == ["Career needs your permission for Bash."]


def test_batch_two_waiting():
    w = World(sess("a", "career", "idle"), sess("b", "heardback", "idle"))
    w.feed(0, "a", "working", "waiting", "Notification")
    w.feed(2, "b", "working", "waiting", "Notification")
    assert w.at(4) == []
    assert w.at(5) == ["Two sessions need you: career and heardback."]


def test_batch_three_and_finished():
    w = World(sess("a", "career", "idle"), sess("b", "heardback", "idle"), sess("c", "lc", "idle"),
              sess("d", "blog", "working"))
    for i, sid in enumerate("abc"):
        w.feed(i, sid, "working", "waiting", "Notification")
    w.feed(3, "d", "working", "idle", "Stop", turn_s=60, detail="done")
    assert w.at(5) == ["Three sessions need you: career, heardback and lc. Blog finished: done."]


def test_debounce_60s():
    w = World(sess("a", "heardback", "working"))
    w.feed(0, "a", "working", "idle", "Stop", turn_s=40, detail="one")
    assert w.at(5) == ["Heardback finished: one."]
    w.feed(6, "a", "idle", "working", "UserPromptSubmit")
    w.feed(15, "a", "working", "idle", "Stop", turn_s=40, detail="two")
    assert w.at(20) == [] and w.at(64) == []
    assert w.at(65) == ["Heardback finished: two."]


def test_debounce_hold_skips_session_reads():
    w = World(sess("a", "heardback", "working"))
    reads = []
    w.ann.sessions = lambda: reads.append(1) or list(w.s.values())
    w.feed(0, "a", "working", "idle", "Stop", turn_s=40, detail="one")
    w.at(5)
    w.feed(6, "a", "idle", "working", "UserPromptSubmit")
    w.feed(15, "a", "working", "idle", "Stop", turn_s=40, detail="two")
    for dt in range(20, 65):
        w.at(dt)
    assert len(reads) == 2  # format and delivery validate once; no re-read during debounce


def test_newer_replaces_pending():
    w = World(sess("a", "heardback", "working", waiting_for=PERM))
    w.feed(0, "a", "working", "waiting", "Notification")
    w.feed(1, "a", "waiting", "working", "PostToolUse")
    w.feed(3, "a", "working", "idle", "Stop", turn_s=35, detail="done")
    assert w.at(5) == []  # the newer event starts its own batch window
    assert w.at(8) == ["Heardback finished: done."]


def test_busy_holds_then_rechecks():
    w = World(sess("a", "heardback", "working"))
    w.feed(0, "a", "working", "idle", "Stop", turn_s=40, detail="done")
    w.busy = True
    assert w.at(5) == []
    w.s["a"]["state"] = "working"  # its feed event is still on its way
    w.busy = False
    assert w.at(6) == [] and w.ann.pending == {}


def test_reminder_once():
    w = World(sess("a", "heardback", "idle", waiting_for=PERM))
    w.feed(0, "a", "working", "waiting", "Notification")
    assert w.at(5) == ["Heardback needs your permission for Bash."]
    assert w.at(604) == []
    assert w.at(605) == ["Reminder: Heardback needs your permission for Bash."]
    assert w.at(1205) == [] and w.at(5000) == []


def test_permission_reminder_stays_silent_after_dialog_disappears():
    session = sess("a", "heardback", "waiting", waiting_for=PERM, tmux_pane="%1")
    w = World(session)
    w.ann.question = lambda _: ("Run Bash? 1 yes, 2 no", {"pane": "%1", "no": "2", "choices": ["1", "2"]})
    w.feed(0, "a", "working", "waiting", "Notification")
    assert w.at(5) == ["Heardback asks: Run Bash? 1 yes, 2 no."]
    w.ann.question = lambda _: None
    assert w.at(605) == []


def test_reminder_cancelled_when_answered():
    w = World(sess("a", "heardback", "idle", waiting_for=PERM))
    w.feed(0, "a", "working", "waiting", "Notification")
    assert w.at(5) == ["Heardback needs your permission for Bash."]
    w.feed(100, "a", "waiting", "working", "PostToolUse")
    assert w.at(605) == [] and w.ann.remind == {}


def test_garbage_event_ignored():
    w = World(sess("a", "heardback", "idle"))
    w.ann.feed({"to": "waiting"})
    w.ann.feed({"session_id": 5, "to": "waiting"})
    assert w.at(10) == [] and w.ann.pending == {}


# Task 17: tail + loop

def test_tail_from_end(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_text('{"n": 1}\n')
    t = Tail(p)
    assert t.read_new() == []
    with p.open("a") as f:
        f.write('{"n": 2}\nnot json\n[1]\n{"n": 3}\n')
    assert t.read_new() == [{"n": 2}, {"n": 3}]
    assert t.read_new() == []


def test_tail_missing_then_created(tmp_path):
    p = tmp_path / "events.jsonl"
    t = Tail(p)
    assert t.read_new() == []
    p.write_text('{"n": 1}\n')
    assert t.read_new() == [{"n": 1}]


def test_tail_partial_line(tmp_path):
    p = tmp_path / "events.jsonl"
    p.touch()
    t = Tail(p)
    with p.open("a") as f:
        f.write('{"n": ')
    assert t.read_new() == []
    with p.open("a") as f:
        f.write('1}\n{"n"')
    assert t.read_new() == [{"n": 1}]


def test_tail_rotation(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_text('{"old": 0}\n' * 50)
    t = Tail(p)
    with p.open("a") as f:
        f.write('{"n": 1}\n')
    assert t.read_new() == [{"n": 1}]
    os.replace(p, tmp_path / "events.jsonl.1")
    assert t.read_new() == []
    p.write_text('{"n": 2}\n')
    assert t.read_new() == [{"n": 2}]


def test_run_says(tmp_path, monkeypatch):
    p = tmp_path / "events.jsonl"
    p.touch()
    tail = Tail(p)
    p.write_text(json.dumps({"session_id": "a", "from": "working", "to": "waiting", "hook": "Notification"}) + "\n")
    t = [T0]
    ann = Announcer(now=lambda: t[0], sessions=lambda: [sess("a", "heardback", "waiting", waiting_for=PERM)])
    monkeypatch.setattr(announce.store, "gc", lambda: {})
    said = []

    async def say(s):
        said.append(s["text"])

    async def main():
        task = asyncio.create_task(announce.run(ann, say, tail))
        await asyncio.sleep(0.05)  # first pass feeds the event
        t[0] += announce.BATCH_S
        await asyncio.sleep(announce.POLL_S + 0.1)
        task.cancel()

    asyncio.run(main())
    assert said == ["Heardback needs your permission for Bash."]


def test_run_survives_errors(capsys, monkeypatch):
    monkeypatch.setattr(announce, "POLL_S", 0.01)
    calls = []

    class BadTail:
        def read_new(self):
            calls.append(1)
            raise RuntimeError("boom")

    monkeypatch.setattr(announce.store, "gc", lambda: {})

    async def main():
        task = asyncio.create_task(announce.run(Announcer(sessions=lambda: []), lambda s: None, BadTail()))
        await asyncio.sleep(0.1)
        task.cancel()

    asyncio.run(main())
    assert len(calls) > 1 and "boom" in capsys.readouterr().err


def test_waiting_reads_the_dialog_and_arms_ask():   # sessionhelp 2
    w = World(sess("a", "career", "idle", waiting_for=PERM, tmux_pane="%3"))
    ask = {"pane": "%3", "no": "3", "choices": ["1", "2", "3"]}
    w.ann.question = lambda s: ("run x? 1 yes, 2 yes and don't ask again, 3 no", ask)
    w.feed(0, "a", "working", "waiting", "Notification")
    assert w.at(5) == ["Career asks: run x? 1 yes, 2 yes and don't ask again, 3 no."] and w.ann.ask == ask
    assert w.at(6) == [] and w.ann.ask is None


def test_pane_question_from_real_captures(monkeypatch):
    from pathlib import Path
    from jv import tmux
    fix = Path(__file__).parent / "fixtures" / "tmux"
    monkeypatch.setattr(tmux, "tmux", lambda *a, **k: (fix / "permission_dialog_touch.txt").read_text())
    line, ask = announce.pane_question({"tmux_pane": "%3"})
    assert ask == {"pane": "%3", "no": "4", "choices": ["1", "2", "3", "4"]} and line.endswith("4 no")
    monkeypatch.setattr(tmux, "tmux", lambda *a, **k: (fix / "trust_dialog.txt").read_text())
    assert announce.pane_question({"tmux_pane": "%3"}) is None   # trust screens are never answered by voice
    assert announce.pane_question({"tmux_pane": None}) is None
