"""Dictation glue for `Voice`: the tmux and desktop dictation sessions, their socket control and notices.

A mixin: it runs on the voice event loop with `Voice`'s own state (mic, busy, state, link) and helpers.
"""
import asyncio
import time

import numpy as np

from .config import log
from .dictation import DictationError
from . import desktop_input
from .wake import WakeGate

DICTATION_TARGET_TIMEOUT_S = 3.2


class DictationCtl:
    def _dictation_event(self, *, phase: str, reason: str | None, session_id: str,
                         chars: int, elapsed_ms: int | None = None) -> None:
        self._tr("dictation", phase=phase, reason=reason, session_id=session_id,
                 chars=chars, elapsed_ms=elapsed_ms)
        key = (session_id, phase, reason)
        if key == self._dictation_notified:
            return
        self._dictation_notified = key
        notices = {
            "listening": "Dictation listening",
            "paused": "Dictation paused",
            "transcribing": "Transcribing dictation",
            "review": "Dictation ready to review",
            "awaiting_commit": "Dictation needs a typed commit",
            "preparing": "Preparing dictation insertion",
            "inserting": "Inserting dictation",
            "finished": "Dictation finished",
            "cancelled": "Dictation cancelled",
        }
        notice = notices.get(phase)
        if phase == "review" and reason in {"preview_error", "preview_timeout", "final_error", "final_timeout"}:
            notice = "Dictation recognition had a problem; preview retained"
        elif phase == "review" and reason in {"audio_limit", "duration_limit", "text_limit"}:
            notice = "Dictation limit reached; review or cancel"
        elif phase == "review" and reason == "insert_uncertain":
            notice = "Dictation paste may have happened; check status and do not retry"
        elif phase == "review" and reason in {"mic_off", "automute", "device_loss", "mic_unavailable"}:
            notice = "Dictation retained; microphone unavailable"
        if notice:
            self._io_call(self.cues.notify, notice)

    def dictation_dashboard(self) -> dict | None:
        """Return the active local in-memory preview for the read-only dashboard."""
        return self.dictation.dashboard()

    def _desktop_event(self, **fields) -> None:
        # Focus loss can arrive from the socket watch thread; Voice belongs to its event loop.
        try:
            on_loop = asyncio.get_running_loop() is self.desktop._loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            self._desktop_event_on_loop(fields)
        else:
            self.desktop._loop.call_soon_threadsafe(self._desktop_event_on_loop, fields)

    def _desktop_event_on_loop(self, fields: dict) -> None:
        if fields.get("event") == "dictation_chunk":
            self._tr("dictation_chunk", **{k: v for k, v in fields.items() if k != "event"})
            return
        phase, reason = fields["phase"], fields.get("reason")
        self._tr("dictation", mode="desktop", **fields)
        key = (fields["session_id"], phase, reason, fields.get("event"))
        if key == self._desktop_notified:
            return
        # Start is announced after the focus watch connects and confirms the pin.
        if phase == "listening" and reason == "start":
            self._desktop_discarded = fields.get("discarded_chars", 0)
            return
        self._desktop_notified = key
        metadata = self.desktop.metadata()
        if fields.get("event") == "dictation_warning" and reason in {"ctrl_release_failed", "key_release_failed"}:
            self.cues.play("error")
            self._io_call(self.cues.notify,
                          ("Dictation: Ctrl may be held; check the keyboard and destination"
                           if reason == "ctrl_release_failed" else
                           "Dictation: keys may be held; check the keyboard and destination"), True)
        elif phase == "listening" and reason != "listening":
            failures = {"lock_leave_failed": "typing lock cleanup failed",
                        "disarm_lock_timer_failed": "typing lock timer cleanup failed",
                        "focus_watch_stop_failed": "focus watch cleanup failed"}
            self.cues.play("error")
            self._io_call(self.cues.notify,
                          "Dictation: " + failures.get(reason, "internal operation failed"), True)
        elif phase == "listening":
            self.cues.play("wake")
            notice = f"Dictating into {metadata['class']}"
            if self._desktop_discarded:
                notice += f"; discarded {self._desktop_discarded} held characters"
                self._desktop_discarded = 0
            self._io_call(self.cues.notify, notice, True)
        elif phase == "paused":
            reasons = {
                "focus_changed": "destination lost focus", "destination_closed": "destination closed",
                "focus_watch_lost": "focus watch lost", "layer_opened": "another layer opened",
                "layer_open": "another layer opened", "locked": "screen locked",
                "unsupported_class": "unsupported window", "denied_class": "protected window",
                "ptt": "push to talk", "mic_off": "microphone off", "auto_mute": "microphone in use",
                "device_loss": "microphone lost", "backlog": "recognition backlog",
                "inference_timeout": "recognition timed out", "inference_error": "recognition failed",
                "insert_uncertain": "insertion uncertain; check the destination",
            }
            self.cues.play("error")
            self._io_call(self.cues.notify,
                          f"Dictation paused: {reasons.get(reason, 'delivery stopped')}; press to stop", True)
        elif phase == "stopping":
            self._io_call(self.cues.notify, "Finishing dictation")
        elif phase in {"stopped", "cancelled"}:
            self.cues.play("ready")
            held = metadata.get("held_chars", 0)
            notice = ("Dictation cancelled; typed text stays" if phase == "cancelled" else
                      f"Dictation stopped; {held} characters held" if held else "Dictation stopped")
            self._io_call(self.cues.notify, notice)
            self._dictation_leave()

    def _desktop_start_check(self) -> None:
        if self._desktop_reset_task is not None and (
                not self._desktop_reset_task.done() or not self._desktop_reset_task.result()):
            raise DictationError("lock_reset_failed")
        self._start_check()

    def _start_check(self) -> None:
        """The one check before any dictation starts (tmux or desktop): no session, voice idle, mic usable."""
        if self.dictation.engaged or self._dictation_mode_active:
            raise DictationError("session_active")
        if self.state != "IDLE" or self._busy or self._wake_gate_pending or \
                self._local_speech_count:
            raise DictationError("voice_busy")
        if not self._dictation_ready():
            raise DictationError("mic_unavailable")

    def _desktop_refused(self, code: str) -> None:
        reasons = {"session_active": "another dictation session is active", "voice_busy": "voice is busy",
                   "mic_unavailable": "microphone unavailable", "no_active_window": "no active window",
                   "denied_class": "protected window", "unsupported_class": "unsupported window class",
                   "locked": "screen locked", "wtype_missing": "wtype is missing",
                   "lock_reset_failed": "typing lock cleanup unavailable",
                   "focus_changed": "focus changed", "destination_closed": "destination closed",
                   "layer_open": "another layer is open", "inference_busy": "recognition is still running"}
        self._io_call(self.cues.notify, "Cannot dictate: " + reasons.get(code, "destination unavailable"))

    def toggle_dictation(self) -> None:
        """Nonblocking SIGWINCH callback; every shortcut renews the key-release wait."""
        self.desktop.shortcut_pressed()
        now = time.monotonic()
        if now - self._dictation_press_at < 0.3:
            return
        self._dictation_press_at = now
        if self._shutting_down or (self._desktop_start_task and not self._desktop_start_task.done()):
            return
        if self.desktop.engaged:
            if self.desktop.phase == "stopping":
                self._io_call(self.cues.notify, "Dictation is already stopping")
            else:
                self.desktop.toggle_stop()
            return
        try:
            self._desktop_start_check()
        except DictationError as error:
            self._desktop_refused(error.code)
            return
        self._desktop_start_task = self._bg(self._desktop_start(self._desktop_epoch, self.t))

    async def _desktop_start(self, epoch: int, turn) -> None:
        try:
            job = asyncio.get_running_loop().run_in_executor(
                self._dictation_io, desktop_input.read_pin, self.cfg.dictation_denied_classes,
                self.cfg.dictation_unsupported_classes, self.cfg.dictation_ignore_layers)
            pin = await asyncio.wait_for(job, DICTATION_TARGET_TIMEOUT_S)
            self._desktop_start_check()
            if epoch != self._desktop_epoch or turn is not self.t:
                raise DictationError("voice_busy")
            self.desktop.start(pin)
            self._desktop_notified = None
            self._dictation_enter()
        except (DictationError, desktop_input.InputRefused) as error:
            self._desktop_refused(error.code)
        except Exception:
            self._desktop_refused("target_unavailable")

    def _dictation_suspend(self, reason: str) -> None:
        if self.desktop.engaged:
            self.desktop.suspend("auto_mute" if reason == "automute" else reason)
        else:
            self.dictation.suspend(reason)

    def _dictation_ready(self) -> bool:
        return self.mic_on and self._mic_available and self.mic_available_probe() and \
            not self.automute and not self._shutting_down

    def _dictation_reset(self) -> None:
        """Entering or leaving dictation: drop listen state and wake/VAD history so neither hears the other."""
        self.ep, self._ep_phase, self._spare, self.t.head = None, None, None, None
        self.t.confirm_s = None
        self.wake_audio = np.zeros(0, dtype=np.int16)
        if self.gate is not None:
            self.gate = WakeGate(self.cfg.wake_threshold)
        if self.wake is not None:
            reset = getattr(self.wake, "reset", None)
            if reset:
                reset()
            ring = getattr(self.wake, "ring", None)
            if ring is not None:
                ring.clear()
        getattr(self.prob, "reset", lambda: None)()

    def _dictation_enter(self) -> None:
        self._dictation_mode_active = True
        self._dictation_reset()
        self._set_busy(True)
        if self.cfg.duck_media:
            self._io_call(self.media.pause, self.t.tid)

    def _dictation_leave(self) -> None:
        if self.dictation.engaged or not self._dictation_mode_active:
            return
        self._dictation_mode_active = False
        self._dictation_reset()
        self._dictation_notified = None
        if self._shutting_down:
            self._set_busy(False)
        else:
            self._idle()

    def set_mic_available(self, available: bool) -> None:
        self._mic_available = bool(available)
        if not available:
            self._desktop_epoch += 1
            self._dictation_suspend("device_loss")

    def mic_frame(self, frame: np.ndarray) -> None:
        if self._shutting_down or not self.mic_available_probe():
            self.set_mic_available(False)
            return
        self.set_mic_available(True)
        self.feed(frame)

    def dictation_shutdown(self) -> None:
        self._shutting_down = True
        self._mic_available = False
        self._desktop_epoch += 1
        if self._desktop_start_task and not self._desktop_start_task.done():
            self._desktop_start_task.cancel()
        self.desktop.shutdown()
        self.dictation.shutdown()
        self._dictation_leave()

    def _dictation_task(self, session_id: str, operation: str, coro) -> asyncio.Task:
        key = (session_id, operation)
        current = self._dictation_tasks.get(key)
        if current is not None and not current.done():
            return current
        task = asyncio.get_running_loop().create_task(coro)
        self._dictation_tasks[key] = task
        task.add_done_callback(lambda done, task_key=key: self._dictation_task_done(task_key, done))
        return task

    def _dictation_task_done(self, key: tuple[str, str], task: asyncio.Task) -> None:
        if self._dictation_tasks.get(key) is task:
            self._dictation_tasks.pop(key, None)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            # Do not stringify model or insertion errors: they may contain user text.
            log("dictation_error", operation=key[1], error="operation_failed")

    async def dictation_control(self, request: dict) -> dict:
        """Handle one allowlisted socket command; long work is acknowledged and polled via status."""
        action = request["action"]
        if action == "start":
            if self._desktop_start_task and not self._desktop_start_task.done():
                raise DictationError("session_active")
            self._start_check()
            from jv.tmux import dictation_target
            loop = asyncio.get_running_loop()
            target_job = loop.run_in_executor(self._dictation_io, dictation_target, request["target"])
            try:
                target = await asyncio.wait_for(target_job, DICTATION_TARGET_TIMEOUT_S)
            except asyncio.TimeoutError:
                raise DictationError("target_timeout") from None
            except Exception:
                raise DictationError("target_unavailable") from None
            # The selector may have taken time to resolve while another voice turn or mic event arrived.
            if self._shutting_down or self.dictation.engaged:
                raise DictationError("voice_busy")
            self._start_check()
            metadata = self.dictation.start(target)
            self._dictation_enter()
            return {**metadata, "operation": "start"}

        metadata = self.dictation.metadata()
        session_id = request.get("session_id")
        if action == "status":
            if session_id is not None and metadata.get("session_id") != session_id:
                raise DictationError("stale_session" if metadata.get("session_id") else "no_session")
            return {**metadata, "operation": "status"}
        if metadata.get("session_id") != session_id:
            raise DictationError("stale_session" if metadata.get("session_id") else "no_session")
        if metadata.get("mode") == "desktop":
            if action == "resume":
                if not self._dictation_ready():
                    raise DictationError("mic_unavailable")
                result = self.desktop.resume(session_id)
            elif action == "pause":
                result = self.desktop.suspend("user_pause") or self.desktop.metadata()
            elif action == "cancel":
                result = self.desktop.cancel()
            else:
                raise DictationError("invalid_action")
            return {**result, "operation": action}
        if action == "pause":
            return {**self.dictation.pause(session_id), "operation": "pause"}
        if action == "resume":
            if not self._dictation_ready():
                raise DictationError("mic_unavailable")
            return {**self.dictation.resume(session_id), "operation": "resume"}
        if action == "finish":
            key = (session_id, "finish")
            existing = self._dictation_tasks.get(key)
            if existing is not None and not existing.done():
                return {**metadata, "operation": "finish"}
            self.dictation.begin_finish(session_id)
            self._dictation_task(session_id, "finish", self._dictation_finish(session_id))
            return {**self.dictation.metadata(), "operation": "finish"}
        if action == "commit":
            revision = request["revision"]
            if not self._dictation_ready():
                raise DictationError("mic_unavailable")
            key = (session_id, "commit")
            existing = self._dictation_tasks.get(key)
            if existing is not None and not existing.done():
                return {**metadata, "operation": "commit"}
            payload = self.dictation.commit_payload(session_id, revision)
            attempt = self.dictation.begin_commit(session_id, revision, explicit=True)["attempt"]
            self._dictation_task(session_id, "commit", self._dictation_insert(
                session_id, revision, attempt, payload["target"], payload["text"], confirmed=True))
            return {**self.dictation.metadata(), "operation": "commit"}
        if action == "cancel":
            result = self.dictation.cancel(session_id)
            self._dictation_leave()
            return {**result, "operation": "cancel"}
        raise DictationError("invalid_action")

    async def _dictation_finish(self, session_id: str) -> None:
        result = await self.dictation.finish(session_id)
        if result.get("error"):
            return
        if not self._dictation_ready():
            self._io_call(self.cues.notify, "Dictation retained; microphone unavailable")
            return
        attempt = self.dictation.begin_commit(session_id, result["revision"], explicit=False)["attempt"]
        await self._dictation_insert(session_id, result["revision"], attempt, result["target"],
                                     result["text"], confirmed=False)

    async def _dictation_insert(self, session_id: str, revision: int, attempt: int, target: dict,
                                text: str, *, confirmed: bool) -> None:
        if not self._dictation_ready():
            self.dictation.attempt_failed(session_id, revision, "mic_unavailable", attempt)
            return
        from jv.tmux import dictation_insert

        def claim() -> bool:
            return self._dictation_ready() and self.dictation.claim_commit(session_id, revision, attempt)

        try:
            dispatch = asyncio.get_running_loop().run_in_executor(
                self._dictation_io,
                lambda: dictation_insert(target, text, confirmed=confirmed, claim=claim))
            result = await dispatch
        except Exception:
            # No exception text is logged or returned. The tmux helper classifies uncertain paste sends itself.
            phase = self.dictation.attempt_phase(session_id, revision, attempt)
            if phase == "inserting":
                self.dictation.complete_commit(session_id, revision, attempt, accepted=False,
                                               reason="insert_uncertain")
            elif phase == "preparing":
                self.dictation.attempt_failed(session_id, revision, "insert_refused", attempt)
            return
        status = result.get("status")
        if status == "confirmation_required":
            self.dictation.awaiting_commit(session_id, revision, attempt)
        elif status == "accepted":
            self.dictation.complete_commit(session_id, revision, attempt, accepted=True, reason="accepted")
            self._dictation_leave()
        elif status == "uncertain":
            self.dictation.complete_commit(session_id, revision, attempt, accepted=False,
                                           reason="insert_uncertain")
        elif status == "cancelled":
            if self.dictation.attempt_phase(session_id, revision, attempt) == "preparing":
                self.dictation.attempt_failed(session_id, revision, "commit_blocked", attempt)
        else:
            if self.dictation.attempt_phase(session_id, revision, attempt) == "preparing":
                self.dictation.attempt_failed(session_id, revision, "insert_refused", attempt)
