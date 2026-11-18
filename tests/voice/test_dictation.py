import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

np = pytest.importorskip("numpy")

from heardback.voice import dictation as dictation_mod
from heardback.voice.audio_in import FRAME
from heardback.voice.dictation import Dictation, DictationError


TARGET = {"target": "1:0.0", "pane_id": "%12", "server": ("42", "1", "/tmp/tmux-100/default")}


class ScriptedStt:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.block_at = set()

    def transcribe_dictation(self, audio):
        self.calls.append(audio.copy())
        index = len(self.calls)
        self.started.set()
        if index in self.block_at:
            self.release.wait(3)
        return self.results[index - 1] if index <= len(self.results) else ""


class Vad:
    def __init__(self, probability=1.0):
        self.probability = probability
        self.resets = 0

    def __call__(self, _chunk):
        return self.probability

    def reset(self):
        self.resets += 1


async def until(predicate, timeout=1.0):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(poll(), timeout)


def frame(samples=FRAME):
    return np.ones(samples, dtype=np.int16)


async def close(dictation, stt, pool):
    dictation.shutdown()
    stt.release.set()
    await asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=True)


async def test_preview_uses_accumulated_audio_and_final_text_is_literal():
    stt, vad, pool = ScriptedStt("you uh hey Claude", "press enter"), Vad(), ThreadPoolExecutor(1)
    events = []
    d = Dictation(stt, pool, vad, event=lambda **event: events.append(event))
    try:
        sid = d.start(TARGET)["session_id"]
        d._session.last_preview_at -= dictation_mod.PREVIEW_INTERVAL_S + 1
        assert d.feed(frame()) is True
        await until(lambda: d.metadata()["chars"] == len("you uh hey Claude"))
        assert d.dashboard()["text"] == "you uh hey Claude"
        assert d.metadata()["phase"] == "listening"
        assert len(stt.calls) == 1 and len(stt.calls[0]) == FRAME

        result = await d.finish(sid)
        assert result == {"text": "press enter", "revision": 2, "target": TARGET}
        assert d.metadata()["phase"] == "review" and d.metadata()["final"] is True
        assert d.commit_payload(sid, 2) == {"target": TARGET, "text": "press enter"}
        assert d.dashboard()["provisional"] is False
        assert all("text" not in event and "preview" not in event for event in events)
        with pytest.raises(DictationError, match="stale_revision"):
            d.begin_commit(sid, 1, explicit=False)
    finally:
        await close(d, stt, pool)


async def test_silence_short_audio_pause_resume_and_stale_session():
    stt, vad, pool = ScriptedStt("thanks"), Vad(0.0), ThreadPoolExecutor(1)
    d = Dictation(stt, pool, vad)
    try:
        sid = d.start(TARGET)["session_id"]
        assert d.feed(frame(73)) is True   # shorter than one VAD block; audio still belongs to dictation
        before = d._session.samples
        d.pause(sid)
        assert d.feed(frame()) is True and d._session.samples == before
        d.resume(sid)
        assert d.feed(frame(37)) is True
        assert d._session.samples == before + 37
        assert vad.resets >= 3
        assert await d.finish(sid) == {"text": "thanks", "revision": 1, "target": TARGET}
        with pytest.raises(DictationError, match="stale_session"):
            d.pause("0" * 32)
    finally:
        await close(d, stt, pool)


async def test_empty_silence_cannot_be_finalized_or_committed():
    stt, vad, pool = ScriptedStt("should not run"), Vad(0.0), ThreadPoolExecutor(1)
    d = Dictation(stt, pool, vad)
    try:
        sid = d.start(TARGET)["session_id"]
        result = await d.finish(sid)
        assert result == {"error": "empty_audio"}
        assert d.metadata()["phase"] == "review" and d.metadata()["reason"] == "empty_audio"
        assert d.metadata()["final"] is False and stt.calls == []
        with pytest.raises(DictationError, match="stale_revision"):
            d.commit_payload(sid, 1)
    finally:
        await close(d, stt, pool)


async def test_audio_text_and_wall_limits_leave_review_without_truncating_text(monkeypatch):
    monkeypatch.setattr(dictation_mod, "MAX_SAMPLES", 100)
    monkeypatch.setattr(dictation_mod, "MAX_TEXT_CHARS", 5)
    stt, vad, pool = ScriptedStt("123456"), Vad(), ThreadPoolExecutor(1)
    d = Dictation(stt, pool, vad)
    try:
        sid = d.start(TARGET)["session_id"]
        assert d.feed(frame(120)) is True
        assert d.metadata()["phase"] == "review" and d.metadata()["reason"] == "audio_limit"
        assert d._session.samples == 100
        # Finishing a limit state still transcribes the bounded captured audio, but a too-long
        # result remains a review error and is never made committable.
        result = await d.finish(sid)
        assert result == {"error": "text_limit"}
        assert d.metadata()["phase"] == "review" and d.metadata()["reason"] == "text_limit"
        assert d.metadata()["final"] is False

        d.cancel(sid)
        sid = d.start(TARGET)["session_id"]
        d.pause(sid)
        d._wall_limit(sid)
        assert d.metadata()["phase"] == "review" and d.metadata()["reason"] == "duration_limit"
        with pytest.raises(DictationError, match="not_paused"):
            d.resume(sid)
    finally:
        await close(d, stt, pool)


async def test_preview_timeout_keeps_job_ownership_until_hung_inference_returns():
    stt, vad, pool = ScriptedStt("too late"), Vad(), ThreadPoolExecutor(1)
    stt.block_at.add(1)
    events = []
    d = Dictation(stt, pool, vad, timeout_s=0.02, event=lambda **event: events.append(event))
    try:
        sid = d.start(TARGET)["session_id"]
        d._session.last_preview_at -= dictation_mod.PREVIEW_INTERVAL_S + 1
        d.feed(frame())
        await asyncio.to_thread(stt.started.wait, 1)
        await until(lambda: d.metadata()["reason"] == "preview_timeout")
        assert d.metadata()["chars"] == 0
        d.cancel(sid)
        with pytest.raises(DictationError, match="inference_busy"):
            d.start(TARGET)
        assert len(stt.calls) == 1   # no second job is queued behind the timed-out worker
        stt.release.set()
        await until(lambda: d._job is None)
        await asyncio.sleep(0)
        assert d.metadata()["phase"] == "cancelled" and d.metadata()["chars"] == 0
        assert not any(event["phase"] == "listening" and event["chars"] for event in events)
        d.start(TARGET)
    finally:
        await close(d, stt, pool)


async def test_final_timeout_and_late_final_after_cancel_never_replace_preview():
    stt, vad, pool = ScriptedStt("kept preview", "late final", "preview two", "late final two"), Vad(), ThreadPoolExecutor(1)
    stt.block_at.add(2)
    events = []
    d = Dictation(stt, pool, vad, timeout_s=0.02, event=lambda **event: events.append(event))
    try:
        sid = d.start(TARGET)["session_id"]
        d._session.last_preview_at -= dictation_mod.PREVIEW_INTERVAL_S + 1
        d.feed(frame())
        await until(lambda: d.metadata()["chars"] == len("kept preview"))
        result = await d.finish(sid)
        assert result == {"error": "final_timeout"}
        assert d.dashboard()["text"] == "kept preview" and d.metadata()["phase"] == "review"
        assert len(stt.calls) == 2
        stt.release.set()
        await until(lambda: d._job is None)
        assert d.dashboard()["text"] == "kept preview"
        assert not any(e["phase"] == "review" and e["reason"] == "finalized" for e in events)

        d.cancel(sid)
        sid = d.start(TARGET)["session_id"]
        stt.block_at.discard(2)
        stt.started.clear()
        stt.release.clear()
        stt.block_at.add(4)
        d._session.last_preview_at -= dictation_mod.PREVIEW_INTERVAL_S + 1
        d.feed(frame())
        await until(lambda: d.metadata()["chars"] == len("preview two"))
        stt.started.clear()
        stt.release.clear()
        finishing = asyncio.create_task(d.finish(sid))
        await asyncio.to_thread(stt.started.wait, 1)
        await until(lambda: d.metadata()["phase"] == "transcribing")
        d.cancel(sid)
        stt.release.set()
        with pytest.raises(DictationError, match="stale_session"):
            await finishing
        assert d.metadata()["phase"] == "cancelled" and d.metadata()["chars"] == 0
    finally:
        await close(d, stt, pool)


async def _finalized():
    stt, vad, pool = ScriptedStt("literal words"), Vad(), ThreadPoolExecutor(1)
    d = Dictation(stt, pool, vad)
    sid = d.start(TARGET)["session_id"]
    d.feed(frame())
    result = await d.finish(sid)
    return d, stt, pool, sid, result


async def test_commit_claim_is_one_shot_but_preparation_failure_can_retry():
    d, stt, pool, sid, result = await _finalized()
    try:
        revision = result["revision"]
        first = d.begin_commit(sid, revision, explicit=False)["attempt"]
        assert d.attempt_failed(sid, revision, "insert_refused", first)["reason"] == "insert_refused"
        assert d.dashboard()["text"] == "literal words"
        assert d.claim_commit(sid, revision, first) is False
        second = d.begin_commit(sid, revision, explicit=False)["attempt"]
        assert d.claim_commit(sid, revision, second) is True
        assert d.claim_commit(sid, revision, second) is False
        with pytest.raises(DictationError, match="insertion_in_progress"):
            d.cancel(sid)
        assert d.complete_commit(sid, revision, second, accepted=False, reason="insert_uncertain")["reason"] == "insert_uncertain"
        assert d.claim_commit(sid, revision, second) is False
        assert d.dashboard()["text"] == "literal words"
        d.cancel(sid)
        assert d.dashboard()["text"] == "" and d.metadata()["phase"] == "cancelled"
    finally:
        await close(d, stt, pool)
