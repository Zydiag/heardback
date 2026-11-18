"""Reminder delivery never uses the bounded general announcement queue."""
import asyncio
from pathlib import Path
import os
import time
import sys

import pytest

np = pytest.importorskip("numpy")

from heardback import coordinator
from heardback.brain.brain import Brain
from heardback.status.announce import Announcer
from heardback.status import away
from heardback.tasks import personal
from heardback.voice.tts import Tts as RealTts
from heardback.voice import tts as tts_module
from heardback.voice.config import Config
from heardback.voice.loop import ANNOUNCE_KEEP, Voice
from conftest import FakeCues, FakeMedia, FakeStt, energy_prob


class Tts(RealTts):
    def __init__(self):
        self.said = []
        self.audio = True
        super().__init__("fake", synth=lambda text: np.ones(240, np.float32))

    async def say(self, text):
        self.said.append(text)
        if self.audio:
            await super().say(text)


PLAYER = "import sys; sys.stdin.buffer.read()"


class Link:
    up = True

    def __init__(self, brain):
        self.brain = brain
        self.sent = []
        self.tasks = []
        self.incoming = asyncio.Queue()

    async def recv(self):
        return await self.incoming.get()

    def close(self):
        self.up = False

    def post(self, msg):
        self.sent.append(msg)
        self.tasks.append(asyncio.create_task(self.brain.handle(msg, self.brain.voice)))
        return True


class Socket:
    def __init__(self, voice):
        self.voice = voice
        self.sent = []
        self.tasks = []

    def is_closing(self):
        return False

    def write(self, data):
        import json
        msg = json.loads(data)
        self.sent.append(msg)
        if not self.tasks:
            self.tasks.append(asyncio.create_task(self.voice.run()))
        self.voice.link.incoming.put_nowait(msg)

    async def drain(self):
        pass


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert personal.database_path().is_relative_to(Path(os.environ["XDG_STATE_HOME"]))
    monkeypatch.setattr(away, "is_muted", lambda now: False)
    monkeypatch.setattr(coordinator, "volume_now", lambda: (50, False))
    monkeypatch.setattr(tts_module, "PW_PLAY", [sys.executable, "-c", PLAYER])
    b = Brain(announcer=Announcer())
    link = Link(b)
    v = Voice(Config(duck_media=False), link, stt=FakeStt([]), tts=Tts(),
              cues=FakeCues(), media=FakeMedia(), prob=energy_prob)
    b.voice = Socket(v)
    c = coordinator.Coordinator(b)
    yield b, c, v
    v.tts.pool.shutdown(wait=True)
    for pool in (v.gpu, v.cpu, v._io, v._dictation_io):
        pool.shutdown(wait=True)


@pytest.mark.parametrize("busy", ["LISTEN", "THINK", "SPEAK", "CONFIRM", "dictating", "automute"])
async def test_refused_then_idle_speaks_once_real_row(rig, busy):
    b, c, v = rig
    row = personal.add_reminder(request_key="one", text="Call mum", due_at=int(time.time()))
    if busy == "dictating":
        # Use the actual dictation slot's public start path, no hardware.
        v.dictation.start({"pane_id": "%999", "window_id": "@999"})
    elif busy == "automute":
        v.automute = True
    else:
        v.state = busy
    await c.tick()
    assert personal.get_reminder(row["reminder_id"])["state"] == "pending"
    assert v.tts.said == [] and list(v.pending) == []
    if busy == "dictating":
        v.dictation.cancel(v.dictation.metadata()["session_id"])
    v.automute, v.state = False, "IDLE"
    await c.tick()
    await c.tick()
    assert v.tts.said == ["Reminder. Call mum."]
    assert personal.get_reminder(row["reminder_id"])["state"] == "spoken"
    assert [m["result"] for m in v.link.sent if m["type"] == "delivery"] == ["refused", "spoken"]


async def test_spoken_reminder_is_traced(rig, monkeypatch):
    b, c, v = rig
    seen = []
    monkeypatch.setattr("heardback.voice.loop.trace.emit", lambda src, ev, tid, **f: seen.append((ev, f)))
    personal.add_reminder(request_key="one", text="Call mum", due_at=int(time.time()))
    await c.tick()
    await c.tick()
    assert ("say", {"text": "Reminder. Call mum.", "announce": True, "delivery": "spoken"}) in seen


@pytest.mark.parametrize("idle", [True, False])
async def test_delivery_never_enters_full_pending(rig, idle):
    b, c, v = rig
    row = personal.add_reminder(request_key="one", text="Call mum", due_at=int(time.time()))
    v.pending.extend(f"Other {n}." for n in range(ANNOUNCE_KEEP))
    if not idle:
        v.state = "LISTEN"
    await c.tick()
    assert all("Call mum" not in text for text in v.pending)
    assert personal.get_reminder(row["reminder_id"])["state"] == ("spoken" if idle else "pending")
    assert v.tts.said[:1] == (["Reminder. Call mum."] if idle else [])
    if v._ann_task:
        v._ann_task.cancel()


async def test_mic_off_delivery_keeps_wake_text_as_speech(rig):
    b, c, v = rig
    row = personal.add_reminder(request_key="one", text="Say hey claude", due_at=int(time.time()))
    v.mic_on = False
    await c.tick()
    assert v.tts.said == ["Reminder. Say hey claude."]
    assert personal.get_reminder(row["reminder_id"])["state"] == "spoken"
    assert not [m for m in v.link.sent if m["type"] in {"utterance", "confirm_reply"}]


async def test_idle_speaks_full_reminder(rig):
    b, c, v = rig
    row = personal.add_reminder(request_key="locked", text="Call mum", due_at=int(time.time()))
    await c.tick()
    assert v.tts.said == ["Reminder. Call mum."]
    assert personal.get_reminder(row["reminder_id"])["state"] == "spoken"


async def test_no_audio_backoff_then_one_spoken_delivery(rig, monkeypatch):
    b, c, v = rig
    now = int(time.time())
    row = personal.add_reminder(request_key="one", text="Call mum", due_at=now)
    v.tts.audio = False
    monkeypatch.setattr(coordinator.time, "time", lambda: now)
    await c.tick()
    assert personal.get_reminder(row["reminder_id"])["state"] == "pending"
    v.tts.audio = True
    monkeypatch.setattr(coordinator.time, "time", lambda: now + 59)
    await c.tick()
    assert v.tts.said == ["Reminder. Call mum."]
    assert personal.get_reminder(row["reminder_id"])["state"] == "pending"
    monkeypatch.setattr(coordinator.time, "time", lambda: now + 60)
    await c.tick()
    await c.tick()
    assert personal.get_reminder(row["reminder_id"])["state"] == "spoken"
    assert v.tts.said == ["Reminder. Call mum."] * 2
    assert [m["result"] for m in v.link.sent if m["type"] == "delivery"] == ["failed", "spoken"]


async def test_ordinary_say_queue_unchanged(rig):
    b, c, v = rig
    v.state = "LISTEN"
    for n in range(5):
        await b._send(b.voice, {"type": "say", "announce": True, "text": f"Other {n}."})
    await wait_until(lambda: list(v.pending) == ["Other 2.", "Other 3.", "Other 4."])
    assert list(v.pending) == ["Other 2.", "Other 3.", "Other 4."] and v.tts.said == []


@pytest.mark.parametrize('player,expected', [
    ('import sys; sys.exit(1)', 'pending'),
    ('import os,sys; os.close(0); sys.exit(1)', 'pending'),
    ('import os,signal; os.kill(os.getpid(),signal.SIGTERM)', 'pending'),
    (PLAYER, 'spoken'),
    (None, 'pending'),
])
async def test_real_tts_player_exit_controls_outcome(rig, monkeypatch, player, expected):
    b, c, v = rig
    monkeypatch.setattr(tts_module, 'PW_PLAY', ['/nonexistent/fixes11-player'] if player is None
                        else [sys.executable, '-c', player])
    row = personal.add_reminder(request_key='exit', text='Call mum', due_at=int(time.time()))
    await c.tick()
    assert personal.get_reminder(row['reminder_id'])['state'] == expected
    assert [m['result'] for m in v.link.sent if m['type'] == 'delivery'] == [
        'spoken' if expected == 'spoken' else 'failed']


async def wait_until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


async def test_barge_in_retains_started_audio_outcome(rig, monkeypatch):
    b, c, v = rig
    monkeypatch.setattr(tts_module, 'PW_PLAY', [sys.executable, '-c',
        'import sys,time; sys.stdin.buffer.read(); time.sleep(60)'])
    row = personal.add_reminder(request_key='barge', text='Call mum', due_at=int(time.time()))
    tick = asyncio.create_task(c.tick())
    await wait_until(lambda: v.tts.first_audio_at is not None)
    v.ptt()
    await tick
    assert v.tts.stopped and v.state == 'LISTEN'
    assert personal.get_reminder(row['reminder_id'])['state'] == 'spoken'


async def test_hung_player_reader_keeps_reading_and_shutdown_reaps(rig, monkeypatch):
    b, c, v = rig
    monkeypatch.setattr(tts_module, 'PW_PLAY', [sys.executable, '-c',
        'import sys,time; sys.stdin.buffer.read(); time.sleep(60)'])
    row = personal.add_reminder(request_key='hung', text='Call mum', due_at=int(time.time()))
    tick = asyncio.create_task(c.tick())
    await wait_until(lambda: v.tts.first_audio_at is not None)
    proc = v.tts._proc
    other = personal.add_reminder(request_key='second', text='Call dad', due_at=int(time.time()))
    await b._send(b.voice, {'type': 'say', 'text': 'Other brain message.', 'announce': True})
    await b._send(b.voice, {'type': 'say', 'text': 'Second reminder.', 'delivery_id': 'second'})
    await wait_until(lambda: any(m.get('id') == 'second' for m in v.link.sent))
    assert list(v.pending) == ['Other brain message.']
    assert [m['result'] for m in v.link.sent if m.get('id') == 'second'] == ['refused']
    assert personal.get_reminder(other['reminder_id'])['state'] == 'pending'
    assert v.tts.said == ['Reminder. Call mum.']
    reader = b.voice.tasks[0]
    reader.cancel()
    await asyncio.gather(reader, return_exceptions=True)
    assert v._delivery_task.done() and proc.returncode is not None
    tick.cancel()
    await asyncio.gather(tick, return_exceptions=True)
    assert personal.get_reminder(row['reminder_id'])['state'] == 'delivering'


@pytest.mark.parametrize('player', [PLAYER, 'import sys; sys.exit(1)'])
async def test_ordinary_speech_keeps_nonraising_player_exit_policy(rig, monkeypatch, player):
    _, _, v = rig
    monkeypatch.setattr(tts_module, 'PW_PLAY', [sys.executable, '-c', player])
    await v.tts.say('Ordinary speech.')
    await v.tts.finish()
    assert v.tts.player_returncode == (0 if player == PLAYER else 1)
    assert not v.tts.speaking


async def test_barge_before_audio_is_failed(rig, monkeypatch):
    b, c, v = rig
    import threading
    entered, release = threading.Event(), threading.Event()
    def synth(text):
        entered.set()
        release.wait(3)
        return np.ones(240, np.float32)
    v.tts.synth = synth
    row = personal.add_reminder(request_key='noaudio', text='Call mum', due_at=int(time.time()))
    tick = asyncio.create_task(c.tick())
    try:
        await wait_until(entered.is_set)
        v.ptt()
        await tick
        assert v.state == 'LISTEN'
        assert personal.get_reminder(row['reminder_id'])['state'] == 'pending'
        assert [m['result'] for m in v.link.sent if m['type'] == 'delivery'] == ['failed']
    finally:
        release.set()
