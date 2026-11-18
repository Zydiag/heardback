import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

np = pytest.importorskip("numpy")

from heardback.voice.audio_in import FRAME
from heardback.voice.config import Config
from heardback.voice.link import Link
from heardback.voice.loop import Voice


TARGET = {"target": "1:2.0", "pane_id": "%12", "pane_pid": "1024", "session_id": "$3",
          "started": "", "server": ("42", "1", "/tmp/tmux-100/default"), "requires_commit": False}


class Stt:
    def __init__(self):
        self.command_calls = 0
        self.dictation_calls = 0

    def transcribe(self, _audio):
        self.command_calls += 1
        return "voice command"

    def transcribe_dictation(self, _audio):
        self.dictation_calls += 1
        return "final literal words"


class Cues:
    def __init__(self):
        self.notes = []

    def notify(self, text, sticky=False):
        self.notes.append(text)

    def play(self, _name):
        pass


class Tts:
    first_audio_at = None

    async def say(self, _text):
        pass

    async def finish(self):
        pass

    def stop(self):
        pass


class Media:
    def pause(self, *_):
        pass

    def resume(self, *_):
        pass


class Vad:
    def __call__(self, _chunk):
        return 0.0

    def reset(self):
        pass


def make_voice(tmp_path):
    gpu, cpu = ThreadPoolExecutor(1), ThreadPoolExecutor(1)
    stt, cues = Stt(), Cues()
    voice = Voice(Config(duck_media=False), Link(tmp_path / "brain.sock"), stt=stt, tts=Tts(), cues=cues,
                  media=Media(), prob=Vad(), gpu=gpu, cpu=cpu)
    voice._mic_available = True
    voice.mic_available_probe = lambda: True
    return voice, stt, cues, gpu, cpu


async def close(voice, gpu, cpu):
    voice.dictation_shutdown()
    await asyncio.to_thread(voice._io.shutdown, wait=True, cancel_futures=True)
    await asyncio.to_thread(voice._dictation_io.shutdown, wait=True, cancel_futures=True)
    await asyncio.to_thread(gpu.shutdown, wait=True, cancel_futures=True)
    await asyncio.to_thread(cpu.shutdown, wait=True, cancel_futures=True)


async def until(predicate, timeout=2.0):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(poll(), timeout)


async def start_voice(voice, monkeypatch):
    from jv import tmux

    monkeypatch.setattr(tmux, "dictation_target", lambda selector: {**TARGET, "target": selector})
    return await voice.dictation_control({"action": "start", "target": "background pane"})


async def test_dictation_owns_audio_and_ptt_mic_loss_are_explicit(tmp_path, monkeypatch):
    voice, stt, cues, gpu, cpu = make_voice(tmp_path)
    try:
        started = await start_voice(voice, monkeypatch)
        sid = started["session_id"]
        assert started["phase"] == "listening"
        voice.feed(np.ones(FRAME, dtype=np.int16))
        captured = voice.dictation._session.samples
        assert captured == FRAME

        voice.ptt()
        assert voice.dictation.phase == "paused"
        voice.feed(np.ones(FRAME, dtype=np.int16))
        assert voice.dictation._session.samples == captured
        voice.ptt()  # status feedback; it must not begin a command turn
        await until(lambda: "Dictation is paused or ready for review" in cues.notes)
        voice.set_mic_available(False)
        assert voice.dictation.phase == "paused" and voice.dictation.metadata()["reason"] == "device_loss"
        voice.set_mic_available(True)
        assert voice.dictation.phase == "paused"  # device recovery never resumes capture by itself
        await voice.dictation_control({"action": "resume", "session_id": sid})
        voice.feed(np.ones(FRAME, dtype=np.int16))
        assert voice.dictation._session.samples == captured + FRAME
        assert stt.command_calls == 0
        voice.cancel()
        assert voice.dictation.phase == "cancelled" and not voice._dictation_mode_active
        assert stt.command_calls == stt.dictation_calls == 0
    finally:
        await close(voice, gpu, cpu)


async def test_pre_dispatch_cancel_invalidates_preparation_and_never_sends(tmp_path, monkeypatch):
    from jv import tmux

    voice, _stt, _cues, gpu, cpu = make_voice(tmp_path)
    entered, release, claims = threading.Event(), threading.Event(), []

    def slow_preparation(_target, _text, *, confirmed, claim):
        entered.set()
        release.wait(2)
        claims.append(claim())
        return {"status": "accepted" if claims[-1] else "cancelled", "accepted": claims[-1]}

    monkeypatch.setattr(tmux, "dictation_insert", slow_preparation)
    try:
        started = await start_voice(voice, monkeypatch)
        sid = started["session_id"]
        voice.feed(np.ones(FRAME, dtype=np.int16))
        await voice.dictation_control({"action": "finish", "session_id": sid})
        await asyncio.to_thread(entered.wait, 1)
        await until(lambda: voice.dictation.phase == "preparing")
        voice.cancel()
        assert voice.dictation.phase == "cancelled"
        release.set()
        await until(lambda: (sid, "finish") not in voice._dictation_tasks)
        assert claims == [False]
    finally:
        release.set()
        await close(voice, gpu, cpu)


async def test_mic_loss_during_preparation_invalidates_attempt_without_auto_resume(tmp_path, monkeypatch):
    from jv import tmux

    voice, _stt, _cues, gpu, cpu = make_voice(tmp_path)
    entered, release, claims = threading.Event(), threading.Event(), []

    def slow_preparation(_target, _text, *, confirmed, claim):
        entered.set()
        release.wait(2)
        claims.append(claim())
        return {"status": "cancelled" if not claims[-1] else "accepted"}

    monkeypatch.setattr(tmux, "dictation_insert", slow_preparation)
    try:
        started = await start_voice(voice, monkeypatch)
        sid = started["session_id"]
        voice.feed(np.ones(FRAME, dtype=np.int16))
        await voice.dictation_control({"action": "finish", "session_id": sid})
        await asyncio.to_thread(entered.wait, 1)
        await until(lambda: voice.dictation.phase == "preparing")
        voice.set_mic_available(False)
        assert voice.dictation.phase == "review" and voice.dictation.metadata()["reason"] == "device_loss"
        voice.set_mic_available(True)
        assert voice.dictation.phase == "review"
        release.set()
        await until(lambda: (sid, "finish") not in voice._dictation_tasks)
        assert claims == [False]
        assert voice.dictation.dashboard()["text"] == "final literal words"
    finally:
        release.set()
        await close(voice, gpu, cpu)


async def test_alternate_screen_commit_is_bound_to_final_revision(tmp_path, monkeypatch):
    from jv import tmux

    voice, _stt, _cues, gpu, cpu = make_voice(tmp_path)
    attempts = []

    def alternate_once(target, text, *, confirmed, claim):
        attempts.append((target, text, confirmed))
        if not confirmed:
            return {"status": "confirmation_required"}
        return {"status": "accepted" if claim() else "cancelled"}

    monkeypatch.setattr(tmux, "dictation_insert", alternate_once)
    try:
        started = await start_voice(voice, monkeypatch)
        sid = started["session_id"]
        voice.dictation._session.target["requires_commit"] = True
        voice.feed(np.ones(FRAME, dtype=np.int16))
        await voice.dictation_control({"action": "finish", "session_id": sid})
        await until(lambda: voice.dictation.phase == "awaiting_commit")
        revision = voice.dictation.metadata()["revision"]
        await voice.dictation_control({"action": "commit", "session_id": sid, "revision": revision})
        await until(lambda: voice.dictation.phase == "finished")
        assert [attempt[2] for attempt in attempts] == [False, True]
        assert attempts[0][0] == attempts[1][0] == voice.dictation._session.target
        assert attempts[0][1] == attempts[1][1] == "final literal words"
        assert voice.dictation.metadata()["revision"] == revision
        assert voice.dictation.dashboard()["text"] == ""
    finally:
        await close(voice, gpu, cpu)


async def test_escape_after_dispatch_is_consumed_and_reports_possible_paste(tmp_path, monkeypatch):
    from jv import tmux

    voice, _stt, cues, gpu, cpu = make_voice(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def dispatched(_target, _text, *, confirmed, claim):
        assert claim() is True
        entered.set()
        release.wait(2)
        return {"status": "accepted"}

    monkeypatch.setattr(tmux, "dictation_insert", dispatched)
    try:
        started = await start_voice(voice, monkeypatch)
        sid = started["session_id"]
        voice.feed(np.ones(FRAME, dtype=np.int16))
        await voice.dictation_control({"action": "finish", "session_id": sid})
        await asyncio.to_thread(entered.wait, 1)
        await until(lambda: voice.dictation.phase == "inserting")
        voice.cancel()  # SIGURG/Escape: the paste claim cannot be undone
        voice.ptt()
        await until(lambda: any("paste may still complete" in note for note in cues.notes))
        voice.set_mic_available(False)
        assert voice.dictation.phase == "inserting"
        release.set()
        await until(lambda: voice.dictation.phase == "finished")
        assert voice.dictation.metadata()["commit_started"] is True
        assert voice.dictation.dashboard()["text"] == ""
    finally:
        release.set()
        await close(voice, gpu, cpu)


async def test_one_start_check_across_sessions_and_a_local_line(tmp_path, monkeypatch):
    """E6b: tmux start, cancel, then the shared start check refuses during a local line and passes after it."""
    from heardback.voice.dictation import DictationError
    voice, stt, cues, gpu, cpu = make_voice(tmp_path)
    try:
        first = await start_voice(voice, monkeypatch)
        with pytest.raises(DictationError) as again:
            await start_voice(voice, monkeypatch)
        assert again.value.code == "session_active"
        await voice.dictation_control({"action": "cancel", "session_id": first["session_id"]})
        assert not voice._dictation_mode_active and voice.state == "IDLE"
        gate = asyncio.Event()
        voice.tts.finish = gate.wait          # a local line still playing
        voice._speak_local("Running on CPU.")
        await until(lambda: voice.state == "SPEAK")
        with pytest.raises(DictationError) as busy:
            await start_voice(voice, monkeypatch)
        assert busy.value.code == "voice_busy"
        gate.set()
        await until(lambda: voice.state == "IDLE")
        second = await start_voice(voice, monkeypatch)
        assert second["phase"] == "listening" and second["session_id"] != first["session_id"]
    finally:
        await close(voice, gpu, cpu)
