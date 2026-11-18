"""Task 10 acceptance across isolated ledger restarts; no live voice or Claude."""
import asyncio
import gc
import json
import weakref
from datetime import datetime

import pytest

from heardback.brain import catalogue
from heardback.brain.brain import Brain
from heardback.coordinator import Coordinator
from heardback.status.announce import Announcer
from heardback.status import away
from heardback.tasks import personal
from jv import cli, reminder


@pytest.fixture
def isolated(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    now = [2_000_000_000.0]
    monkeypatch.setattr(reminder, "_now", lambda: datetime.fromtimestamp(now[0]).astimezone())
    from heardback import coordinator as coordinator_module
    monkeypatch.setattr(coordinator_module.time, "time", lambda: now[0])
    monkeypatch.setattr(coordinator_module.log, "disabled", True)
    monkeypatch.setattr(coordinator_module, "volume_now", lambda: (50, False))
    monkeypatch.setattr(away, "is_muted", lambda _now: False)

    def invoke(*argv):
        rc = cli.run([*argv, "--json"])
        out, _ = capsys.readouterr()
        return rc, json.loads(out)

    def fresh_tick(*, ticks=1):
        brain = Brain(claude_cmd=None, announcer=Announcer())
        voice = Voice(brain)
        brain.voice = voice
        coordinator = Coordinator(brain)

        async def run():
            assert await coordinator.startup()
            for _ in range(ticks):
                await coordinator.tick()

        asyncio.run(run())
        messages = voice.messages
        brain_ref, coordinator_ref = weakref.ref(brain), weakref.ref(coordinator)
        brain.coordinator = None
        coordinator.brain = None
        voice.brain = None
        del coordinator, voice, brain
        return messages, brain_ref, coordinator_ref

    return now, invoke, fresh_tick


class Voice:
    def __init__(self, brain):
        self.brain = brain
        self.messages = []
        self.closed = False

    def is_closing(self):
        return self.closed

    def write(self, data):
        message = json.loads(data)
        self.messages.append(message)
        if message.get("delivery_id"):
            asyncio.get_running_loop().create_task(self.brain.handle(
                {"type": "delivery", "id": message["delivery_id"], "result": "spoken"}, self))

    async def drain(self):
        pass


def _drop_restart_refs(refs):
    gc.collect()
    assert all(ref() is None for ref in refs)


def test_voice_reminder_survives_restart_and_is_spoken_once(isolated):
    now, invoke, fresh_tick = isolated
    rc, created = invoke("reminder", "add", "Call mum", "--when", "in 1 minute")
    assert rc == 0 and created["ok"]
    row = created["reminder"]
    assert row["state"] == "pending" and row["due_at"] == int(now[0]) + 60

    first_messages, *first_refs = fresh_tick()
    assert not [m for m in first_messages if m.get("delivery_id")]
    _drop_restart_refs(first_refs)

    now[0] += 60
    messages, *delivered_refs = fresh_tick(ticks=3)
    spoken = [m["text"] for m in messages if m.get("delivery_id")]
    assert spoken == ["Reminder. Call mum."]
    assert personal.get_reminder(row["reminder_id"])["state"] == "spoken"
    _drop_restart_refs(delivered_refs)

    messages, *_ = fresh_tick(ticks=3)
    assert not [m for m in messages if m.get("delivery_id")]
    assert personal.get_reminder(row["reminder_id"])["state"] == "spoken"


def test_cancelled_reminder_never_fires_after_restart(isolated):
    now, invoke, fresh_tick = isolated
    rc, created = invoke("reminder", "add", "Call mum", "--when", "in 1 minute")
    assert rc == 0 and created["ok"]
    row = created["reminder"]
    rc, confirmation = invoke("reminder", "cancel", "Call mum")
    assert rc == 3 and confirmation["source_revision"]
    rc, cancelled = invoke("reminder", "cancel", "Call mum", "--yes",
                           "--source-revision", confirmation["source_revision"])
    assert rc == 0 and cancelled["ok"]
    assert personal.get_reminder(row["reminder_id"])["state"] == "cancelled"

    first_messages, *first_refs = fresh_tick()
    assert not [m for m in first_messages if m.get("delivery_id")]
    _drop_restart_refs(first_refs)
    now[0] += 60
    messages, *_ = fresh_tick(ticks=3)
    assert not [m for m in messages if m.get("delivery_id")]
    assert personal.get_reminder(row["reminder_id"])["state"] == "cancelled"


def test_catchup_routes_and_runs_with_claude_off(isolated, monkeypatch):
    now, _, _ = isolated
    row = personal.add_reminder(request_key="catchup", text="Call mum", due_at=int(now[0]) - 1)
    brain = Brain(claude_cmd=None, announcer=Announcer())
    voice = Voice(brain)
    brain.voice = voice
    real_catalogue = cli.catalogue(cli.all_verbs())
    monkeypatch.setattr(catalogue, "get", lambda: catalogue._index(real_catalogue))

    async def fail_if_called(*args, **kwargs):
        pytest.fail("catch me up attempted a Claude handoff")

    async def run_jv(argv, **kwargs):
        from contextlib import redirect_stdout
        from io import StringIO

        output = StringIO()
        with redirect_stdout(output):
            rc = cli.run(argv[1:])
        return rc, output.getvalue(), ""

    monkeypatch.setattr(brain, "to_claude", fail_if_called)
    monkeypatch.setattr(catalogue, "run_jv", run_jv)
    asyncio.run(brain.utterance("catch me up", True, voice))

    replies = [m["text"] for m in voice.messages if m.get("type") == "say"]
    assert brain.claude_cmd is None
    assert replies == ["1 overdue or held reminder."]
    assert personal.get_reminder(row["reminder_id"])["state"] == "pending"
