"""Poll durable reminders and claim each before attempting speech."""
import asyncio
from datetime import datetime
import logging
import math
import os
import signal
import time
import uuid

from heardback.tasks import personal
from jv.sysctl import volume_now

TICK_S = 5
RESUME_SETTLE_S = 10
RETRY_S = 60
DELIVERY_TIMEOUT_S = 120
KEEP_AWAKE_S = 60 * 60
KEEP_AWAKE_OVERDUE_S = 10 * 60
INHIBITOR_GRACE_S = 5
log = logging.getLogger(__name__)


def wording(row: dict, now: float) -> str:
    text = row["text"]
    text = text[0].upper() + text[1:]
    if not text.endswith((".", "!", "?")):
        text += "."
    prefix = "Reminder."
    if now - row["due_at"] > 120:
        due = datetime.fromtimestamp(row["due_at"])
        clock = due.strftime("%-I %p" if due.minute == 0 else "%-I %M %p").lower()
        prefix = f"Late reminder from {clock}."
    return f"{prefix} {text}"


class Coordinator:
    def __init__(self, brain):
        self.brain = brain
        brain.coordinator = self
        self.failure_notice = False
        self.failure_text = "Reminders are off, the storage check failed"
        self.startup_outage = False
        self.volume_outage = False
        self.outcome = None
        self.retry_at: dict[str, float] = {}
        self.delivery = None
        self.recovering = False
        self.inhibitor = None
        self.inhibitor_retry_at = 0.0
        self.inhibitor_started_at = 0.0
        self.inhibitor_checked_at = 0.0

    async def startup(self) -> bool:
        self.recovering = True
        while True:
            try:
                await asyncio.to_thread(personal.verify_store)
                await asyncio.to_thread(personal.recover_delivering)
                self.startup_outage = False
                self.recovering = False
                return True
            except personal.PersonalStoreError as exc:
                if exc.code == "busy":
                    await asyncio.sleep(RETRY_S)
                    continue
                log.exception("reminder startup failed")
            except Exception:
                log.exception("reminder startup failed")
            if not self.startup_outage:
                self.startup_outage = True
                self.failure_notice = True
                self.failure_text = "Reminders are off, the storage check failed"
                await self.notify_failure()
            return False

    async def notify_failure(self) -> None:
        voice = self.brain.voice
        if self.failure_notice and voice is not None and not voice.is_closing():
            self.failure_notice = False
            await self.brain.announce(self.failure_text)

    def reply(self, msg, conn) -> None:
        if self.delivery is None:
            return
        delivery_id, voice, future = self.delivery
        if (conn is voice and msg.get("id") == delivery_id and not future.done()
                and msg.get("result") in ("spoken", "refused", "failed")):
            future.set_result(msg["result"])

    async def tick(self) -> None:
        try:
            if self.outcome is not None:
                await self._finish_outcome()
                return
            now = time.time()
            rows = await asyncio.to_thread(personal.list_reminders, state="pending",
                                           due_before=int(now), limit=1)
            if rows:
                self.brain.events.add({"kind": "reminder", "id": rows[0]["reminder_id"]})
            await self.brain.flush_events()
        except personal.PersonalStoreError as exc:
            if exc.code != "busy":
                self.failure_notice = True
                self.failure_text = "Reminders are off, the storage check failed"
                log.exception("reminder tick failed")
        except Exception:
            self.failure_notice = True
            self.failure_text = "Reminders are off, the storage check failed"
            log.exception("reminder tick failed")
        finally:
            await self._refresh_inhibitor(time.time())

    async def deliver(self, event: dict) -> bool:
        if self.recovering:
            return False
        reminder_id = event["id"]
        row = await asyncio.to_thread(personal.get_reminder, reminder_id)
        if not row or row["state"] != "pending" or row["due_at"] > time.time():
            return True
        if time.time() < self.retry_at.get(reminder_id, 0):
            return False
        try:
            volume = await asyncio.to_thread(volume_now)
        except Exception:
            if not self.volume_outage:
                self.volume_outage = True
                log.warning("reminder volume check failed; holding reminders")
                self.failure_text = "Reminders are held, the volume check failed"
                self.failure_notice = True
                await self.notify_failure()
            return False
        if self.volume_outage:
            self.volume_outage = False
            log.info("reminder volume check recovered")
        if volume[1] or await self.brain.event_policy(event) != "now":
            return False
        voice = self.brain.voice
        claim = await asyncio.to_thread(personal.claim_reminder, reminder_id,
                                        expected_revision=row["revision"])
        if claim is None:
            return True
        if await self.brain.event_policy(event) != "now" or self.brain.voice is not voice:
            await asyncio.to_thread(personal.finish_reminder, reminder_id,
                                    expected_revision=claim["revision"], result="pending")
            return False
        # A claim stays delivering on cancellation/error; startup makes it unknown.
        delivery_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.delivery = delivery_id, voice, future
        try:
            async with asyncio.timeout(DELIVERY_TIMEOUT_S):
                await self.brain._send(voice, {"type": "say", "text": wording(row, time.time()),
                    "final": True, "announce": True, "delivery_id": delivery_id})
                result = await future
        except asyncio.TimeoutError:
            result = "unknown"
        finally:
            self.delivery = None
        if result == "failed":
            self.retry_at[reminder_id] = time.time() + RETRY_S
        else:
            self.retry_at.pop(reminder_id, None)
        self.outcome = (reminder_id, claim["revision"],
                        "pending" if result in ("refused", "failed") else result)
        await self._finish_outcome()
        return True

    async def _finish_outcome(self) -> None:
        reminder_id, revision, result = self.outcome
        await asyncio.to_thread(personal.finish_reminder, reminder_id,
                                expected_revision=revision, result=result)
        self.outcome = None

    async def _refresh_inhibitor(self, now: float) -> None:
        try:
            await self._update_inhibitor(now)
        except Exception:
            log.exception("could not update reminder sleep inhibitor")

    async def _update_inhibitor(self, now: float) -> None:
        try:
            rows = await asyncio.to_thread(personal.list_reminders, state="pending",
                due_after=math.ceil(now - KEEP_AWAKE_OVERDUE_S),
                due_before=math.floor(now + KEEP_AWAKE_S), limit=1)
        except personal.PersonalStoreError as exc:
            if exc.code == "busy":
                log.debug("reminder sleep inhibitor eligibility read busy")
            else:
                log.exception("could not update reminder sleep inhibitor")
        except Exception:
            log.exception("could not update reminder sleep inhibitor")
        else:
            self.inhibitor_checked_at = now
            if not rows:
                if self.inhibitor is not None:
                    await self._release_inhibitor()
                return
            if self.inhibitor is not None:
                if self.inhibitor.returncode is None:
                    return
                log.warning("reminder sleep inhibitor exited with status %s",
                            self.inhibitor.returncode)
                if now - self.inhibitor_started_at <= RETRY_S:
                    self.inhibitor_retry_at = now + RETRY_S
                await self._release_inhibitor()
            if now < self.inhibitor_retry_at:
                return
            try:
                self.inhibitor = await asyncio.create_subprocess_exec(
                    "systemd-inhibit", "--what=sleep", "--mode=block", "--who=heardback",
                    "--why=Pending reminder", "tail", f"--pid={os.getpid()}", "-f", "/dev/null",
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                )
                self.inhibitor_started_at = now
                if self.inhibitor.returncode is not None:
                    log.warning("reminder sleep inhibitor exited early with status %s",
                                self.inhibitor.returncode)
                    self.inhibitor_retry_at = now + RETRY_S
                    await self._release_inhibitor()
            except Exception:
                self.inhibitor_retry_at = now + RETRY_S
                log.exception("could not start reminder sleep inhibitor")
            return
        if now - self.inhibitor_checked_at > KEEP_AWAKE_OVERDUE_S:
            await self._release_inhibitor()

    async def _release_inhibitor(self) -> None:
        proc = self.inhibitor
        if proc is None:
            return
        try:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except OSError:
                    pass
                try:
                    async with asyncio.timeout(INHIBITOR_GRACE_S):
                        await proc.wait()
                    return
                except TimeoutError:
                    pass
            log.error("reminder sleep inhibitor was not reaped after SIGKILL")
        except OSError:
            log.exception("could not release reminder sleep inhibitor")
        except asyncio.CancelledError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            raise
        finally:
            self.inhibitor = None

    async def run(self) -> None:
        try:
            while not await self.startup():
                await asyncio.sleep(RETRY_S)
            await self.brain.flush_events()
            while True:
                before = time.time()
                await asyncio.sleep(TICK_S)
                if time.time() - before > 3 * TICK_S:
                    await asyncio.sleep(RESUME_SETTLE_S)
                await self.tick()
        finally:
            await self._release_inhibitor()
