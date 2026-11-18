"""The voice state machine: IDLE → LISTEN → THINK → SPEAK (→ CONFIRM) → IDLE, plus IDLE → ANNOUNCE → IDLE.

Every move goes through `_go`, which checks MOVES and arms a wall-clock deadline in every state but IDLE:
a stuck mic, brain, model or player falls back to IDLE (music resumed) instead of wedging.

`feed` runs on the event loop thread (the daemon wraps the mic thread with call_soon_threadsafe);
model calls go to the GPU / CPU executors so the loop never blocks on them.
"""
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from concurrent.futures import Executor, ThreadPoolExecutor
from typing import Callable

import numpy as np

from jv import trace

from .audio_in import RATE, backoff
from .config import Config, log
from .dictation import Dictation, DictationError
from .desktop_dictation import DesktopDictation, DictationSlot
from .dictation_ctl import DictationCtl
from . import desktop_input
from .endpoint import Endpointer
from .link import Link
from .text import dangling, fix_mishear, match_confirm, match_repeat, norm
from .wake import BARGE_BOOST, DEBOUNCE_S, WakeGate

STUCK_S = 20.0
STT_TIMEOUT_S = 15.0   # STT is ~0.4 s on the GPU, a few s on the CPU fallback; past this, give up the turn
RESUME_DELAY_S = 0.5
ANNOUNCE_KEEP = 3
FOLLOWUP_WAIT_S = 6.0  # after a question: no speech this long → IDLE quietly
ANNOUNCE_GAP_S = 0.3   # an announcement has no `done`: finish after this long with no new `say`
KEEP_PAUSED = frozenset({"stop", "pause", "stop it", "pause it"})   # bare: the ducked music stays paused
GRACE_S = 2.0           # LISTEN / CONFIRM: past the endpointer's own deadline, the state deadline is a backstop
SPEECH_S_PER_CHAR = 0.1  # SPEAK / ANNOUNCE: queued text buys playback time (Kokoro speaks ~15 chars/s)

IDLE, LISTEN, THINK, SPEAK, CONFIRM, ANNOUNCE = "IDLE", "LISTEN", "THINK", "SPEAK", "CONFIRM", "ANNOUNCE"
MOVES = {   # legal moves; any state may go to IDLE
    IDLE: {LISTEN, SPEAK, ANNOUNCE},          # SPEAK from IDLE: a local line ("My brain is offline.")
    LISTEN: {THINK},
    THINK: {LISTEN, SPEAK, CONFIRM},          # LISTEN: a dangling head, or a barge
    SPEAK: {LISTEN, SPEAK, CONFIRM},
    CONFIRM: {THINK, CONFIRM},                # CONFIRM again: push-to-talk restarts the yes/no listen
    ANNOUNCE: {LISTEN},
}


@dataclass
class VTurn:
    """Everything one turn owns. A newer turn replaces the object: `t is not self.t` means "superseded"."""
    n: int = 0
    tid: str | None = None       # trace turn id, sent with the utterance (docs/observability.md)
    parent: str | None = None    # a follow-up's id points at the turn that asked
    ok: bool = True              # trusted (push-to-talk, or passed the speaker check)
    followup: bool = False       # this turn is the answer to Claude's question
    local: bool = False          # a line spoken by voice itself, no brain turn behind it
    open: bool = False           # a turn_end is still owed for tid
    outcome: str = "done"
    marks: dict = field(default_factory=dict)
    head: tuple[str, float, bool] | None = None   # (text, audio_s, speaker_ok) of a dangling head
    expect: bool = False         # the brain's final say asked for a reply
    ann_expect: bool = False     # an announcement asked (a waiting alert read a dialog out): listen after it
    last_said: str = ""
    confirmation_id: str | None = None
    confirmation_deadline: float | None = None
    confirm_s: float | None = None   # a confirm is being read back or listened for (its timeout)
    readback: list | None = None     # [confirmation_id] while a read-back plays; closed by the next turn
    talk_end: float = 0.0            # when the text queued so far should have finished playing


class Voice(DictationCtl):
    def __init__(self, cfg: Config, link: Link, *, stt, tts, cues, media, prob: Callable[[np.ndarray], float],
                 wake=None, speaker=None, stuck_s: float = STUCK_S, stt_timeout_s: float = STT_TIMEOUT_S,
                 gpu: Executor | None = None, cpu: Executor | None = None):
        self.cfg, self.link = cfg, link
        self.stt, self.tts, self.cues, self.media, self.prob = stt, tts, cues, media, prob
        self.wake, self.speaker, self.stuck_s, self.stt_timeout_s = wake, speaker, stuck_s, stt_timeout_s
        self.gate = WakeGate(cfg.wake_threshold) if wake is not None else None
        self.wake_audio = np.zeros(0, np.int16)
        self.gpu = gpu or ThreadPoolExecutor(1)
        self.cpu = cpu or ThreadPoolExecutor(1)
        self._io = ThreadPoolExecutor(1)      # playerctl / notify-send: blocking, kept in order
        self._dictation_io = ThreadPoolExecutor(1, "heardback-dictation-io")
        tmux_dictation = Dictation(stt, self.gpu, prob, event=self._dictation_event)
        self.desktop = DesktopDictation(stt, self.gpu, prob, desktop_input, self._dictation_io,
                                        settings=cfg, event=self._desktop_event)
        self.dictation = DictationSlot(tmux_dictation, self.desktop)
        self._desktop_start_task: asyncio.Task | None = None
        self._desktop_reset_task: asyncio.Task | None = None
        self._desktop_epoch = 0
        self._dictation_press_at = float("-inf")
        self._desktop_notified = None
        self._desktop_discarded = 0
        self._dictation_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._dictation_notified: tuple[str | None, str, str | None] | None = None
        self._dictation_mode_active = False
        self._mic_available = False
        self.mic_available_probe: Callable[[], bool] = lambda: True
        self._shutting_down = False
        self._wake_gate_pending = 0
        self._wake_miss_peak = 0.0
        self._wake_miss_state = None
        self._wake_miss_last = float("-inf")
        self._wake_miss_gate_at = float("-inf")
        self._speaker_reject_last = float("-inf")
        self._local_speech_count = 0
        self.state = IDLE
        self.t = VTurn()
        self._now: Callable[[], float] = time.monotonic   # the state-deadline clock (tests fake it)
        self._source: tuple[str, float | None] = ("ptt", None)   # what started the next turn
        self.utterances = 0
        self._limit_notice: str | None = None   # queued with announcements; dropped when a new listen starts
        self.ep: Endpointer | None = None
        self._ep_phase: str | None = None   # listen / followup / continuation / confirm, for endpoint telemetry
        self._spare: Endpointer | None = None   # listens on while STT runs, in case the transcript is a dangling head
        self._busy = False
        self.pending: deque[str] = deque(maxlen=ANNOUNCE_KEEP)
        self._ann_last = 0.0
        self._ann_task: asyncio.Task | None = None
        self._delivery_task: asyncio.Task | None = None
        self._deadline: float | None = None   # the state deadline (`_go`, `_wait_brain`); None in IDLE
        self._fatal_error: Exception | None = None
        self.mic_on = True
        self.automute = False
        self.on_mic_toggle: Callable[[bool], None] = lambda on: None
        self.on_mic_lost_fallback: Callable[[], None] = lambda: None

    @property
    def state(self) -> str:
        return self._state

    @state.setter
    def state(self, s: str) -> None:
        """Every state change stamps `state_since`: the dashboard's live state reads both (no polling, no lock)."""
        if s != getattr(self, "_state", None):
            self._state, self.state_since = s, time.time()

    def _go(self, s: str, limit: float | None = None) -> bool:
        """The one checked way to change state. Refuses an illegal move (False); arms the state deadline."""
        if s != IDLE and s not in MOVES[self.state]:
            log("voice_error", error="illegal_move", frm=self.state, to=s)
            self._tr("illegal_move", frm=self.state, to=s)
            return False
        self.state = s
        if s == IDLE:
            self._deadline = None
        elif limit is not None:
            self._deadline = self._now() + limit
        elif s == LISTEN:
            self._deadline = self._now() + FOLLOWUP_WAIT_S + self.cfg.max_utterance_s + GRACE_S
        else:
            self._wait_brain()
        return True

    def _new_turn(self, **fields) -> VTurn:
        """Start turn n+1. A read-back the old turn left playing is answered now: the brain stops waiting."""
        old = self.t
        if old.readback is not None:
            self._send({"type": "confirm_reply", "ok": False, "unasked": True, "confirmation_id": old.readback[0]})
            old.readback.clear()             # the read-back's own late check sees it answered
            old.readback = None
        self.t = VTurn(n=old.n + 1, **fields)
        return self.t

    def _brain_turn(self) -> bool:
        """THINK / SPEAK for a brain turn (not a local line)."""
        return self.state in (THINK, SPEAK) and not self.t.local

    # --- helpers ---------------------------------------------------------------------------
    def _bg(self, coro) -> asyncio.Task:
        t = asyncio.get_running_loop().create_task(coro)
        t.add_done_callback(self._bg_done)
        return t

    def _bg_done(self, t: asyncio.Task) -> None:
        if t.cancelled() or not (e := t.exception()):
            return
        log("voice_error", error=repr(e))
        trace.error("voice", self.t.tid, f"bg {t.get_coro().__qualname__}", e)

    def _tr(self, ev: str, **fields) -> None:
        trace.emit("voice", ev, self.t.tid, **fields)

    def _endpoint_event(self, ep: Endpointer, phase: str, *, timeout_s: float | None = None) -> None:
        fields = {"phase": phase, "status": ep.status, "reason": ep.reason,
                  "audio_s": round(len(ep.audio) / RATE, 2)}
        if phase == "confirm":
            fields["timeout_s"] = timeout_s
        else:
            fields["max_utterance_s"] = self.cfg.max_utterance_s
        self._tr("endpoint", **fields)

    def _drop_pending_limit_notice(self) -> None:
        if self._limit_notice is None:
            return
        try:
            self.pending.remove(self._limit_notice)
        except ValueError:
            pass
        self._limit_notice = None

    def _end_turn(self, outcome: str | None = None) -> None:
        """One `turn_end` per turn (outcome: done/timeout/nothing/error/interrupted) with its latencies."""
        if not self.t.open:
            return
        self.t.open = False
        m, now = self.t.marks, time.monotonic()
        rel = lambda a, b: None if a is None or b is None else round(b - a, 3)
        fa = self.tts.first_audio_at
        self._tr("turn_end", outcome=outcome or self.t.outcome, total_s=rel(m.get("t_listen"), now),
                 stt_s=rel(m.get("t_end"), m.get("t_stt")),
                 first_audio_s=rel(m.get("t_end"), fa) if fa and fa >= m.get("t_end", now + 1) else None)

    def _io_call(self, fn, *a) -> None:
        asyncio.get_running_loop().run_in_executor(self._io, fn, *a)

    def _new_tid(self, suffix: str = "") -> str:
        return f"{time.strftime('%Y%m%d-%H%M%S')}-{self.t.n}{suffix}"

    def _send(self, msg: dict) -> bool:
        return self.link.post(msg)

    def _set_busy(self, busy: bool) -> None:
        if busy != self._busy:
            self._busy = busy
            self._send({"type": "state", "busy": busy})

    # --- inputs ----------------------------------------------------------------------------
    def _speak_local(self, text: str) -> None:
        """A line of voice's own. From IDLE it is a SPEAK turn, so push-to-talk barges (stops it) before listening."""
        self._local_speech_count += 1
        async def go():
            t = None
            try:
                if self.state == IDLE and not self.dictation.engaged and self._go(SPEAK):
                    t = self._new_turn(tid=self.t.tid, local=True)
                    self._wait_brain(text)
                await self.tts.say(text)
                await self.tts.finish()
            finally:
                self._local_speech_count = max(0, self._local_speech_count - 1)
                if t is not None and t is self.t and self.state == SPEAK:
                    self._idle()
        self._bg(go())

    def ptt(self) -> None:
        self.desktop.shortcut_pressed()
        self._desktop_epoch += 1
        if self.desktop.engaged:
            self.desktop.suspend("ptt")
        elif self.dictation.engaged:
            if self.dictation.phase == "listening":
                try:
                    self.dictation.pause(self.dictation.metadata()["session_id"])
                except DictationError:
                    pass
            else:
                self._io_call(self.cues.notify, "Dictation is paused or ready for review")
        elif not self.mic_on:
            self._io_call(self.cues.notify, "Mic off (Super+Shift+C)")
        elif self.state == CONFIRM and self.t.confirm_s is not None and self.link.up:
            self.tts.stop()
            self._tr("confirm_ptt", state=self.state)
            self._confirm_listen(self.t.confirm_s)
        else:
            self._source = ("ptt", None)
            self._start(turn_ok=True)

    def cancel(self) -> None:
        """Stop everything in flight (speech, brain turn, listen, confirm) and go idle. A confirm is answered "no"."""
        self.desktop.shortcut_pressed()
        self._desktop_epoch += 1
        if self.desktop.engaged:
            self.desktop.cancel()
            return
        if self.dictation.engaged:
            try:
                self.dictation.cancel(self.dictation.metadata()["session_id"])
            except DictationError as e:
                if e.code == "insertion_in_progress":
                    self._io_call(self.cues.notify,
                                  "Dictation paste may still complete; check status before retrying")
                return
            self._dictation_leave()
            return
        self._drop_pending_limit_notice()
        if self.state == IDLE:
            return
        self._tr("cancel", state=self.state)
        if self.state in (LISTEN, CONFIRM):
            self._abort_listen()
        else:
            self.tts.stop()
            if self._brain_turn():
                self._send({"type": "interrupt", "t": time.time()})
            self._end_turn("interrupted")
            self._idle()
        self.tts.stop()

    def _start(self, turn_ok: bool) -> None:
        if self.dictation.engaged or self._shutting_down:
            return
        if not self.link.up:
            if self.state == IDLE:
                self._bg(self._offline(turn_ok))
        elif self.state in (IDLE, THINK, SPEAK, ANNOUNCE):
            self._barge(turn_ok)

    def _woke(self, score: float) -> None:
        """Wake turns are untrusted (turn_ok False); with a speaker check they pass its gates instead."""
        audio = self.wake.ring_audio(2.0)
        self.wake.reset()
        if self.speaker is None:
            self.wake_audio = audio
            log("wake", score=round(score, 3), gate="pass", state=self.state)
            self._source = ("wake", round(score, 3))
            return self._start(turn_ok=False)
        self._wake_gate_pending += 1
        self._bg(self._wake_gate(score, audio, self.state, self.t))

    async def _wake_gate(self, score: float, audio: np.ndarray, state: str, turn: VTurn) -> None:
        try:
            spk = await asyncio.get_running_loop().run_in_executor(
                self.cpu, self.speaker.score, audio[-int(1.5 * RATE):])
            if self.dictation.engaged or self._shutting_down or turn is not self.t or \
                    self.state in (LISTEN, CONFIRM):
                return       # a push-to-talk turn or dictation start happened meanwhile
            if spk < self.cfg.speaker_wake:
                trace.emit("voice", "reject", None, why="speaker_wake", wake_score=round(score, 3),
                           spk=round(spk, 3))
                if time.monotonic() - self._speaker_reject_last >= 3.0:
                    self._speaker_reject_last = time.monotonic()
                    self.cues.play("nothing")
                return log("wake", score=round(score, 3), gate="speaker_reject", spk=round(spk, 3), state=state)
            self.wake_audio = audio
            log("wake", score=round(score, 3), gate="pass", spk=round(spk, 3), state=state)
            self._source = ("wake", round(score, 3))
            self._start(turn_ok=self.cfg.speaker_check == "wake")   # "wake": the wake score is the only check
        finally:
            self._wake_gate_pending = max(0, self._wake_gate_pending - 1)

    def _gated(self) -> bool:
        return self.speaker is not None and not self.t.ok and self.cfg.speaker_check == "on"

    async def _stt_and_score(self, audio: np.ndarray, spk_audio: np.ndarray,
                             also: bool = False) -> tuple[str, float | None] | None:
        """STT on the GPU pool and, for gated turns, the speaker score on the CPU pool, in parallel.
        None if either model raised. A timeout restarts voice: its executor thread may be hung."""
        loop = asyncio.get_running_loop()
        marks = self.t.marks

        def score():
            s = self.speaker.score(spk_audio)
            marks["t_spk"] = time.monotonic()
            return s
        async def both():
            stt = loop.run_in_executor(self.gpu, self.stt.transcribe, audio)
            if not (self._gated() or also):
                return await stt, None
            return tuple(await asyncio.gather(stt, loop.run_in_executor(self.cpu, score)))
        try:
            return await asyncio.wait_for(both(), self.stt_timeout_s)
        except TimeoutError:
            log("voice_error", turn=self.t.n, error="stt_timeout", timeout_s=self.stt_timeout_s)
            self._tr("stt_timeout", timeout_s=self.stt_timeout_s)
            self._fatal_error = RuntimeError("STT timed out; executor may be hung")
            return None
        except Exception as e:
            log("voice_error", turn=self.t.n, error=repr(e))
            trace.error("voice", self.t.tid, "stt", e)
            return None

    def _barge(self, turn_ok: bool) -> None:
        """Start a turn, cutting off whatever is speaking or thinking."""
        if self.state in (THINK, SPEAK, ANNOUNCE):
            self.tts.stop()
            if self._brain_turn():
                self._send({"type": "interrupt", "t": time.time()})
                self._tr("barge", state=self.state)
            self._end_turn("interrupted")
            self._log_turn()
        self._listen(turn_ok)

    async def _offline(self, turn_ok: bool) -> None:
        if await self.link.connect():
            if self.state == IDLE and not self.dictation.engaged and not self._shutting_down:
                self._listen(turn_ok)
            return
        if self.dictation.engaged or self._shutting_down:
            return
        self.cues.play("error")
        self._speak_local("My brain is offline.")

    def toggle_mic(self) -> None:
        self.desktop.shortcut_pressed()
        self._desktop_epoch += 1
        self.mic_on = not self.mic_on
        if not self.mic_on:
            self._mic_available = False
            self._dictation_suspend("mic_off")
            self._abort_listen()
        self._io_call(self.cues.notify, "Mic on" if self.mic_on else "Mic off")
        self.on_mic_toggle(self.mic_on)

    def set_automute(self, on: bool) -> None:
        if on != self.automute:
            log("automute", on=on)
        self.automute = on
        if on:
            self._desktop_epoch += 1
            self._dictation_suspend("automute")

    def mic_lost(self) -> None:
        self.set_mic_available(False)
        if self.dictation.engaged:
            return
        if self.state not in (LISTEN, CONFIRM):
            return
        self._abort_listen()
        self.cues.play("error")
        self._speak_local("I lost the mic, say that again.")
        self.on_mic_lost_fallback()

    def _abort_listen(self) -> None:
        if self.state == CONFIRM:
            self._send({"type": "confirm_reply", "ok": False, "text": "", "speaker_ok": self.t.ok,
                        "confirmation_id": self.t.confirmation_id, "t": time.time()})
        if self.state in (LISTEN, CONFIRM):
            self.t.outcome = "interrupted"
            self._idle()

    def _brain_lost(self) -> None:
        if self.dictation.engaged:
            return
        if self.state in (IDLE, LISTEN) or self.t.local:
            return
        log("brain_lost", state=self.state, turn=self.t.n)
        self._tr("brain_lost", state=self.state)
        self.t.outcome = "error"
        self.tts.stop()
        self.pending.clear()
        self._idle()
        self.cues.play("error")
        self._speak_local("My brain is offline.")

    def feed(self, frame: np.ndarray) -> None:
        if self._shutting_down:
            return
        if self.dictation.engaged:
            if not self.mic_on or self.automute or not self._mic_available or not self.mic_available_probe():
                reason = "mic_off" if not self.mic_on else "automute" if self.automute else "device_loss"
                self._dictation_suspend(reason)
            else:
                if self.desktop.engaged:
                    self.desktop.feed(frame)
                else:
                    self.dictation.feed(frame)
            return
        if self.gate is not None and self.mic_on and not self.automute:
            score = self.wake.score(frame)
            boost = BARGE_BOOST if self.state in (THINK, SPEAK, ANNOUNCE) else 0.0
            now = time.monotonic()
            eligible = self.state in (IDLE, THINK, SPEAK, ANNOUNCE)
            fired = eligible and self.gate.update(score, now, boost)
            if fired:
                self._wake_miss_peak = 0.0
                self._wake_miss_state = None
                self._wake_miss_gate_at = now
                return self._woke(score)
            if not eligible or now - self._wake_miss_gate_at < DEBOUNCE_S:
                self._wake_miss_peak = 0.0
                self._wake_miss_state = None
            elif score >= self.cfg.wake_miss_floor:
                self._wake_miss_peak = max(score, self._wake_miss_peak)
                self._wake_miss_state = self.state
            elif self._wake_miss_peak:
                now = time.monotonic()
                if now - self._wake_miss_last >= 10.0:
                    trace.emit("voice", "wake_near_miss", None, peak=round(self._wake_miss_peak, 3),
                               state=self._wake_miss_state)
                    self._wake_miss_last = now
                self._wake_miss_peak = 0.0
                self._wake_miss_state = None
        if self.state in (LISTEN, CONFIRM) and self.ep is not None:
            ep = self.ep
            status = ep.feed(frame)
            if status in ("done", "timeout"):
                phase = self._ep_phase or ("confirm" if self.state == CONFIRM else "listen")
                self._endpoint_event(ep, phase, timeout_s=self.t.confirm_s)
                self.ep, self._ep_phase, self.t.confirm_s = None, None, None
                if self.state == LISTEN and status == "done" and ep.reason != "max_duration" and self.t.head is None:
                    self._spare = Endpointer(self.prob, end_silence=self.cfg.end_silence,
                                             max_s=self.cfg.max_utterance_s, reset=False)
                self._bg(self._listened(self.t, self.state, status, ep))
        elif self._spare is not None:
            spare = self._spare
            if spare.reason is None:
                status = spare.feed(frame)
                if status in ("done", "timeout"):
                    self._endpoint_event(spare, "continuation_buffer")

    # --- transitions -----------------------------------------------------------------------
    def _listen(self, turn_ok: bool, followup: bool = False) -> None:
        if not self._go(LISTEN):
            return
        self._drop_pending_limit_notice()
        t = self._new_turn(parent=self.t.tid if followup else None, ok=turn_ok, followup=followup, open=True,
                           marks={"t_listen": time.monotonic()})
        t.tid = self._new_tid()
        self._spare = None
        if not followup:
            self._tr("wake", source=self._source[0], score=self._source[1])
        self._tr("listen", followup=followup, parent=self.t.parent)
        self._set_busy(True)
        if self.cfg.duck_media:
            self._io_call(self.media.pause, self.t.tid)
        self.cues.play("wake")
        if followup:     # the question stays on screen; the reply is scored on its own
            self.wake_audio = np.zeros(0, np.int16)
            self._ep_phase = "followup"
            self.ep = Endpointer(self.prob, end_silence=self.cfg.end_silence, wait_s=FOLLOWUP_WAIT_S,
                                 max_s=self.cfg.max_utterance_s, short_silence=self.cfg.short_silence or None)
            return
        self._io_call(self.cues.notify, "Listening…")
        self._ep_phase = "listen"
        self.ep = Endpointer(self.prob, end_silence=self.cfg.end_silence, max_s=self.cfg.max_utterance_s,
                             short_silence=self.cfg.short_silence or None)

    async def _listened(self, turn: VTurn, state: str, status: str, ep: Endpointer) -> None:
        if state == CONFIRM:
            return await self._confirmed(turn, status, ep)
        if ep.reason == "max_duration":
            if turn is not self.t or self.state != state:
                return
            if self.t.followup:
                log("followup", turn=turn.n, outcome="timeout")
                self._tr("followup", outcome="timeout", parent=self.t.parent)
            self.t.head = self._spare = None
            self.t.outcome = "timeout"
            notice = (f"Listening reached its {self.cfg.max_utterance_s:g}-second limit. "
                      "Please try shorter parts.")
            self._limit_notice = notice
            self.pending.append(notice)
            self.cues.play("nothing")
            self._io_call(self.cues.notify, notice)
            self._idle()
            return
        head, self.t.head = self.t.head, None
        if self.t.followup and head is None:
            log("followup", turn=turn.n, outcome="reply" if status == "done" else "timeout")
            self._tr("followup", outcome="reply" if status == "done" else "timeout", parent=self.t.parent)
        if status == "timeout" and head is None:
            if not self.t.followup:
                self.cues.play("nothing")
            self.t.outcome = "nothing"
            return self._idle()
        if not self._go(THINK):
            return
        self.t.marks["t_end"] = time.monotonic()
        if status == "timeout":          # the extension heard nothing: send the head alone (Claude asks)
            text, spk, audio_s = "", None, 0.0
            self.t.marks.pop("t_stt", None)
        else:
            res = await self._stt_and_score(ep.audio, np.concatenate([self.wake_audio, ep.audio]))
            self.t.marks["t_stt"] = time.monotonic()
            if turn is not self.t or self.state != THINK:
                return  # barged or reset meanwhile
            if res is None:
                self.cues.play("error")
                self.t.outcome = "error"
                return self._idle()
            (text, spk), audio_s = res, len(ep.audio) / RATE
        speaker_ok = self.t.ok if spk is None else spk >= self.cfg.speaker_utt
        spare, self._spare = self._spare, None
        if head is None and text and speaker_ok and dangling(text):   # keep listening once (bounded: no 2nd)
            self.t.head = (text, audio_s, speaker_ok)
            self._go(LISTEN)
            self.ep = spare or Endpointer(self.prob, end_silence=self.cfg.end_silence, max_s=self.cfg.max_utterance_s)
            self._ep_phase = "continuation"
            if self.ep.status in ("done", "timeout"):    # the rest was already said while STT ran
                ep, self.ep = self.ep, None
                self._ep_phase = None
                self._bg(self._listened(turn, LISTEN, ep.status, ep))
            return
        m = self.t.marks
        joined = None
        if head is not None:
            joined = [head[0], text] if text else [head[0]]
            text, audio_s, speaker_ok = " ".join(joined), audio_s + head[1], speaker_ok and head[2]
        raw_text = text
        text = fix_mishear(text, self.cfg.stt_fixes)
        self._tr("heard", text=text, **({"raw": raw_text} if raw_text != text else {}), audio_s=round(audio_s, 2),
                 stt_ms=round((m["t_stt"] - m["t_end"]) * 1000) if "t_stt" in m else None,
                 spk=None if spk is None else round(spk, 3), **({"joined": joined} if joined else {}))
        if spk is not None and not speaker_ok:
            log("utt_reject", turn=turn.n, score=round(spk, 3))
            self._tr("reject", why="speaker", spk=round(spk, 3), need=self.cfg.speaker_utt)
            self.cues.play("nothing")
            self.t.outcome = "nothing"
            return self._idle()
        if not text:
            self._tr("reject", why="empty_stt")
            self.cues.play("nothing")
            self.t.outcome = "nothing"
            return self._idle()
        if self.cfg.log_text:
            log("heard", turn=turn.n, text=text)
        if norm(text) in KEEP_PAUSED:
            self._io_call(self.media.keep)
        self.utterances += 1
        self._wait_brain()
        msg = {"type": "utterance", "text": text, "speaker_ok": speaker_ok, "t": time.time(), "turn": self.t.tid}
        if self.t.parent:
            msg["parent"] = self.t.parent
        if not self._send(msg):
            self._brain_lost()
        elif not speaker_ok:
            self._tr("reject", why="untrusted")   # wake turn without a speaker check: the brain drops it
            self._idle()     # the brain drops untrusted utterances without a `done`

    async def _confirmed(self, turn: VTurn, status: str, ep: Endpointer) -> None:
        confirmation_id = getattr(ep, "confirmation_id", None)
        if turn is not self.t or confirmation_id != self.t.confirmation_id or not self._go(THINK):
            return
        text, speaker_ok, spk = "", not self._gated(), None
        if status == "done":
            # "wake" mode: score the yes too, log only (user 2026-11-02 rule: nothing enforced)
            res = await self._stt_and_score(ep.audio, ep.audio,
                                            also=self.speaker is not None and self.cfg.speaker_check == "wake")
            if turn is not self.t or self.state != THINK or confirmation_id != self.t.confirmation_id:
                return
            text, spk = res or ("", None)     # a model error answers "no": the brain waits on this reply
            raw_text = text
            text = fix_mishear(text, self.cfg.stt_fixes)
            if res is None:
                speaker_ok = False
            elif spk is not None:
                would = spk >= self.cfg.speaker_utt
                if self._gated():
                    speaker_ok = would
                log("confirm_spk", turn=turn.n, score=round(spk, 3), ok=would, enforced=self._gated())
        ok = bool(text) and match_confirm(text) is True and speaker_ok
        if self.cfg.log_text:
            log("confirm_heard", turn=turn.n, text=text, ok=ok)
        answer = ("timeout" if status != "done" or not text else "untrusted" if not speaker_ok else
                  "repeat" if match_repeat(text) else "yes" if ok else "no" if match_confirm(text) is False else "other")
        self._tr("confirm_heard", text=text, **({"raw": raw_text} if status == "done" and raw_text != text else {}),
                 answer=answer, confirmation_id=confirmation_id,
                 speaker_ok=speaker_ok, **({"spk": round(spk, 3)} if spk is not None else {}))
        self._wait_brain()
        if not self._send({"type": "confirm_reply", "ok": ok, "text": text, "speaker_ok": speaker_ok,
                           "confirmation_id": confirmation_id,
                           "t": time.time(), "turn": self._new_tid("c"), "parent": self.t.tid}):
            self._brain_lost()

    def _confirm_listen(self, timeout: float) -> None:
        if self.t.confirmation_deadline is not None:
            timeout = min(timeout, max(0.001, self.t.confirmation_deadline - time.monotonic()))
        if not self._go(CONFIRM, timeout + GRACE_S):
            return
        self.cues.play("tick")
        self._ep_phase = "confirm"
        self.ep = Endpointer(self.prob, end_silence=0.5, wait_s=timeout, max_s=timeout)
        self.ep.deadline = time.monotonic() + timeout
        self.ep.confirmation_id = self.t.confirmation_id

    def _wait_brain(self, text: str = "") -> None:
        """Re-arm the deadline: stuck_s past the end of the speech queued so far (each `text` adds playback time)."""
        now, t = self._now(), self.t
        t.talk_end = max(now, t.talk_end) + SPEECH_S_PER_CHAR * len(text)
        self._deadline = t.talk_end + self.stuck_s

    def _stall(self) -> None:
        """A state outlived its deadline: answer an open yes/no "no", stop speech, back to IDLE (music resumes)."""
        log("stuck", turn=self.t.n)
        self._tr("stuck", state=self.state, after_s=self.stuck_s)
        if self.state == CONFIRM:
            self._send({"type": "confirm_reply", "ok": False, "text": "", "speaker_ok": self.t.ok,
                        "confirmation_id": self.t.confirmation_id, "t": time.time()})
        self.t.outcome = "timeout"
        self.tts.stop()
        self.cues.play("error")
        self._idle()

    def _idle(self) -> None:
        if self.dictation.engaged:
            return
        self.ep, self._ep_phase, self.t.confirm_s, self.t.ann_expect = None, None, None, False
        self.t.confirmation_id, self.t.confirmation_deadline = None, None
        self._spare = self.t.head = None
        if self.state != IDLE:
            self._end_turn()
            self._log_turn()
        self._go(IDLE)
        if self.pending:                     # queued announcements play before going idle (busy stays)
            return self._announce(self.pending.popleft())
        self._set_busy(False)
        if self.cfg.duck_media:
            self._bg(self._resume_later(self.t, self.t.tid))

    async def _resume_later(self, turn: VTurn, tid: str | None) -> None:
        await asyncio.sleep(RESUME_DELAY_S)
        if self.state == IDLE and turn is self.t and not self.dictation.engaged:
            self._io_call(self.media.resume, tid)

    def _log_turn(self) -> None:
        m = self.t.marks
        if "t_end" not in m:
            return
        rel = lambda t: None if t is None else round(t - m["t_end"], 3)
        log("turn", turn=self.t.n, t_end=0.0, t_stt=rel(m.get("t_stt")), t_spk=rel(m.get("t_spk")),
            t_first_audio=rel(self.tts.first_audio_at if self.tts.first_audio_at and
                              self.tts.first_audio_at >= m["t_end"] else None))
        self.t.marks = {}

    # --- brain messages --------------------------------------------------------------------
    def _announce(self, text: str, tid: str | None = None) -> None:
        """Start (or extend) an announcement from IDLE."""
        if self.dictation.engaged:
            self.pending.append(text)
            return
        if text == self._limit_notice:
            self._limit_notice = None
        if self.state == IDLE:
            if not self._go(ANNOUNCE):
                return
            self._new_turn(tid=self.t.tid)
            self._set_busy(True)
            if self.cfg.duck_media:
                self._io_call(self.media.pause, None)
        self._wait_brain(text)
        self._ann_last = time.monotonic()
        trace.emit("voice", "say", tid, text=text, announce=True)
        self._bg(self.tts.say(text))
        if self._ann_task is None or self._ann_task.done():
            self._ann_task = self._bg(self._finish_announcement(self.t))

    async def _finish_announcement(self, turn: VTurn) -> None:
        while (left := self._ann_last + ANNOUNCE_GAP_S - time.monotonic()) > 0:
            await asyncio.sleep(left)
        self._ann_task = None
        await self.tts.finish()
        if turn is self.t and self.state == ANNOUNCE:
            if self.dictation.engaged:
                return
            if self.t.ann_expect and self.mic_on and self.cfg.follow_up and not self.pending:
                self.t.ann_expect = False
                return self._listen(True, followup=True)
            self._idle()

    async def _on_message(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "say" and "delivery_id" in msg:
            if self._delivery_task is not None and not self._delivery_task.done():
                self._send({"type": "delivery", "id": msg["delivery_id"], "result": "refused"})
            else:
                self._delivery_task = self._bg(self._deliver(msg))
            return
        if self.dictation.engaged:
            if kind == "say" and msg.get("announce"):
                self.pending.append(str(msg.get("text") or ""))
            elif kind == "confirm":
                self._send({"type": "confirm_reply", "ok": False, "unasked": True,
                            "confirmation_id": msg.get("confirmation_id")})
            return
        if kind == "say":
            text = str(msg.get("text") or "")
            ask = msg.get("announce") and msg.get("expect_reply") and self.state in (IDLE, ANNOUNCE)
            if self.state == ANNOUNCE and self._ann_task is not None:
                self._announce(text, msg.get("turn"))
            elif self._brain_turn() and not msg.get("announce"):
                self._go(SPEAK)
                self._wait_brain(text)
                if msg.get("expect_reply") and self.cfg.follow_up:
                    self.t.expect = True
                    self._io_call(self.cues.notify, text or self.t.last_said, True)   # sticky
                self.t.last_said = text or self.t.last_said
                trace.emit("voice", "say", msg.get("turn") or self.t.tid, text=text,
                           expect_reply=bool(msg.get("expect_reply")), final=bool(msg.get("final")))
                await self.tts.say(text)
            elif self.state == IDLE:
                self._announce(text, msg.get("turn"))
            else:
                self.pending.append(text)    # never spoken inside a turn
            if ask:
                self.t.ann_expect = True
        elif kind == "done" and self._brain_turn():
            turn = self.t      # the deadline stays armed: playback past it is a stall
            await self.tts.finish()
            if turn is self.t and self.state in (THINK, SPEAK):
                if self.t.expect and self.mic_on:      # Claude asked a question: listen for the answer, same trust
                    self._end_turn("done")
                    self._log_turn()
                    return self._listen(self.t.ok, followup=True)
                self._idle()
        elif kind == "confirm" and self._brain_turn():
            turn = self.t
            timeout = float(msg.get("timeout_s") or 8)
            confirmation_id = self.t.confirmation_id = msg.get("confirmation_id")
            remaining = msg.get("remaining_s")
            self.t.confirmation_deadline = time.monotonic() + float(remaining) if remaining is not None else None
            self._tr("confirm_asked", text=str(msg.get("text") or ""), timeout_s=timeout,
                     confirmation_id=self.t.confirmation_id)
            self.t.confirm_s = timeout
            readback = str(msg.get("text") or "")
            self._wait_brain(readback)
            rb = turn.readback = [confirmation_id]   # emptied if the next turn answers it first
            await self.tts.say(readback)   # in full: you hear exactly what you confirm (2026-11-17)
            await self.tts.finish()
            if turn.readback is rb:
                turn.readback = None
            if turn is not self.t or turn.confirm_s is None or self.state not in (THINK, SPEAK):
                if rb and (turn is not self.t or turn.confirmation_id != confirmation_id):
                    self._send({"type": "confirm_reply", "ok": False, "unasked": True,
                                "confirmation_id": confirmation_id})
                return     # barged, reset, or push-to-talk already started (or finished) the listen
            self._confirm_listen(timeout)
        elif kind == "confirm":
            self._send({"type": "confirm_reply", "ok": False, "unasked": True,
                        "confirmation_id": msg.get("confirmation_id")})

    async def _deliver(self, msg: dict) -> None:
        """A reminder is acknowledged directly, never queued or dispatched as input."""
        reply = {"type": "delivery", "id": msg["delivery_id"], "result": "refused"}
        if (self.state != IDLE or self.dictation.engaged or self.automute
                or self._shutting_down or self._local_speech_count or self.tts.speaking):
            self._send(reply)
            return
        self._go(ANNOUNCE)
        turn = self._new_turn(tid=self.t.tid)
        self._wait_brain(str(msg.get("text") or ""))
        self._set_busy(True)
        if self.cfg.duck_media:
            self._io_call(self.media.pause, None)
        start = time.monotonic()
        try:
            await self.tts.say(str(msg.get("text") or ""))
            await self.tts.finish()
        except Exception as exc:
            log("delivery_failed", error=type(exc).__name__)
        audio = self.tts.first_audio_at
        played = self.tts.player_returncode == 0 or self.tts.stopped
        reply["result"] = "spoken" if audio is not None and audio > start and played else "failed"
        trace.emit("voice", "say", None, text=str(msg.get("text") or ""), announce=True, delivery=reply["result"])
        self._send(reply)
        if turn is self.t and self.state == ANNOUNCE:
            self._idle()

    async def _stuck_watch(self) -> None:
        tick = min(0.1, self.stuck_s / 4)
        while True:
            await asyncio.sleep(tick)
            if self._fatal_error is not None:
                raise self._fatal_error
            if self.ep is not None and self.state in (LISTEN, CONFIRM) and \
                    time.monotonic() >= self.ep.deadline:
                ep, self.ep = self.ep, None
                phase, self._ep_phase = self._ep_phase, None
                ep.status, ep.reason = "timeout", "wall_timeout"
                self.t.head = self._spare = None
                self._endpoint_event(ep, phase or "listen", timeout_s=self.t.confirm_s)
                self._bg(self._listened(self.t, self.state, "timeout", ep))
            elif self._deadline is not None and self.state != IDLE and self._now() > self._deadline:
                self._stall()

    async def _reader(self) -> None:
        n = 0
        while True:
            if not self.link.up:
                if not await self.link.connect():
                    await asyncio.sleep(backoff(n))
                    n += 1
                    continue
                n = 0
                if self._busy:
                    self._send({"type": "state", "busy": True})   # a restarted brain doesn't know
            msg = await self.link.recv()
            if msg is None:
                self._brain_lost()       # the utterance in flight is never resent
            else:
                await self._on_message(msg)

    async def run(self) -> None:
        watch = asyncio.get_running_loop().create_task(self._stuck_watch())
        reader = asyncio.get_running_loop().create_task(self._reader())
        try:
            await asyncio.gather(reader, watch)
        finally:
            watch.cancel()
            reader.cancel()
            await asyncio.gather(reader, watch, return_exceptions=True)
            self.tts.stop()
            if self._delivery_task is not None:
                self._delivery_task.cancel()
                await asyncio.gather(self._delivery_task, return_exceptions=True)
            writer = getattr(self.tts, "_writer", None)
            if writer is not None:
                await asyncio.gather(writer, return_exceptions=True)
            self.link.close()
