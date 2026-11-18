"""One policy across confirmations, follow-ups, mute and brain restarts."""
import asyncio
import contextlib
import json
import stat
import subprocess
import time

import pytest

from event_voice import Voice
from heardback.brain.brain import Awaiting, Brain, Turn
from heardback.brain.events import speak_policy
from heardback.coordinator import Coordinator
from heardback.status import away, store, watch
from heardback.status.announce import Announcer, BATCH_S
from heardback.tasks import personal


@pytest.mark.parametrize("kind", ["done", "answer", "session", "back", "reminder"])
@pytest.mark.parametrize("muted,busy,connected,awaiting", [
    (False, False, True, False), (False, True, True, False),
    (False, False, False, False), (False, False, True, True),
    (True, False, True, False), (True, True, True, True)])
def test_policy(kind, muted, busy, connected, awaiting):
    expected = "hold" if busy or awaiting or not connected else "now"
    if muted and kind != "answer":
        expected = "hold" if kind == "reminder" else "drop"
    assert speak_policy({"kind": kind}, dict(muted=muted, busy=busy, connected=connected), awaiting) == expected


@pytest.fixture
def rig(monkeypatch):
    original = subprocess.run
    def run(argv, *a, **kw):
        if argv[0] == "wpctl":
            assert argv[1:] == ["get-volume", "@DEFAULT_AUDIO_SINK@"]
            return subprocess.CompletedProcess(argv, 0, "Volume: 0.50\n", "")
        return original(argv, *a, **kw)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("HEARDBACK_TURN", "event-turn")
    away.flag_path().touch()
    b = Brain()
    b.voice = Voice(b)
    return b, Coordinator(b)


def says(b):
    return [m["text"] for m in b.voice.messages if m["type"] == "say"]


def test_due_reminder_waits_for_readback_answer(rig):
    b, c = rig
    row = personal.add_reminder(request_key="due", text="Call mum", due_at=int(time.time()))
    async def run():
        asking = asyncio.create_task(b.ask(b.voice, "Close this tab?", True))
        while b.voice not in b.awaiting:
            await asyncio.sleep(0)
        await c.tick()
        assert says(b) == [] and personal.get_reminder(row["reminder_id"])["state"] == "pending"
        q = b.awaiting[b.voice]
        await b.handle({"type": "confirm_reply", "confirmation_id": q.id, "ok": True,
                        "speaker_ok": True, "text": "yes"}, b.voice)
        assert await asking == (True, "yes")
        await b.say(b.voice, "Closed it.", final=True)
        await b.handle({"type": "state", "busy": False}, b.voice)
        await b.flush_events()
        assert says(b) == ["Closed it.", "Reminder. Call mum."]
        assert personal.get_reminder(row["reminder_id"])["state"] == "spoken"
    asyncio.run(run())


def test_idle_state_does_not_block_delivery_ack_on_voice_socket(rig, runtime_dir):
    b, _ = rig
    b.voice, b.voice_token = None, "event-token"
    row = personal.add_reminder(request_key="socket", text="Call mum", due_at=int(time.time()))
    b.events.add({"kind": "reminder", "id": row["reminder_id"]})
    async def run():
        from pathlib import Path
        path = Path(runtime_dir) / "events.sock"
        server = asyncio.create_task(b.serve(path))
        writer = None
        try:
            while not path.exists():
                await asyncio.sleep(0)
            reader, writer = await asyncio.open_unix_connection(str(path))
            writer.write(b'{"type":"hello","role":"voice","token":"event-token"}\n'
                         b'{"type":"state","busy":false}\n')
            await writer.drain()
            msg = json.loads(await reader.readline())
            assert msg["text"] == "Reminder. Call mum."
            writer.write((json.dumps({"type": "delivery", "id": msg["delivery_id"], "result": "spoken"}) + "\n").encode())
            await writer.drain()
            while personal.get_reminder(row["reminder_id"])["state"] != "spoken":
                await asyncio.sleep(0)
        finally:
            if writer:
                writer.close()
                await writer.wait_closed()
            server.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server
            for task in list(b._tasks):
                task.cancel()
            await asyncio.gather(*list(b._tasks), return_exceptions=True)
    asyncio.run(asyncio.wait_for(run(), 2))


def test_session_done_waits_for_model_followup(rig):
    b, _ = rig
    now = [time.time()]
    s = dict(session_id="s", state="idle", project="build", tmux_pane=None)
    ann = Announcer(now=lambda: now[0], sessions=lambda: [s])
    b.announcer = ann
    async def run():
        turn = Turn(b.voice, "choose", tid="turn")
        q = Awaiting(asyncio.get_running_loop().create_future(), True, "question", "Which one?",
                     time.monotonic() + 30, turn, kind="question", source="model")
        b._set_awaiting(b.voice, q)
        assert b._model_waiting()
        ann.feed(dict(session_id="s", to="idle", hook="Stop", turn_s=60,
                      **{"from": "working", "detail": "All fixed"}))
        now[0] += BATCH_S
        for event in ann.due():
            await b.announce(event)
        assert says(b) == []
        await b.handle({"type": "utterance", "text": "thanks", "speaker_ok": True,
                        "parent": "turn"}, b.voice)
        await b.handle({"type": "state", "busy": False}, b.voice)
        await b.flush_events()
        assert says(b) == ["Build finished: All fixed."]
        await b.flush_events()
        assert len(says(b)) == 1
    asyncio.run(run())


def test_held_watch_answer_and_done_survive_restart_once(rig):
    b, _ = rig
    s = dict(session_id="s", state="idle", project="build", tmux_pane="%3", tmux_target="0:3.0")
    s2 = dict(session_id="s2", state="idle", project="review", tmux_pane="%4", tmux_target="0:4.0")
    ann = Announcer(sessions=lambda: [s, s2])
    b.announcer = ann
    async def run():
        await b.handle({"type": "state", "busy": True}, b.voice)
        assert watch.arm("%3", session_id="s", now=time.time())
        ann.feed(dict(session_id="s", hook="Stop", to="idle", t=store.now_iso(), detail="All fixed"))
        assert watch.arm("%4", kind="status", session_id="s2", now=time.time())
        ann.feed(dict(session_id="s2", hook="Stop", to="idle", t=store.now_iso(), detail="All fixed."))
        for event in ann.due():
            await b.announce(event)
        assert says(b) == [] and len(b.events.rows) == 2
        assert stat.S_IMODE(b.events.path.stat().st_mode) == 0o600
        restarted = Brain(announcer=Announcer(sessions=lambda: [s]))
        restarted.voice = Voice(restarted)
        await restarted.flush_events()
        await restarted.flush_events()
        assert says(restarted) == ["Window 3 is done: All fixed.", "Window 4 says: All fixed."]
        again = Brain()
        again.voice = Voice(again)
        await again.flush_events()
        assert says(again) == [] and json.loads(again.events.path.read_text()) == []
    asyncio.run(run())


def test_mute_drops_done_keeps_answer_and_holds_reminder(rig):
    b, c = rig
    row = personal.add_reminder(request_key="mute", text="Call mum", due_at=int(time.time()))
    away.mute(None, time.time())
    async def run():
        await b.announce("Build is done.")
        await c.tick()
        await b.announce({"kind": "answer", "text": "Build says: All fixed."})
        assert says(b) == ["Build says: All fixed."]
        assert b.events.rows == [{"kind": "reminder", "id": row["reminder_id"]}]
        away.unmute()
        await c.tick()
        await c.tick()
        assert says(b) == ["Build says: All fixed.", "Reminder. Call mum."]
    asyncio.run(run())


@pytest.mark.parametrize("bad", [None, {"kind": []}, {"kind": "reminder", "id": "bad"},
    {"kind": "session", "text": "bad", "states": []},
    {"kind": "done", "text": "bad", "ask": {"pane": "%3", "no": "2", "choices": [{}]}}])
def test_malformed_saved_event_does_not_silence_valid_answer(rig, bad):
    b, _ = rig
    b.events.path.write_text(json.dumps([bad, {"kind": "answer", "text": "All fixed."}]))
    restarted = Brain()
    restarted.voice = Voice(restarted)
    asyncio.run(restarted.flush_events())
    assert says(restarted) == ["All fixed."]


@pytest.mark.parametrize("change", ["edit", "cancel", "postpone"])
def test_held_queue_rechecks_dashboard_changes_after_restart(rig, change):
    b, c = rig
    row = personal.add_reminder(request_key="edit", text="Old text", due_at=int(time.time()))
    async def run():
        b.voice_busy = True
        await c.tick()
        # The dashboard uses this same revision-checked store API.
        if change == "cancel":
            personal.cancel_reminder(row["reminder_id"], expected_revision=row["revision"])
        else:
            personal.edit_reminder(row["reminder_id"], expected_revision=row["revision"],
                                   text="New text", due_at=row["due_at"] + (3600 if change == "postpone" else 0))
        restarted = Brain()
        restarted.voice = Voice(restarted)
        c2 = Coordinator(restarted)
        assert await c2.startup()
        await c2.tick()
        assert says(restarted) == (["Reminder. New text."] if change == "edit" else [])
        assert personal.get_reminder(row["reminder_id"])["state"] == {
            "edit": "spoken", "cancel": "cancelled", "postpone": "pending"}[change]
    asyncio.run(run())
