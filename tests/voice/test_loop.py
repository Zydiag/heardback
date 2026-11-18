import asyncio
import contextlib
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

sys.path.insert(0, str(Path(__file__).parent.parent))
from fake_brain import FakeBrain  # noqa: E402

from conftest import FIXTURES, FakeCues, FakeMedia, FakeStt, FakeTts, energy_prob, push_wav, push_zeros  # noqa: E402
from heardback.voice.config import Config  # noqa: E402
from heardback.voice.link import Link  # noqa: E402
from heardback.voice.loop import Voice  # noqa: E402

CMD = FIXTURES / "cmd.wav"


async def until(cond, timeout=5.0):
    async def poll():
        while not cond():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(poll(), timeout)


@contextlib.asynccontextmanager
async def rig(tmp_path, script=(), cfg=None, start_brain=True, stt_cls=FakeStt, prob=energy_prob, **kw):
    path = tmp_path / "s.sock"
    brain = FakeBrain(path)
    if start_brain:
        await brain.start()
    v = Voice(cfg or Config(), Link(path, "test-voice-token"), stt=stt_cls(script), tts=FakeTts(), cues=FakeCues(),
              media=FakeMedia(), prob=prob, **kw)
    task = asyncio.create_task(v.run())
    if start_brain:
        await brain.wait_for("hello")
    else:
        await asyncio.sleep(0.05)
    try:
        yield v, brain
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await brain.close()


async def test_hello_on_connect(tmp_path):
    async with rig(tmp_path) as (v, brain):
        assert brain.received[0] == {"type": "hello", "role": "voice", "token": "test-voice-token"}
        assert v.state == "IDLE"


@pytest.mark.parametrize("state", ["IDLE", "LISTEN", "CONFIRM", "announcing", "dictation"])
async def test_unavailable_confirm_acknowledged_within_one_second(tmp_path, monkeypatch, state):
    from types import SimpleNamespace
    async with rig(tmp_path) as (v, brain):
        with monkeypatch.context() as patch:
            v.state = "ANNOUNCE" if state == "announcing" else "THINK" if state == "dictation" else state
            if state == "dictation":
                patch.setattr(v, "dictation", SimpleNamespace(engaged=True))
            async with asyncio.timeout(1):
                await v._on_message({"type": "confirm", "text": "Close window 6?",
                                     "confirmation_id": "cannot-speak", "timeout_s": 8})
                reply = await brain.wait_for("confirm_reply")
            assert reply == {"type": "confirm_reply", "ok": False, "unasked": True,
                             "confirmation_id": "cannot-speak"}
            assert not v.tts.said


async def test_ptt_cmd_sends_utterance(tmp_path):
    async with rig(tmp_path, ["switch to tmux window 2"]) as (v, brain):
        v.ptt()
        push_wav(v, CMD)
        u = await brain.wait_for("utterance")
        types = [m["type"] for m in brain.received]
        assert types.index("state") < types.index("utterance")
        assert brain.of("state")[0] == {"type": "state", "busy": True}
        assert u["text"] == "switch to tmux window 2" and u["speaker_ok"] is True and isinstance(u["t"], float)
        assert v.cues.played[0] == "wake" and v.state == "THINK"
        assert v.media.calls == ["pause"]


async def test_command_mishear_fix_is_sent_and_original_is_traced(tmp_path):
    async with rig(tmp_path, ["cloud code status"]) as (v, brain):
        v.ptt()
        push_wav(v, CMD)
        u = await brain.wait_for("utterance")
        assert u["text"] == "Claude Code status"
        heard, = [e for e in _trace() if e["ev"] == "heard"]
        assert heard["text"] == u["text"] and heard["raw"] == "cloud code status"


async def test_say_done_returns_idle(tmp_path):
    async with rig(tmp_path, ["switch to tmux window 2"]) as (v, brain):
        v.ptt()
        push_wav(v, CMD)
        await brain.wait_for("utterance")
        await brain.send({"type": "say", "text": "Done.", "final": True})
        await brain.send({"type": "done"})
        await brain.wait_for("state", n=2)
        assert brain.of("state")[1] == {"type": "state", "busy": False}
        assert v.tts.said == ["Done."] and v.state == "IDLE"
        await until(lambda: v.media.calls == ["pause", "resume"], 2)


@pytest.mark.parametrize("said,calls", [("Stop.", ["pause", "keep", "resume"]),
                                        ("stop the build", ["pause", "resume"])])
async def test_bare_stop_keeps_the_music_paused(tmp_path, said, calls):   # duck
    async with rig(tmp_path, [said]) as (v, brain):
        v.ptt()
        push_wav(v, CMD)
        await brain.wait_for("utterance")
        await brain.send({"type": "done"})
        await until(lambda: v.media.calls == calls, 2)


async def test_silence_gives_nothing_cue(tmp_path):
    async with rig(tmp_path) as (v, brain):
        v.ptt()
        push_zeros(v, 5)
        await until(lambda: v.state == "IDLE")
        assert "nothing" in v.cues.played and not brain.of("utterance")
        assert v.stt.calls == 0


async def test_empty_transcript_not_sent(tmp_path):
    async with rig(tmp_path, [""]) as (v, brain):
        v.ptt()
        push_wav(v, CMD)
        await until(lambda: v.state == "IDLE")
        assert "nothing" in v.cues.played and not brain.of("utterance")
        await brain.wait_for("state", n=2)


# --- Task 15: announcements, stuck turns, confirm -------------------------------------------
YES = FIXTURES / "yes.wav"


async def turn_to_think(v, brain, n=1):
    v.ptt()
    push_wav(v, CMD)
    return await brain.wait_for("utterance", n=n)


async def test_announcement_from_idle(tmp_path):
    async with rig(tmp_path) as (v, brain):
        await brain.send({"type": "say", "text": "Build finished.", "final": True, "announce": True})
        await brain.wait_for("state", n=2)
        assert v.tts.said == ["Build finished."] and v.state == "IDLE"
        assert brain.of("state") == [{"type": "state", "busy": True}, {"type": "state", "busy": False}]
        await until(lambda: v.media.calls == ["pause", "resume"], 2)


async def test_announcements_queue_newest_3(tmp_path):
    async with rig(tmp_path, ["switch to tmux window 2"]) as (v, brain):
        v.ptt()
        for i in range(5):
            await brain.send({"type": "say", "text": f"News {i}.", "final": True, "announce": True})
        await until(lambda: len(v.pending) == 3)
        push_wav(v, CMD)
        await brain.wait_for("utterance")
        await brain.send({"type": "say", "text": "Done.", "final": True})
        await brain.send({"type": "done"})
        await brain.wait_for("state", n=2)
        assert v.tts.said == ["Done.", "News 2.", "News 3.", "News 4."] and v.state == "IDLE"
        assert [m["busy"] for m in brain.of("state")] == [True, False]


async def test_stuck_turn_errors(tmp_path):
    async with rig(tmp_path, ["switch to tmux window 2"], stuck_s=0.2) as (v, brain):
        await turn_to_think(v, brain)
        await until(lambda: v.state == "IDLE", 2)
        assert v.cues.played[-1] == "error" and v.tts.stopped >= 1


async def confirm_turn(tmp_path, reply_text, wav=YES, seconds=None):
    async with rig(tmp_path, ["switch to tmux window 2", reply_text]) as (v, brain):
        await turn_to_think(v, brain)
        await brain.send({"type": "confirm", "text": "Close 7 tabs?", "timeout_s": 8})
        await until(lambda: v.state == "CONFIRM")
        assert v.tts.said == ["Close 7 tabs?"] and "tick" in v.cues.played
        v.ptt()                          # not a yes: restarts the same listen
        assert v.state == "CONFIRM"
        if wav:
            push_wav(v, wav)
        else:
            push_zeros(v, seconds)
        r = await brain.wait_for("confirm_reply")
        assert v.state == "THINK"
        assert r["parent"] == v.t.tid and r["turn"] == r["turn"][:-1] + "c" != v.t.tid   # own id + parent
        return r, v


async def test_confirm_yes(tmp_path):
    r, v = await confirm_turn(tmp_path, "yeah do it")
    assert r["ok"] is True and r["text"] == "yeah do it" and r["speaker_ok"] is True


async def test_confirm_maybe(tmp_path):
    r, _ = await confirm_turn(tmp_path, "maybe later")
    assert r["ok"] is False and r["text"] == "maybe later"


async def test_confirm_timeout(tmp_path):
    r, v = await confirm_turn(tmp_path, "unused", wav=None, seconds=8.5)
    assert r["ok"] is False and r["text"] == "" and v.stt.calls == 1


class BrokenStt(FakeStt):
    """Raises on the call whose text is "BOOM" (a CUDA error, an OOM)."""
    def transcribe(self, audio):
        t = super().transcribe(audio)
        if t == "BOOM":
            raise RuntimeError("cuda error")
        return t


async def test_stt_error_goes_idle(tmp_path):
    async with rig(tmp_path, ["BOOM"], stt_cls=BrokenStt) as (v, brain):
        v.ptt()
        push_wav(v, CMD)
        await until(lambda: v.state == "IDLE", 2)
        assert v.cues.played == ["wake", "error"] and not brain.of("utterance")
        await brain.wait_for("state", n=2)       # busy:false, so the announcer isn't held


class SlowStt(FakeStt):
    """Hangs on "HANG" (a wedged CUDA call)."""
    def transcribe(self, audio):
        t = super().transcribe(audio)
        if t == "HANG":
            import time
            time.sleep(1.0)
        return t


async def test_hung_stt_fails_voice_run_instead_of_reusing_blocked_gpu(tmp_path):
    import threading

    release = threading.Event()

    class HungStt(FakeStt):
        def transcribe(self, audio):
            release.wait(2)
            return "too late"

    with pytest.raises(RuntimeError, match="STT timed out"):
        try:
            async with rig(tmp_path, stt_cls=HungStt, stt_timeout_s=0.02) as (v, brain):
                v.ptt()
                push_wav(v, CMD)
                await asyncio.sleep(0.3)
                assert not brain.of("utterance")
        finally:
            release.set()


@pytest.mark.parametrize("phase", ["listen", "followup", "continuation", "confirm"])
async def test_mic_stalls_without_frames_listen_expires_and_resumes_music(tmp_path, phase):
    async with rig(tmp_path) as (v, brain):
        v.ptt()
        await until(lambda: v.media.calls == ["pause"])
        if phase == "followup":
            v.t.followup = True
        elif phase == "continuation":
            v.t.head = ("switch to", 1.0, True)
        elif phase == "confirm":
            v.t.confirmation_id = "stalled-confirm"
            v.state = "THINK"   # a confirm listen starts after the read-back (LISTEN -> CONFIRM is illegal)
            v._confirm_listen(0.02)
        v.ep.deadline = asyncio.get_running_loop().time() + 0.02
        await until(lambda: v.ep is None)
        if phase == "confirm":
            reply = await brain.wait_for("confirm_reply")
            assert reply["ok"] is False and reply["text"] == ""
            await brain.send({"type": "done"})
        await until(lambda: v.state == "IDLE" and v.media.calls == ["pause", "resume"])
        assert not brain.of("utterance") and not v.stt.calls


async def test_stt_error_in_confirm_says_no(tmp_path):
    async with rig(tmp_path, ["switch to tmux window 2", "BOOM"], stt_cls=BrokenStt) as (v, brain):
        await turn_to_think(v, brain)
        await brain.send({"type": "confirm", "text": "Close 7 tabs?", "timeout_s": 8})
        await until(lambda: v.state == "CONFIRM")
        push_wav(v, YES)
        r = await brain.wait_for("confirm_reply")
        assert r["ok"] is False and r["speaker_ok"] is False


async def test_long_command_readback_is_spoken_in_full(tmp_path):   # 2026-11-17 (user): you hear exactly what you confirm
    full = "Run 'cd \"$(zoxide query webapp)\" && claude' in window 5?"
    async with rig(tmp_path, ["switch to tmux window 2"]) as (v, brain):
        await turn_to_think(v, brain)
        await brain.send({"type": "confirm", "text": full, "timeout_s": 8})
        await until(lambda: v.state == "CONFIRM")
        assert v.tts.said == [full]
        assert [e["text"] for e in _trace() if e["ev"] == "confirm_asked"] == [full]   # the log keeps it all


async def test_ptt_in_confirm_restarts_listen(tmp_path):
    r, v = await confirm_turn(tmp_path, "yes")
    assert r["ok"] is True and v.cues.played.count("wake") == 1 and v.cues.played.count("tick") == 2


async def test_ptt_during_readback_closes_confirm(tmp_path):
    async with rig(tmp_path, ["switch to tmux window 2", "yes"]) as (v, brain):
        await turn_to_think(v, brain)
        v.tts.finish_delay = 1.0         # still reading back
        await brain.send({"type": "confirm", "text": "Press enter in window 1?", "timeout_s": 8,
                          "confirmation_id": "ptt-barged"})
        await until(lambda: v.tts.said == ["Press enter in window 1?"])
        v.ptt()
        assert v.state == "LISTEN" and v.tts.stopped == 1 and v.cues.played.count("wake") == 2
        r = await brain.wait_for("confirm_reply")
        assert r == {"type": "confirm_reply", "ok": False, "unasked": True,
                     "confirmation_id": "ptt-barged"}
        assert brain.of("interrupt") and v.state == "LISTEN" and not v.cues.played.count("tick")


async def test_barged_readback_closes_original_confirmation(tmp_path):
    async with rig(tmp_path, ["switch to tmux window 2"]) as (v, brain):
        await turn_to_think(v, brain)
        v.tts.finish_delay = 0.1
        await brain.send({"type": "confirm", "text": "Close window 6?", "timeout_s": 8,
                          "remaining_s": 60, "confirmation_id": "barged"})
        await until(lambda: v.tts.said == ["Close window 6?"])
        v._start(turn_ok=True)
        reply = await brain.wait_for("confirm_reply")
        assert reply == {"type": "confirm_reply", "ok": False, "unasked": True,
                         "confirmation_id": "barged"}
        assert v.state == "LISTEN" and not v.cues.played.count("tick")


async def test_ptt_after_confirm_answered_barges(tmp_path):
    async with rig(tmp_path, ["switch to tmux window 2", "yes"]) as (v, brain):
        await turn_to_think(v, brain)
        await brain.send({"type": "confirm", "text": "Close 7 tabs?", "timeout_s": 8})
        await until(lambda: v.state == "CONFIRM")
        push_wav(v, YES)
        await brain.wait_for("confirm_reply")
        v.ptt()                          # the confirm is answered: an ordinary barge-in
        await brain.wait_for("interrupt")
        assert v.state == "LISTEN"


# --- Task 16: barge-in, mic toggle, brain down, mic lost ------------------------------------
async def test_ptt_barge_in_sends_interrupt(tmp_path):
    async with rig(tmp_path, ["switch to tmux window 2"]) as (v, brain):
        await turn_to_think(v, brain)
        await brain.send({"type": "say", "text": "Here is a long answer.", "final": False})
        await until(lambda: v.state == "SPEAK")
        v.ptt()
        await brain.wait_for("interrupt")
        assert v.tts.stopped == 1 and v.state == "LISTEN" and v.cues.played.count("wake") == 2


async def test_ptt_over_announcement_no_interrupt(tmp_path):
    async with rig(tmp_path) as (v, brain):
        v.tts.finish_delay = 1.0
        await brain.send({"type": "say", "text": "Build finished.", "final": True, "announce": True})
        await until(lambda: v.state == "ANNOUNCE")
        v.ptt()
        assert v.state == "LISTEN" and v.tts.stopped == 1
        await asyncio.sleep(1.5)
        assert not brain.of("interrupt") and v.state == "LISTEN"


async def test_mic_off_blocks_ptt(tmp_path):
    async with rig(tmp_path) as (v, brain):
        toggled = []
        v.on_mic_toggle = toggled.append
        v.toggle_mic()
        v.ptt()
        await asyncio.sleep(0.1)
        assert v.state == "IDLE" and toggled == [False] and not v.mic_on
        assert v.cues.notes == ["Mic off", "Mic off (Super+Shift+C)"]
        v.toggle_mic()
        await asyncio.sleep(0.05)
        assert toggled == [False, True] and v.cues.notes[-1] == "Mic on"


async def test_mic_off_aborts_listen(tmp_path):
    async with rig(tmp_path) as (v, brain):
        v.ptt()
        v.toggle_mic()
        assert v.state == "IDLE" and "error" not in v.cues.played


async def test_brain_down_at_ptt(tmp_path):
    async with rig(tmp_path, start_brain=False) as (v, brain):
        v.ptt()
        await until(lambda: v.tts.said)
        assert v.cues.played == ["error"] and v.tts.said == ["My brain is offline."] and v.state == "IDLE"


async def test_brain_drop_mid_turn(tmp_path):
    async with rig(tmp_path, ["switch to tmux window 2"]) as (v, brain):
        await turn_to_think(v, brain)
        await brain.drop()
        await until(lambda: v.state == "IDLE")
        assert v.cues.played[-1] == "error" and v.tts.said == ["My brain is offline."]
        await until(lambda: v.media.calls == ["pause", "resume"], 2)
        await brain.wait_for("hello", n=2, timeout=5)
        await asyncio.sleep(0.2)
        assert len(brain.of("utterance")) == 1 and v.link.up


async def test_mic_lost_mid_listen(tmp_path):
    async with rig(tmp_path) as (v, brain):
        fell = []
        v.on_mic_lost_fallback = lambda: fell.append(1)
        v.ptt()
        push_zeros(v, 0.5)
        v.mic_lost()
        await until(lambda: v.tts.said)
        assert v.state == "IDLE" and v.cues.played[-1] == "error" and fell == [1]
        assert v.tts.said == ["I lost the mic, say that again."] and v.stt.calls == 0


async def test_mic_lost_in_confirm_says_no(tmp_path):
    async with rig(tmp_path, ["switch to tmux window 2"]) as (v, brain):
        await turn_to_think(v, brain)
        await brain.send({"type": "confirm", "text": "Close 7 tabs?", "timeout_s": 8})
        await until(lambda: v.state == "CONFIRM")
        v.mic_lost()
        r = await brain.wait_for("confirm_reply")
        assert r["ok"] is False and v.state == "IDLE"


# --- Task 19: wake word ------------------------------------------------------------------------
class FakeWake:
    """score() pops a scripted list (0.0 when empty); ring_audio returns silence."""
    def __init__(self, scores=()):
        self.scores, self.resets = list(scores), 0

    def score(self, frame):
        return self.scores.pop(0) if self.scores else 0.0

    def ring_audio(self, seconds):
        return np.zeros(int(seconds * 16000), np.int16)

    def reset(self):
        self.resets += 1


async def test_loop_wake_starts_turn_untrusted(tmp_path):
    async with rig(tmp_path, ["switch to tmux window 2"], wake=FakeWake([0.9, 0.9])) as (v, brain):
        push_zeros(v, 0.16)
        assert v.state == "LISTEN" and v.cues.played == ["wake"] and v.wake.resets == 1
        assert len(v.wake_audio) == 32000
        push_wav(v, CMD)
        u = await brain.wait_for("utterance")
        assert u["speaker_ok"] is False
        await until(lambda: v.state == "IDLE")      # the brain sends no `done` for it: no stuck wait
        assert "error" not in v.cues.played


async def test_wake_ignored_when_automuted(tmp_path):
    async with rig(tmp_path, wake=FakeWake([0.9] * 10)) as (v, brain):
        v.set_automute(True)
        push_zeros(v, 0.8)
        assert v.state == "IDLE" and v.wake.scores == [0.9] * 10
        v.set_automute(False)
        push_zeros(v, 0.16)
        assert v.state == "LISTEN"


async def test_wake_near_miss_is_one_episode_with_peak(tmp_path):
    async with rig(tmp_path, wake=FakeWake([0.2, 0.15, 0.0, 0.0])) as (v, brain):
        push_zeros(v, 0.32)
        events = [e for e in _trace() if e["ev"] == "wake_near_miss"]
        assert len(events) == 1 and events[0]["peak"] == 0.2 and events[0]["state"] == "IDLE"


async def test_wake_tail_after_fire_does_not_log_near_miss(tmp_path):
    async with rig(tmp_path, wake=FakeWake([0.9, 0.9, 0.2, 0.0])) as (v, brain):
        push_zeros(v, 0.32)
        assert v.state == "LISTEN"
        assert not [e for e in _trace() if e["ev"] == "wake_near_miss"]


async def test_wake_near_miss_during_think_is_logged_without_barging(tmp_path):
    async with rig(tmp_path, ["switch to tmux window 2"], wake=FakeWake()) as (v, brain):
        await turn_to_think(v, brain)
        v.wake.scores = [0.2, 0.0]
        push_zeros(v, 0.16)
        events = [e for e in _trace() if e["ev"] == "wake_near_miss"]
        assert len(events) == 1 and events[0]["state"] == "THINK" and v.state == "THINK"


async def test_wake_barge_in_needs_boost(tmp_path):
    async with rig(tmp_path, ["switch to tmux window 2"], wake=FakeWake()) as (v, brain):
        await turn_to_think(v, brain)
        await brain.send({"type": "say", "text": "Here is a long answer.", "final": False})
        await until(lambda: v.state == "SPEAK")
        v.wake.scores = [0.65, 0.65, 0.65]
        push_zeros(v, 0.24)
        assert v.state == "SPEAK" and not brain.of("interrupt")
        v.wake.scores = [0.75, 0.75]
        push_zeros(v, 0.16)
        await brain.wait_for("interrupt")
        assert v.state == "LISTEN" and v.t.ok is False and v.tts.stopped == 1


# --- Task 24: speaker gates --------------------------------------------------------------------
GATED = Config(speaker_check="on", speaker_wake=0.5, speaker_utt=0.6)


class FakeSpeaker:
    """score() pops a scripted list; records the length of each clip; optional sleep."""
    def __init__(self, scores, sleep=0.0):
        self.scores, self.sleep, self.lens = list(scores), sleep, []

    def score(self, audio):
        import time
        time.sleep(self.sleep)
        self.lens.append(len(audio))
        return self.scores.pop(0)


class SlowStt(FakeStt):
    def transcribe(self, audio):
        import time
        time.sleep(0.2)
        return super().transcribe(audio)


async def test_wake_gate_reject_plays_nothing_cue(tmp_path):
    async with rig(tmp_path, cfg=GATED, wake=FakeWake([0.9, 0.9]), speaker=FakeSpeaker([0.3])) as (v, brain):
        push_zeros(v, 0.16)
        await until(lambda: v.speaker.lens)
        await asyncio.sleep(0.05)
        assert v.speaker.lens == [24000]               # last 1.5 s of the ring
        assert v.state == "IDLE" and v.cues.played == ["nothing"] and not brain.of("state")


async def wake_turn(tmp_path, spk, script=("switch to tmux window 2",), **kw):
    rigged = rig(tmp_path, list(script), cfg=GATED, wake=FakeWake([0.9, 0.9]), speaker=spk, **kw)
    v, brain = await rigged.__aenter__()
    push_zeros(v, 0.16)
    await until(lambda: v.state == "LISTEN")
    return rigged, v, brain


async def test_utt_gate_drop(tmp_path):
    rigged, v, brain = await wake_turn(tmp_path, FakeSpeaker([0.9, 0.4]))
    try:
        push_wav(v, CMD)
        await until(lambda: v.state == "IDLE")
        assert not brain.of("utterance") and v.cues.played == ["wake", "nothing"]
        assert v.speaker.lens[1] > 32000              # wake audio + the utterance
    finally:
        await rigged.__aexit__(None, None, None)


async def test_utt_gate_pass_sets_speaker_ok(tmp_path):
    rigged, v, brain = await wake_turn(tmp_path, FakeSpeaker([0.9, 0.7]))
    try:
        push_wav(v, CMD)
        u = await brain.wait_for("utterance")
        assert u["speaker_ok"] is True and v.state == "THINK"   # a trusted turn waits for `done`
    finally:
        await rigged.__aexit__(None, None, None)


async def test_confirm_reply_gate(tmp_path):
    rigged, v, brain = await wake_turn(tmp_path, FakeSpeaker([0.9, 0.7, 0.2]), ["close tabs", "yeah do it"])
    try:
        push_wav(v, CMD)
        await brain.wait_for("utterance")
        await brain.send({"type": "confirm", "text": "Close 7 tabs?", "timeout_s": 8})
        await until(lambda: v.state == "CONFIRM")
        push_wav(v, YES)
        r = await brain.wait_for("confirm_reply")
        assert r["ok"] is False and r["speaker_ok"] is False and r["text"] == "yeah do it"
    finally:
        await rigged.__aexit__(None, None, None)


async def test_stt_and_ecapa_parallel(tmp_path):
    rigged, v, brain = await wake_turn(tmp_path, FakeSpeaker([0.9, 0.7]), stt_cls=SlowStt)
    try:
        v.speaker.sleep = 0.2
        push_wav(v, CMD, tail_s=1.0)
        t0 = asyncio.get_running_loop().time()
        u = await brain.wait_for("utterance")
        assert asyncio.get_running_loop().time() - t0 < 0.35 and u["speaker_ok"] is True
    finally:
        await rigged.__aexit__(None, None, None)


async def test_ptt_skips_gates(tmp_path):
    async with rig(tmp_path, ["switch to tmux window 2", "yeah do it"], cfg=GATED, wake=FakeWake(),
                   speaker=FakeSpeaker([])) as (v, brain):
        await turn_to_think(v, brain)
        assert brain.of("utterance")[0]["speaker_ok"] is True
        await brain.send({"type": "confirm", "text": "Close 7 tabs?", "timeout_s": 8})
        await until(lambda: v.state == "CONFIRM")
        push_wav(v, YES)
        r = await brain.wait_for("confirm_reply")
        assert r["ok"] is True and r["speaker_ok"] is True and v.speaker.lens == []


# --- speaker_check = "wake": only the wake word is scored (user, 2026-11-02) ------------------
WAKE_ONLY = Config(speaker_check="wake", speaker_wake=0.5)


async def test_wake_mode_pass_is_trusted_and_unscored(tmp_path):
    async with rig(tmp_path, ["switch to tmux window 2", "yeah do it"], cfg=WAKE_ONLY,
                   wake=FakeWake([0.9, 0.9]), speaker=FakeSpeaker([0.9, 0.9])) as (v, brain):
        push_zeros(v, 0.16)
        await until(lambda: v.state == "LISTEN")
        push_wav(v, CMD)
        u = await brain.wait_for("utterance")
        assert u["speaker_ok"] is True and v.speaker.lens == [24000]   # only the wake clip was scored
        await brain.send({"type": "confirm", "text": "Close 7 tabs?", "timeout_s": 8})
        await until(lambda: v.state == "CONFIRM")
        push_wav(v, YES)
        r = await brain.wait_for("confirm_reply")
        assert r["ok"] is True and r["speaker_ok"] is True and len(v.speaker.lens) == 2   # the yes is scored too, log only


async def test_wake_mode_confirm_scored_logged_not_enforced(tmp_path):
    """Plan 18 T5: wake turn passes (score 1.0), then the yes scores 0.0: still a trusted yes, score in the trace."""
    from heardback.voice.speaker import SpeakerCheck
    embs = [np.array([1.0, 0.0]), np.array([0.0, 1.0])]      # wake clip, then the confirm clip
    spk = SpeakerCheck(np.array([1.0, 0.0]), embed=lambda a: embs.pop(0))
    async with rig(tmp_path, ["switch to tmux window 2", "yeah do it"], cfg=Config(speaker_check="wake", speaker_wake=0.5,
                   speaker_utt=0.6), wake=FakeWake([0.9, 0.9]), speaker=spk) as (v, brain):
        push_zeros(v, 0.16)
        await until(lambda: v.state == "LISTEN")
        push_wav(v, CMD)
        await brain.wait_for("utterance")
        await brain.send({"type": "confirm", "text": "Close 7 tabs?", "timeout_s": 8})
        await until(lambda: v.state == "CONFIRM")
        push_wav(v, YES)
        r = await brain.wait_for("confirm_reply")
        assert r["ok"] is True and r["speaker_ok"] is True
        c = [e for e in _trace() if e["ev"] == "confirm_heard"][-1]
        assert c["spk"] == 0.0 and c["speaker_ok"] is True and c["answer"] == "yes"


async def test_wake_mode_fail_is_rejected(tmp_path):
    async with rig(tmp_path, cfg=WAKE_ONLY, wake=FakeWake([0.9, 0.9]), speaker=FakeSpeaker([0.3])) as (v, brain):
        push_zeros(v, 0.16)
        await until(lambda: v.speaker.lens)
        await asyncio.sleep(0.05)
        assert v.state == "IDLE" and not brain.of("utterance")


# --- add-on: follow-up listen after a question (user, 2026-10-30) --------------------------------
async def ask_back(v, brain, text="Which window?"):
    await turn_to_think(v, brain)
    await brain.send({"type": "say", "text": text, "final": True, "expect_reply": True})
    await brain.send({"type": "done"})


async def test_question_listens_again(tmp_path):
    async with rig(tmp_path, ["type it working", "the zen tab"]) as (v, brain):
        await ask_back(v, brain)
        await until(lambda: v.state == "LISTEN")
        assert v.t.n == 2 and v.t.ok is True and v.cues.played.count("wake") == 2
        assert v.cues.sticky == ["Which window?"] and v.cues.notes.count("Listening…") == 1
        push_wav(v, CMD)
        u = await brain.wait_for("utterance", n=2)
        assert u["text"] == "the zen tab" and u["speaker_ok"] is True


async def test_question_silence_goes_idle_quietly(tmp_path, monkeypatch):
    monkeypatch.setattr("heardback.voice.loop.FOLLOWUP_WAIT_S", 0.5)
    async with rig(tmp_path, ["type it working"]) as (v, brain):
        await ask_back(v, brain)
        await until(lambda: v.state == "LISTEN")
        push_zeros(v, 0.4)
        assert v.state == "LISTEN"
        push_zeros(v, 0.3)
        await until(lambda: v.state == "IDLE")
        assert "error" not in v.cues.played and "nothing" not in v.cues.played
        assert brain.of("state")[-1] == {"type": "state", "busy": False}


async def test_question_barged_no_followup(tmp_path):
    async with rig(tmp_path, ["type it working"]) as (v, brain):
        await turn_to_think(v, brain)
        await brain.send({"type": "say", "text": "Which window?", "final": True, "expect_reply": True})
        await until(lambda: v.state == "SPEAK")
        v.ptt()                                       # barge-in during the question
        await brain.send({"type": "done"})
        await asyncio.sleep(0.1)
        assert v.t.n == 2 and v.cues.played.count("wake") == 2 and v.ep is not None   # the barge turn only


async def test_follow_up_off(tmp_path):
    async with rig(tmp_path, ["type it working"], cfg=Config(follow_up=False)) as (v, brain):
        await ask_back(v, brain)
        await until(lambda: v.state == "IDLE")
        assert v.t.n == 1


async def test_question_with_mic_off_goes_idle(tmp_path):
    async with rig(tmp_path, ["type it working"]) as (v, brain):
        await turn_to_think(v, brain)
        await brain.send({"type": "say", "text": "Which window?", "final": True, "expect_reply": True})
        await until(lambda: v.state == "SPEAK")
        v.toggle_mic()                                # no frames will come: don't wait in LISTEN
        await brain.send({"type": "done"})
        await until(lambda: v.state == "IDLE")


# --- trace: turn ids --------------------------------------------------------------------------
async def test_turn_ids_sent_and_followup_has_parent(tmp_path):
    async with rig(tmp_path, ["type it working", "the zen tab"]) as (v, brain):
        await ask_back(v, brain)
        await until(lambda: v.state == "LISTEN")
        push_wav(v, CMD)
        first, second = brain.of("utterance")[0], await brain.wait_for("utterance", n=2)
        assert first["turn"].endswith("-1") and "parent" not in first
        assert second["turn"].endswith("-2") and second["parent"] == first["turn"]


def _trace():
    import json
    import os
    p = Path(os.environ["XDG_STATE_HOME"]) / "heardback" / "trace.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


async def test_turn_trace_events(tmp_path):
    async with rig(tmp_path, ["switch to tmux window 2"]) as (v, brain):
        v.ptt()
        push_wav(v, CMD)
        await brain.wait_for("utterance")
        await brain.send({"type": "say", "text": "Window 2.", "final": True})
        await brain.send({"type": "done"})
        await until(lambda: v.state == "IDLE")
        evs = [e for e in _trace() if e["turn"] == v.t.tid]
        assert [e["ev"] for e in evs] == ["wake", "listen", "endpoint", "endpoint", "heard", "say", "turn_end"]
        wake, _, endpoint, continuation, heard, say, end = evs
        assert endpoint["phase"] == "listen" and endpoint["status"] == "done" and endpoint["reason"] == "silence"
        assert continuation["phase"] == "continuation_buffer" and continuation["reason"] == "no_speech_timeout"
        assert wake["source"] == "ptt" and heard["text"] == "switch to tmux window 2" and heard["audio_s"] > 0
        assert say["text"] == "Window 2." and end["outcome"] == "done" and end["total_s"] > 0


async def test_listen_timeout_ends_nothing(tmp_path):
    async with rig(tmp_path) as (v, brain):
        v.ptt()
        push_zeros(v, 12)
        await until(lambda: v.state == "IDLE")
        assert [e["outcome"] for e in _trace() if e["ev"] == "turn_end"] == ["nothing"]


async def test_listening_hard_cap_does_not_dispatch_a_partial_command(tmp_path):
    import numpy as np
    from heardback.voice.audio_in import FRAME

    cfg = Config(max_utterance_s=5.0, duck_media=False)
    async with rig(tmp_path, ["must not dispatch"], cfg=cfg) as (v, brain):
        v.ptt()
        loud = np.full(FRAME, 12000, dtype=np.int16)
        for _ in range(5 * 16000 // FRAME + 2):
            v.feed(loud)
        await until(lambda: v.state == "IDLE")
        assert not brain.of("utterance") and v.stt.calls == 0
        endpoint, = [e for e in _trace() if e.get("turn") == v.t.tid and e["ev"] == "endpoint"]
        assert endpoint["status"] == "done" and endpoint["reason"] == "max_duration"
        assert endpoint["max_utterance_s"] == 5.0
        assert [e["outcome"] for e in _trace() if e["ev"] == "turn_end" and e.get("turn") == v.t.tid] == ["timeout"]


async def test_background_exception_lands_as_error_event(tmp_path):
    async with rig(tmp_path) as (v, brain):
        async def boom():
            raise RuntimeError("kaput")
        t = v._bg(boom())
        with contextlib.suppress(RuntimeError):
            await t
        await asyncio.sleep(0)   # done callbacks run on the next loop pass
        (err,) = [e for e in _trace() if e["ev"] == "error"]
        assert err["src"] == "voice" and "RuntimeError: kaput" in err["traceback"] and "boom" in err["where"]


async def test_vad_reset_per_listen_and_confirm(tmp_path):
    resets = []

    def prob(chunk):
        return energy_prob(chunk)
    prob.reset = lambda: resets.append(1)
    async with rig(tmp_path, ["switch to tmux window 2", "yes"], prob=prob) as (v, brain):
        await turn_to_think(v, brain)
        assert len(resets) == 1
        await brain.send({"type": "confirm", "text": "Close 7 tabs?", "timeout_s": 8})
        await until(lambda: v.state == "CONFIRM")
        assert len(resets) == 2


# --- frictionfix 1: keep listening after a dangling command head (friction #19-20, #36, #38) ----
def _heard():
    return [e for e in _trace() if e["ev"] == "heard"]


async def test_dangling_head_joins_next_endpoint(tmp_path):
    async with rig(tmp_path, ["Type in this window.", "hello world"]) as (v, brain):
        v.ptt()
        push_wav(v, CMD, tail_s=1.0)
        await until(lambda: v.t.head is not None and v.state == "LISTEN")
        assert not brain.of("utterance") and v.cues.played.count("wake") == 1
        push_wav(v, CMD, tail_s=1.0)
        u = await brain.wait_for("utterance")
        assert u["text"] == "Type in this window. hello world" and v.t.n == 1
        (h,) = _heard()
        assert h["joined"] == ["Type in this window.", "hello world"] and h["turn"] == u["turn"]


async def test_dangling_rest_spoken_during_stt_is_kept(tmp_path):
    async with rig(tmp_path, ["typing", "in window 1 hello"]) as (v, brain):
        v.ptt()
        push_wav(v, CMD, tail_s=0.8)       # endpoint fires; the next words arrive before STT returns
        push_wav(v, CMD, tail_s=1.0)
        u = await brain.wait_for("utterance")
        assert u["text"] == "typing in window 1 hello" and len(brain.of("utterance")) == 1


async def test_dangling_head_alone_after_silence(tmp_path):
    async with rig(tmp_path, ["type in this window"]) as (v, brain):
        v.ptt()
        push_wav(v, CMD, tail_s=1.0)
        await until(lambda: v.t.head is not None)
        push_zeros(v, 5)
        u = await brain.wait_for("utterance")
        assert u["text"] == "type in this window" and v.stt.calls == 1
        assert _heard()[0]["joined"] == ["type in this window"] and "nothing" not in v.cues.played


async def test_dangling_extends_only_once(tmp_path):
    async with rig(tmp_path, ["type", "in this window"]) as (v, brain):
        v.ptt()
        push_wav(v, CMD, tail_s=1.0)
        await until(lambda: v.t.head is not None)
        push_wav(v, CMD, tail_s=1.0)
        u = await brain.wait_for("utterance")
        assert u["text"] == "type in this window" and v.state == "THINK"


async def test_announcement_with_question_listens_once(tmp_path):   # sessionhelp 2
    async with rig(tmp_path, ["yes", "yes"]) as (v, brain):
        previous = None
        for count in (1, 2):
            await brain.send({"type": "say", "text": "heardback asks: run x? 1 yes, 2 no.", "final": True,
                              "announce": True, "expect_reply": True})
            await until(lambda: v.state == "LISTEN")
            assert v.t.followup
            push_wav(v, YES)
            u = await brain.wait_for("utterance", n=count)
            assert u["text"] == "yes" and u.get("parent") == previous
            previous = u["turn"]
            await brain.send({"type": "done"})
            await until(lambda: v.state == "IDLE")


async def test_cancel_stops_think_and_listen(tmp_path):
    async with rig(tmp_path, ["switch to tmux window 2"]) as (v, brain):
        v.cancel()                       # idle: no-op
        v.ptt()
        v.cancel()                       # while listening
        assert v.state == "IDLE"
        await turn_to_think(v, brain)
        v.cancel()                       # while the brain works
        await brain.wait_for("interrupt")
        assert v.state == "IDLE"


async def test_repeat_preserves_confirmation_id_and_absolute_expiry(tmp_path):
    async with rig(tmp_path, ["open Spotify", "repeat please", "say that again", "yes"]) as (v, brain):
        await turn_to_think(v, brain)
        for index in range(3):
            await brain.send({"type": "confirm", "text": "Open Spotify in a new tab?", "timeout_s": 8,
                              "confirmation_id": "same-attempt", "remaining_s": 60 - index})
            await until(lambda: v.state == "CONFIRM")
            push_wav(v, CMD)
            reply = await brain.wait_for("confirm_reply", n=index + 1)
            assert reply["confirmation_id"] == "same-attempt"
            assert reply["ok"] is (index == 2)
        assert v.tts.said == ["Open Spotify in a new tab?"] * 3


async def test_old_confirmation_stt_cannot_borrow_replacement_id(tmp_path, monkeypatch):
    from types import SimpleNamespace
    async with rig(tmp_path) as (v, brain):
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args, **kw):
            entered.set()
            await release.wait()
            return "yes", None
        monkeypatch.setattr(v, "_stt_and_score", delayed)
        v.t.confirmation_id = "old"
        v.state = "CONFIRM"
        endpoint = SimpleNamespace(confirmation_id="old", audio=np.zeros(100, dtype=np.int16))
        task = asyncio.create_task(v._confirmed(v.t, "done", endpoint))
        await entered.wait()
        v.t.confirmation_id = "replacement"
        release.set()
        await task
        assert not brain.of("confirm_reply")


@pytest.mark.parametrize('state', ['THINK', 'IDLE'])
async def test_brain_correction_say_uses_brain_turn_id(tmp_path, monkeypatch, state):
    from heardback.voice import loop
    v = Voice(Config(), Link(tmp_path / 'unused.sock'), stt=FakeStt([]), tts=FakeTts(),
              cues=FakeCues(), media=FakeMedia(), prob=energy_prob)
    v.state, v.t.tid = state, None
    events = []
    monkeypatch.setattr(loop.trace, 'emit', lambda *a, **kw: events.append((a, kw)))
    monkeypatch.setattr(v, '_wait_brain', lambda *a: None)
    monkeypatch.setattr(v, '_bg', lambda coro: coro.close())
    monkeypatch.setattr(v, '_io_call', lambda *a, **kw: None)
    monkeypatch.setattr(v, '_set_busy', lambda *a: None)
    await v._on_message({'type': 'say', 'text': 'Correction reply.', 'turn': '20261108-120000-b1', 'final': True})
    says = [a for a, kw in events if a[:2] == ('voice', 'say')]
    assert says == [('voice', 'say', '20261108-120000-b1')]


# --- E6: states, transition table, deadlines --------------------------------------------------
def jump(v, s=1000.0):
    """Fake the state-deadline clock: `s` seconds later, every armed deadline is past."""
    import time
    v._now = lambda: time.monotonic() + s


@pytest.mark.parametrize("state", ["LISTEN", "THINK", "SPEAK", "SPEAK_PLAYBACK", "CONFIRM", "ANNOUNCE"])
async def test_stall_in_any_state_returns_to_idle_and_resumes_music(tmp_path, state):
    async with rig(tmp_path, ["switch to tmux window 2"]) as (v, brain):
        v.tts.finish_delay = 60          # a hung player: finish() never returns in time
        if state == "LISTEN":
            v.ptt()
        elif state == "ANNOUNCE":
            await brain.send({"type": "say", "text": "Build finished.", "announce": True})
        else:
            await turn_to_think(v, brain)
            if state in ("SPEAK", "SPEAK_PLAYBACK"):
                await brain.send({"type": "say", "text": "Working on it."})
            if state == "SPEAK_PLAYBACK":
                await brain.send({"type": "done"})
            if state == "CONFIRM":
                v.tts.finish_delay = 0
                await brain.send({"type": "confirm", "text": "Close 7 tabs?", "confirmation_id": "c1", "timeout_s": 30})
        want = "SPEAK" if state == "SPEAK_PLAYBACK" else state
        await until(lambda: v.state == want and v.media.calls == ["pause"])
        await asyncio.sleep(0.3)
        assert v.state == want                     # real time: nothing is due yet
        jump(v)
        await until(lambda: v.state == "IDLE" and v.media.calls == ["pause", "resume"])
        assert v.cues.played[-1] == "error"
        if state == "CONFIRM":
            assert (await brain.wait_for("confirm_reply"))["ok"] is False


async def test_illegal_moves_are_refused(tmp_path):
    v = Voice(Config(), Link(tmp_path / "unused.sock"), stt=FakeStt([]), tts=FakeTts(), cues=FakeCues(),
              media=FakeMedia(), prob=energy_prob)
    for bad in ("THINK", "CONFIRM"):
        assert v._go(bad) is False and v.state == "IDLE" and v._deadline is None
    assert v._go("LISTEN") and v._deadline is not None
    for bad in ("SPEAK", "CONFIRM", "ANNOUNCE", "LISTEN"):
        assert v._go(bad) is False and v.state == "LISTEN"
    assert v._go("IDLE") and v._deadline is None


async def test_confirm_left_open_by_a_barge_closes_when_the_next_turn_starts(tmp_path):
    async with rig(tmp_path, ["switch to tmux window 2", "switch to tmux window 3"]) as (v, brain):
        await turn_to_think(v, brain)
        v.tts.finish_delay = 1.0             # the read-back is still playing when the user barges
        await brain.send({"type": "confirm", "text": "Close 7 tabs?", "confirmation_id": "c1", "timeout_s": 8})
        await until(lambda: v.tts.said == ["Close 7 tabs?"])
        v.ptt()                              # turn 2 starts: c1 is answered now, not when the player ends
        reply = await brain.wait_for("confirm_reply", timeout=0.5)
        assert reply == {"type": "confirm_reply", "ok": False, "unasked": True, "confirmation_id": "c1"}
        assert v.state == "LISTEN" and brain.of("interrupt")
        v.tts.finish_delay = 0
        push_wav(v, CMD)
        second = await brain.wait_for("utterance", n=2)
        assert second["text"] == "switch to tmux window 3" and second["turn"] == v.t.tid
        await asyncio.sleep(1.2)             # the old read-back ends: c1 is not answered twice
        assert len(brain.of("confirm_reply")) == 1


async def test_local_line_is_a_speak_turn_push_to_talk_cuts_it(tmp_path):
    async with rig(tmp_path, ["switch to tmux window 2"]) as (v, brain):
        v.tts.finish_delay = 5
        v._speak_local("My brain is offline.")
        await until(lambda: v.state == "SPEAK")
        await brain.send({"type": "say", "text": "News.", "announce": True})
        await asyncio.sleep(0.1)
        assert v.tts.said == ["My brain is offline."] and list(v.pending) == ["News."]   # not over the line
        v.tts.finish_delay = 0
        v.ptt()                              # barge: speech stops before the mic opens
        assert v.state == "LISTEN" and v.tts.stopped == 1 and not brain.of("interrupt")
        push_wav(v, CMD)
        u = await brain.wait_for("utterance")
        assert u["text"] == "switch to tmux window 2"
