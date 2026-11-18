"""Brain socket server: utterance -> built-in | fast-path jv call | the persistent Claude process."""
import asyncio
import hmac
import json
import logging
import os
import re
import sys
import tempfile
import textwrap
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from heardback.brain import catalogue, memory as user_memory, play_result, router, usage
from heardback.brain.claude_proc import PROMPT, ClaudeProc, build_cmd
from heardback.brain.events import EventQueue, speak_policy
from heardback.brain.speech import SENTENCE_MAX, SentenceSplitter, asks
from heardback.status.store import state_dir
from heardback.status import away, speak
from heardback.voice.text import match_confirm, match_repeat   # stdlib only: no voice extra needed
from jv import grants, trace
from jv.verb import UNCONFIRMED, observation_result, runtime_path

# R4: the brain holds Claude-bound turns and sends the next only after the previous `result`. The Task 17 probe
# (2026-10-30, text-only turns) saw stream-json queue a mid-turn message as its own turn, but typed text merges
# into a running turn at tool boundaries (decisions 2026-10-29), so we never rely on Claude's queue.
QUEUE_IN_CLAUDE = False
JV_TIMEOUT_S = catalogue.JV_TIMEOUT_S
RECENT_MAX = 5
BACKOFF_S = 1          # respawn backoff start: 1, 2, 4 … BACKOFF_MAX_S
BACKOFF_MAX_S = 60
IDLE_RESET_S = 600     # context bounds (spec §2.4)
TOKEN_RESET = 40_000
COST_RESET = 0.25
TURN_RESET = 30
MSG_COST_RESET = 0.05
CONFIRM_TIMEOUT_S = 8   # sent to the voice client, which answers no at its own timeout
CONFIRM_GRACE_S = 22    # safety net only: voice speaks the read-back first, then listens, and always answers
INTERRUPT_GRACE_S = 2   # no `result` this long after an interrupt → kill + respawn
WORKING_S = 5           # no sentence this long after send → "Working on it." once
WALL_CAP_S = 90         # no result this long after send → interrupt
MIN_TURN_GAP_S = 2      # Claude sends at least this far apart
AWAIT_S = 30           # lifetime of a non-confirm question, refreshed when spoken
CONSTANTS = {"JV_TIMEOUT_S", "RECENT_MAX", "BACKOFF_S", "IDLE_RESET_S", "TOKEN_RESET", "COST_RESET", "TURN_RESET",
             "MSG_COST_RESET", "CONFIRM_TIMEOUT_S", "CONFIRM_GRACE_S", "INTERRUPT_GRACE_S", "WORKING_S", "WALL_CAP_S",
             "MIN_TURN_GAP_S", "AWAIT_S"}   # Brain(**overrides) may replace any (tests use tiny ones)

IGNORE = {"you", "thank you", "thanks"}   # common STT hallucinations on silence
STOP = {router.normalise(p) for p in ("stop", "cancel", "never mind", "shut up")}
RESET = {router.normalise(p) for p in ("new topic", "start over", "forget that")}

OFF = "Claude is off; simple commands still work."
RESTARTED = "Claude restarted, please say that again."
SIGNED_OUT = "Claude isn't signed in; simple commands still work."
MCP_FAILED = "Claude's tools failed to load; simple commands still work."
UNFINISHED = "I couldn't finish that."
WORKING = "Working on it."
TOO_LONG = "That was taking too long, so I stopped. I can hand it to a Claude session."
NOT_DOING = "Okay, not doing it."
FRESH = "Okay, fresh start."
SAID_NO = "User said no."
NO_ANSWER = "No answer heard; nothing was done."   # timeout / nothing heard is not a no
NO_ANSWER_SAY = "I didn't hear an answer, so I didn't do it."
STEP_LIMIT = "Too many steps; ask me one at a time."
NOT_DONE = {
    "no": NOT_DOING,
    "other": "I didn't do that. I'll take your answer as a new request.",
    "timeout": NO_ANSWER_SAY,
    "unasked": "I couldn't ask you to confirm, so I didn't do it.",
    "refused": "I didn't do that because the tool call was refused.",
    "preflight": "I couldn't do that because the preflight check failed.",
    "tainted": "I read outside text in this conversation, so I won't do that on my own. Say new topic, then ask again.",
    "steps": STEP_LIMIT,
}
OTHER_ANSWER = "The user answered with other words; their answer comes next."
NOT_ALLOWED = "heardback refused that tool call."
UNKNOWN_VERB = "heardback refused: unknown verb or args; call jv_find first."
PERSONAL_TOOLS = frozenset(("reminder_add", "reminder_list", "reminder_cancel", "inbox_add",
                            "fact_remember", "fact_forget", "catchup_catchup"))
PERSONAL_WRITES = PERSONAL_TOOLS - {"reminder_list", "catchup_catchup"}
PERSONAL_UNCERTAIN = "I'm not sure that saved. Ask what my reminders are."
PERSONAL_FALLBACK = dict.fromkeys(PERSONAL_TOOLS, PERSONAL_UNCERTAIN) | {
    "inbox_add": "I'm not sure that saved. Say read my notes.",
    "fact_remember": "I'm not sure that changed. Please check your memory file.",
    "fact_forget": "I'm not sure that changed. Please check your memory file.",
    "reminder_list": "I couldn't read that. Try again.",
    "catchup_catchup": "I couldn't read that. Try again."}
PERSONAL_SECOND = "That wasn't set. Say it again."

log = logging.getLogger(__name__)


@dataclass
class Turn:
    conn: asyncio.StreamWriter
    text: str
    model: str | None = None
    t0: float = field(default_factory=time.monotonic)
    splitter: SentenceSplitter = field(default_factory=SentenceSplitter)
    raw_lines: deque = field(default_factory=lambda: deque(maxlen=50))
    sent: bool = False      # R4: only the head of the FIFO is ever sent to Claude
    sent_at: float = 0.0    # monotonic time of the (latest) send; an MCP respawn resends
    spoken: bool = False
    interrupted: bool = False   # our own interrupt: later text unspoken, its error result not an error
    speaker_ok: bool = True   # utterances failing the speaker check never become turns
    tools: list = field(default_factory=list)   # jv tools Claude called, in order, for brain.log
    acts: list[dict] = field(default_factory=list)
    reply_text: str = ""
    reply_lines: list[str] = field(default_factory=list)
    confirm_reason: str = ""
    tid: str | None = None   # trace turn id (made by voice, else by the brain)
    tool_ids: dict = field(default_factory=dict)   # tool_use id → name, for tool_result trace events
    tool_msgs: dict = field(default_factory=dict)  # tool_use id → assistant message id: a retry comes in a later message
    followup: bool = False   # a reply to Claude's question (or a read-back): a memory reset would lose the thread
    deferred_speech: list[str] = field(default_factory=list)
    sensitive_context: bool = False
    terminal: str | bool | None = None
    terminal_expect_reply: bool = False
    execution_attempt: str | None = None
    finished: bool = False
    route_text: str | None = None
    parent: str | None = None
    personal_write: bool = False
    tool_output: bool = False
    context_tokens: int | None = None
    acting_calls: int = 0


@dataclass
class Awaiting:
    future: asyncio.Future
    speaker_default: bool
    id: str
    question: str
    expires: float
    owner: Turn | None = None
    replay: asyncio.Event = field(default_factory=asyncio.Event)
    outcome: dict | None = None
    kind: str = "confirm"
    source: str = "readback"
    options: dict = field(default_factory=dict)
    original: str = ""
    labels: dict = field(default_factory=dict)   # normalisation turns spoken "two" into "2"


def answer_for(aw: Awaiting, text: str) -> router.Route | str:
    """Match a choice or classify a follow-up; built-ins keep normal routing."""
    norm = router.normalise(text)
    if norm in STOP | RESET:
        return "new"
    label = re.sub(r"^(?:on|in|use|choose|pick|option|answer|press|say) ", "", norm)
    if aw.source == "alert" and (yes := match_confirm(label)) is not None:
        label = "yes" if yes else "no"
    if aw.kind == "choice" and label in aw.options:
        return aw.options[label] or "model"
    return "model" if aw.source == "model" and aw.kind != "confirm" else "new"


def act(turn: Turn, tool_id, name: str) -> dict:
    for item in turn.acts:
        if item["id"] == tool_id:
            return item
    item = {"id": tool_id, "name": name, "state": "failed"}
    turn.acts.append(item)
    return item


def closing(turn: Turn, read_tools=()) -> list[str]:
    """Choose speech from observed outcomes without changing the turn or its permissions."""
    if turn.interrupted and not any(a["state"] == "ran" and a.get("name") in PERSONAL_WRITES for a in turn.acts):
        return []
    ran = [a for a in turn.acts if a["state"] == "ran"]
    denied = any(a["state"] == "denied" for a in turn.acts)
    tool_lines = [a["say"] for a in ran or turn.acts if isinstance(a.get("say"), str) and a["say"].strip()
                  and not (denied and a.get("name") in read_tools)]
    terminal = turn.terminal if isinstance(turn.terminal, str) else ""
    text_lines = [] if turn.terminal else turn.reply_lines or (SentenceSplitter().split(turn.reply_text) if not turn.spoken else [])
    has_reply = turn.spoken or any(s.strip().casefold() not in {"", "okay.", "done."} for s in text_lines)
    lines = [terminal] if terminal else list(text_lines)
    personal_lines = [a["say"] for a in turn.acts if a.get("name") in PERSONAL_TOOLS
                      and isinstance(a.get("say"), str) and a["say"].strip()]
    if personal_lines and not terminal:
        lines = [personal_lines[-1]]
    if terminal and turn.terminal_expect_reply and not turn.spoken:
        lines = [a["say"] for a in ran if a.get("name") not in read_tools and a.get("say")] + lines
    if not terminal and not turn.spoken and not any(s.strip() for s in text_lines):
        lines = tool_lines
    if len(ran) != len(turn.acts):
        reasons = [a.get("say") if a.get("name") in PERSONAL_WRITES and a.get("say") else
                   NOT_DONE.get(a.get("reason"), "I couldn't confirm that worked, so it isn't done.")
                   for a in turn.acts if a["state"] != "ran" and
                   (a["state"] == "denied" or not has_reply) and
                   not (turn.spoken and a.get("reason") == "preflight")]
        personal_second = terminal and any(a.get("name") in PERSONAL_WRITES and a["state"] == "ran" for a in turn.acts)
        if personal_second:
            lines += reasons
        elif terminal:
            pass
        elif turn.spoken:
            lines = lines[:-1] + reasons + lines[-1:] if turn.terminal_expect_reply else lines + reasons
        else:
            lines = reasons + lines
        lines = list(dict.fromkeys(lines))
        lines = [s for s in lines if s.strip().casefold() not in {"okay.", "done."}]
    if not lines and not turn.spoken:
        lines = ["Done." if turn.acts and len(ran) == len(turn.acts) else "I'm not sure that worked."]
    return lines


def confirmation(text: str) -> bool | None:
    # Action-bearing answers may change the request; only a plain yes approves it.
    if match_confirm(text) is True and re.search(r"\b(close|kill|run|send|press|open|delete|remove|start|play|type|enter|quit|exit|submit|answer)\b", text, re.I):
        return None
    return match_confirm(text)


def correction(question: str, heard: str, original: str) -> str:
    return f'heardback asked: "{question}". The user answered: "{heard}". Original request: "{original}".'


def _json(out: str) -> dict:
    try:
        data = json.loads(out) if out.strip() else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _stream_record(turn: Turn, ev: dict) -> dict:
    """Do not persist text read from the local clipboard or selection."""
    if not turn.sensitive_context:
        return ev
    kind = ev.get("type")
    if kind == "result" and isinstance(ev.get("result"), str):
        return {**ev, "result": "[local context reply omitted]"}
    if kind == "stream_event":
        event = ev.get("event") or {}
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta":
            return {**ev, "event": {**event, "delta": {**delta, "text": "[local context reply omitted]"}}}
    if kind in ("assistant", "user"):
        message = ev.get("message") or {}
        content = message.get("content") or []
        changed = []
        for block in content:
            if not isinstance(block, dict):
                changed.append(block)
            elif kind == "user" and block.get("type") == "tool_result" and \
                    turn.tool_ids.get(block.get("tool_use_id")) == "ctx_ctx":
                changed.append({**block, "content": "[local context text omitted]"})
            elif kind == "assistant" and block.get("type") == "text":
                changed.append({**block, "text": "[local context reply omitted]"})
            else:
                changed.append(block)
        return {**ev, "message": {**message, "content": changed}}
    return ev


def _reason(rc: int, data: dict, err: str) -> str:
    """jv's failure reason (technical: logged, sent to Claude as a deny message; spoken only via `spoken`)."""
    if rc == 2 and data.get("candidates"):
        labels = [str(c.get("label") or c.get("title") or "that page") if isinstance(c, dict) else str(c)
                  for c in data["candidates"][:4]]
        labels = [" ".join(re.sub(r"https?://\S+", "page", label).split()[:10]) for label in labels]
        return f"which one: {', '.join(labels)}?"
    reason = data.get("error") or next(iter(err.strip().splitlines()), "") or f"exit {rc}"
    if data.get("closest"):
        reason += f". Closest: {', '.join(map(str, data['closest']))}."
    return reason


def spoken(rc: int, data: dict, err: str) -> str:
    """A failed fast-path action as one plain sentence: jv's own `say`, else "Couldn't <reason>."
    TTS pauses at colons, so none; and "couldn't" once (playfix)."""
    if isinstance(data.get("say"), str) and data["say"].strip():
        return data["say"].strip()
    r = re.sub(r"\s*:\s*", ", ", _reason(rc, data, err)).strip().rstrip(".")
    if r.lower().startswith("no "):
        return r[0].upper() + r[1:] + "."
    if re.search(r"(?i)\b(?:couldn't|could not|can't|cannot)\b", r):   # already says it: no second "couldn't"
        return r[0].upper() + r[1:] + "."
    return f"Couldn't {r}."


def _model_family(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    lowered = value.casefold()
    return next((family for family in ("haiku", "sonnet", "opus") if family in lowered), None)


def sock_path() -> Path:
    return Path(runtime_path("heardback.sock"))


class Brain:
    def __init__(self, claude_cmd: list[str] | None = None, announcer=None,
                 voice_token: str | None = None, **overrides):
        for k, v in overrides.items():
            if k not in CONSTANTS:
                raise TypeError(f"unknown brain constant {k!r}")
        for k in CONSTANTS:
            setattr(self, k, overrides.get(k, globals()[k]))
        self.claude_cmd, self.announcer = claude_cmd, announcer
        self.voice_token = voice_token
        self.voice: asyncio.StreamWriter | None = None
        self.router = router.Router({})
        self.recent: deque = deque(maxlen=self.RECENT_MAX)   # (epoch, verb, args, ok) of fast-path calls
        self.unshown = 0   # the last `unshown` of them Claude hasn't seen yet
        self.claude: ClaudeProc | None = None
        self._tainted: ClaudeProc | None = None   # its conversation took in outside text; any new process starts clean
        self._taint_by: str | None = None   # the tool call that first tainted it, so the effect gate skips its own taint
        self.claude_off: str | None = None   # "tools" (non-jv tool listed) | "mcp" | "missing"
        self.allowed: list[str] | None = None   # the running process's --allowedTools; None = spawned without MCP
        self.mcp_fails = 0
        self.awaiting: dict[asyncio.StreamWriter, Awaiting] = {}
        self.turns: deque[Turn] = deque()
        self.backoff = self.BACKOFF_S
        self.proc_turns, self.last_cost = 0, 0.0   # per process; last_cost = previous cumulative total_cost_usd
        self._idle: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task] = set()
        self._procs: set[ClaudeProc] = set()   # every live process we started
        self._send_lock = asyncio.Lock()   # one sender at a time, so the gap wait can't double-send the head
        self._memory_context_revision: str | None = None   # revision present in the active Claude conversation
        self._memory_logged_revision: str | None = None   # metadata only; never retains the memory body
        self._memory_reset_pending = False
        self._followup_reset: str | None = None
        self.last_send = float("-inf")
        self.model: str | None = None   # last per-turn observed model; unknown until Claude reports one
        self.rate = usage.RateGuard()
        self.day_warned: date | None = None   # the day the DAY_TURN_WARN line was said
        self._tids = 0
        self.coordinator = None
        self.events = EventQueue()
        if announcer is not None and announcer.back:
            back = {s["session_id"]: s for s in announcer.back}
            covered = [e for e in self.events.rows if back.keys() & e.get("states", {}).keys()]
            if covered:
                sessions = announcer.sessions()
                by_id = {s["session_id"]: s for s in sessions}
                for event in covered:
                    for sid, state in event["states"].items():
                        if sid not in back and sid in by_id and by_id[sid].get("state") == state:
                            back[sid] = by_id[sid]
                self.events.rows = [e for e in self.events.rows if e not in covered]
                self.events.add({"kind": "back", "text": speak.back(list(back.values()), sessions)})
                announcer.back = []
        self.voice_busy = False
        self._intakes = 0
        self._event_lock = asyncio.Lock()

    def new_tid(self) -> str:
        """Turn id for utterances that came without one (`jv ask`, old clients)."""
        self._tids += 1
        return f"{time.strftime('%Y%m%d-%H%M%S')}-b{self._tids}"

    def _bg(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._bg_done)

    def _bg_done(self, t: asyncio.Task) -> None:
        self._tasks.discard(t)
        if not t.cancelled() and (e := t.exception()):
            log.error("brain: background task %s failed: %r", t.get_coro().__qualname__, e)
            trace.error("brain", self._tid(), f"bg {t.get_coro().__qualname__}", e)

    def _tid(self) -> str | None:
        """The Claude turn in flight, for events not tied to a connection's turn."""
        return self.turns[0].tid if self.turns and self.turns[0].sent else None

    # --- socket ---------------------------------------------------------------
    async def serve(self, path: Path) -> None:
        path = Path(path)
        if path.exists() or path.is_symlink():
            try:
                _, w = await asyncio.open_unix_connection(str(path))
            except OSError:
                path.unlink()   # stale: nobody listening
            else:
                w.close()
                raise SystemExit("heardbackd already running")
        old = os.umask(0o177)   # socket is born 0600, no window where others can connect
        try:
            server = await asyncio.start_unix_server(self._client, str(path))
        finally:
            os.umask(old)
        os.chmod(path, 0o600)
        from heardback.coordinator import Coordinator
        coordinator = asyncio.create_task(Coordinator(self).run())
        try:
            async with server:   # entered first: a cancel during the pre-warm still closes the server + clients
                if self.claude_cmd:
                    await self._spawn()   # pre-warmed
                await server.serve_forever()
        finally:
            coordinator.cancel()
            await asyncio.gather(coordinator, return_exceptions=True)
            if self._idle:
                self._idle.cancel()
            if self.turns and self.turns[0].sent:
                self._revoke_execution(self.turns[0])
            self.claude = None   # shutting down: the exits below are not crashes
            for p in list(self._procs):   # also one detached mid-reset or being respawned
                await p.kill()

    # --- claude process -------------------------------------------------------
    @staticmethod
    def _allowed(cat: dict) -> list[str]:
        return []

    async def _spawn(self) -> bool:
        cat = await asyncio.to_thread(catalogue.get)
        self.allowed = self._allowed(cat) if cat else None   # no jv: no tools (spec §5)
        owner = uuid.uuid4().hex
        cmd = build_cmd(self.claude_cmd, PROMPT, self.allowed, mcp_python=sys.executable,
                        execution_owner=owner) if cat else \
            build_cmd(self.claude_cmd, PROMPT)
        proc = ClaudeProc(cmd, state_dir() / "brain-cwd", execution_owner=owner)
        self._procs = {p for p in self._procs if p.alive}
        try:
            await proc.start()
            self._procs.add(proc)
            if self.claude is not None:   # a concurrent reset/respawn already installed one (M4 review I3)
                await proc.kill()
                return True
        except FileNotFoundError:
            log.error("brain: %s not found; Claude path off", self.claude_cmd[0])
            self.claude_off = "missing"
            await self._flush_turns(OFF)
            return False
        except OSError as e:
            log.warning("brain: claude spawn failed (%s)", e)
            self._bg(self._respawn())
            return False
        self.claude = proc
        self._memory_context_revision = None
        trace.emit("brain", "claude_spawn", self._tid(), pid=proc.pid, mcp=bool(cat),
                   allowed=len(self.allowed or []))
        self.proc_turns, self.last_cost = 0, 0.0   # per process: fresh after a reset or a crash
        self.model = None
        self._bg(self._pump(proc))
        await self._kick()
        return True

    async def _respawn(self) -> None:
        await asyncio.sleep(self.backoff)
        self.backoff = min(self.backoff * 2, BACKOFF_MAX_S)
        if self.claude is None and not self.claude_off:
            await self._spawn()

    async def reset(self, reason: str, old: ClaudeProc | None = None) -> None:
        """Fresh pre-warmed process: context gone (spec §2.4). `old`: a process already detached."""
        for conn, question in list(self.awaiting.items()):
            if question.kind != "confirm":
                self._clear_awaiting(conn, "reset")
        if (old is None or self.claude is old) and self.turns and self.turns[0].sent:
            self._revoke_execution(self.turns[0])
        if old is None:
            old, self.claude = self.claude, None
            self._memory_context_revision = None
        elif self.claude is old:
            self.claude = None
            self._memory_context_revision = None
        if old:
            await old.kill()
        self._logline(f"reset reason={reason}")
        trace.emit("brain", "claude_reset", self._tid(), reason=reason, pid=old.pid if old else None)
        self._followup_reset = None
        if self.claude is None and self.claude_cmd and not self.claude_off:
            await self._spawn()

    async def _flush_turns(self, text: str, only_sent: bool = False) -> None:
        while self.turns and (self.turns[0].sent or not only_sent):
            t = self.turns.popleft()
            t.finished = True
            if t.sent:
                self._revoke_execution(t)
            await self.say(t.conn, text, final=True)
            await self.done(t.conn)

    async def _kick(self) -> None:
        """R4 (QUEUE_IN_CLAUDE = False): send the FIFO head only once the previous turn's result is in."""
        if self._memory_reset_pending:
            return
        if self._followup_reset and not self.awaiting and not (
                self.turns and (self.turns[0].sent or self.turns[0].followup)):
            return await self.reset(self._followup_reset)
        reset_memory = False
        async with self._send_lock:
            if self._memory_reset_pending:
                return
            if self._ready() and (wait := self.last_send + self.MIN_TURN_GAP_S - time.monotonic()) > 0:
                await asyncio.sleep(wait)
            if not self._ready() or any(q.kind == "confirm" for q in self.awaiting.values()) or self._memory_reset_pending:
                # A confirmation stays attached to its current action; the finisher kicks the queue again.
                return
            turn, proc = self.turns[0], self.claude
            if not self.rate.claude_allowed(time.time()):
                self.turns.popleft()
                await self.say(turn.conn, self.rate.limit(), final=True)
                await self.done(turn.conn)
                self._bg(self._kick())
                return
            # Read after the gap, immediately before dispatch. A revision change retires the old
            # conversation outside _send_lock; queued Turn objects never capture memory.
            snapshot = user_memory.read_memory()
            memory_changed = self._memory_context_revision is not None and snapshot.revision != self._memory_context_revision
            if memory_changed and not (self._model_waiting() or turn.followup):
                self._memory_reset_pending = True
                reset_memory = True
            else:
                if memory_changed:
                    self._followup_reset = self._followup_reset or "memory"
                if snapshot.revision != self._memory_logged_revision:
                    size = "unknown" if snapshot.size is None else str(snapshot.size)
                    self._logline(f"memory status={snapshot.status} revision={snapshot.revision} "
                                  f"size={size} reason={snapshot.reason or '-'}")
                    self._memory_logged_revision = snapshot.revision
                try:
                    turn.execution_attempt = catalogue.set_current_turn(turn.tid, owner=proc.execution_owner)
                except OSError as e:
                    self.turns.popleft()
                    trace.error("brain", turn.tid, "execution gate setup", e)
                    await self.say(turn.conn, "I couldn't prepare safe action execution; please ask again.", final=True)
                    await self.done(turn.conn)
                    self._bg(self._kick())
                    return
                self.last_send = turn.sent_at = time.monotonic()
                turn.sent = True
                if turn.tool_output:
                    self._tainted = proc
                message = user_memory.user_message(snapshot, turn.text) if self._memory_context_revision is None else turn.text
                if not await proc.send(message):
                    if proc is not self.claude:
                        return
                    self.claude = None
                    self._memory_context_revision = None
                    await self._flush_turns(RESTARTED, only_sent=True)
                    self._bg(self.reset("send", proc))
                    return
                if proc is self.claude and self._memory_context_revision is None:
                    self._memory_context_revision = snapshot.revision
        if reset_memory:
            # reset() spawns a fresh process whose own _kick is held by the pending flag. Always release it
            # and kick again, including when spawning fails and a later respawn must carry the queued turn.
            try:
                await self.reset("memory")
            finally:
                self._memory_reset_pending = False
            await self._kick()
            return
        self._bg(self._after(turn, turn.sent_at, self.WORKING_S, self._working))
        self._bg(self._after(turn, turn.sent_at, self.WALL_CAP_S, self._too_long))

    def _ready(self) -> bool:
        return bool(self.turns) and not self.turns[0].sent and self.claude is not None and self.claude.alive

    async def _after(self, turn: Turn, sent_at: float, delay: float, fn) -> None:
        """fn(turn) `delay` s after the send, if that send is still in flight."""
        await asyncio.sleep(delay)
        if self.turns and self.turns[0] is turn and turn.sent_at == sent_at and not turn.interrupted:
            await fn(turn)

    async def _working(self, turn: Turn) -> None:
        question = self.awaiting.get(turn.conn)
        if not turn.terminal and not turn.spoken and not (question and question.kind == "confirm"):
            await self.say(turn.conn, WORKING)

    async def _too_long(self, turn: Turn) -> None:
        await self.say(turn.conn, TOO_LONG, final=True)
        await self.interrupt()

    async def _pump(self, proc: ClaudeProc) -> None:
        while True:
            ev = await proc.events.get()
            if proc is not self.claude:   # killed or replaced: its leftovers mean nothing
                return
            try:
                await self._on_event(ev)
            except Exception as e:
                log.exception("brain: claude event %r failed", ev.get("type"))
                trace.error("brain", self._tid(), f"claude event {ev.get('type')}", e)
            if ev.get("type") == "_exit":
                return

    async def _on_event(self, ev: dict) -> None:
        turn = self.turns[0] if self.turns and self.turns[0].sent else None
        if turn:
            turn.raw_lines.append(_stream_record(turn, ev))
        kind = ev.get("type")
        if kind in ("assistant", "user"):
            self._note_taint(ev)
        if kind == "system" and ev.get("subtype") == "init":
            tools = [str(t) for t in ev.get("tools") or []]
            if bad := [t for t in tools if not t.startswith("mcp__jv__")]:
                await self._tools_off(bad)
            elif self.allowed is not None and not tools:
                await self._mcp_failed()
            else:
                self.mcp_fails = 0
        elif kind == "stream_event" and turn and not turn.terminal and not turn.interrupted and \
                not any(name in PERSONAL_TOOLS for name in turn.tools) and not ev.get("parent_tool_use_id"):   # not a subagent's text
            e = ev.get("event") or {}
            d = e.get("delta") or {}
            if e.get("type") == "content_block_delta" and d.get("type") == "text_delta":
                for s in turn.splitter.feed(str(d.get("text", ""))):
                    turn.spoken = True
                    await self.say(turn.conn, s)
        elif kind == "assistant" and turn and not ev.get("parent_tool_use_id"):
            u = (ev.get("message") or {}).get("usage")
            turn.context_tokens = sum(int(u.get(k) or 0) for k in
                                      ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")) if u else None
            if model := _model_family((ev.get("message") or {}).get("model")):
                turn.model = self.model = model
            blocks = (ev.get("message") or {}).get("content") or []
            for c in blocks:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    name = self._inner(c.get("name"), c.get("input"))
                    if name == "ctx_ctx":
                        turn.sensitive_context = True
                    turn.tools.append(name)
                    turn.tool_ids[c.get("id")] = name
                    turn.tool_msgs[c.get("id")] = (ev.get("message") or {}).get("id")
                    act(turn, c.get("id"), name)
                    trace.emit("brain", "tool_use", turn.tid, name=name, args=c.get("input"), id=c.get("id"))
        elif kind == "user" and turn:
            for c in (ev.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    body = c.get("content")
                    text = body if isinstance(body, str) else " ".join(
                        str(b.get("text", "")) for b in body or [] if isinstance(b, dict))
                    name = turn.tool_ids.get(c.get("tool_use_id"))
                    data = _json(text)
                    if name in {"zen_play", "media_media"}:
                        data["say"] = play_result.take(data.get("receipt"), turn.tid, turn.execution_attempt) or data.get("say")
                    entry = next((e for e in self.router.cat.values() if catalogue.tool_name(e) == name), {})
                    data = observation_result(data, entry.get("observes", False))
                    outcome = act(turn, c.get("tool_use_id"), name)
                    if outcome["state"] != "denied":
                        outcome.update(state="failed" if c.get("is_error") or data.get("ok") is False else "ran",
                                       say=data.get("say"))
                    if outcome["state"] != "denied" and entry.get("observes") and data.get("ok") is True and \
                            data.get("say") == UNCONFIRMED:
                        self._terminal(turn, UNCONFIRMED)
                    # A parallel denial can arrive before the admitted write's result.
                    if name in PERSONAL_TOOLS and outcome["state"] != "denied":
                        outcome["say"] = data.get("say") or PERSONAL_FALLBACK[name]
                        if name in PERSONAL_WRITES and not turn.terminal and not turn.interrupted:
                            self._terminal(turn, outcome["say"])
                    if name == "media_media" and outcome["state"] != "denied" and not turn.terminal:
                        if question := self._media_question(turn.conn, data, owner=turn):
                            self._terminal(turn, question, expect_reply=True)
                        else:
                            self._terminal(turn, data.get("say") or "I couldn't verify the media action.")
                    if name == "zen_ask" and not turn.terminal:
                        self._terminal(turn, data.get("say") or
                                       "I couldn't confirm the Claude.ai request. Check the chat before trying again.")
                    if name == "zen_play" and outcome["state"] != "denied" and not turn.terminal:
                        self._terminal(turn, data.get("say") or "I couldn't confirm the video started.")
                    if name == "ctx_ctx":
                        trace.emit("brain", "tool_result", turn.tid, name=name,
                                   is_error=bool(c.get("is_error")), chars=len(text), id=c.get("tool_use_id"))
                    else:
                        trace.emit("brain", "tool_result", turn.tid, name=name,
                                   is_error=bool(c.get("is_error")), text=text, id=c.get("tool_use_id"))
        elif kind == "result" and turn:
            await self._on_result(turn, ev)
        elif kind == "rate_limit_event":
            if s := self.rate.update(ev.get("rate_limit_info") or {}):
                await (self.say(turn.conn, s) if turn else self.announce(s))
        elif kind == "control_request" and (ev.get("request") or {}).get("subtype") == "can_use_tool":
            self._bg(self._can_use_tool(self.claude, turn, ev.get("request_id"), ev["request"]))   # pump keeps reading
        elif kind == "control_response":
            pass  # ClaudeProc resolves correlated control waiters in its stdout reader, before queueing this event.
        elif kind == "_exit":
            log.warning("brain: claude exited (code %s); respawning in %s s", ev.get("code"), self.backoff)
            trace.emit("brain", "claude_exit", turn.tid if turn else None, code=ev.get("code"), backoff_s=self.backoff)
            self.claude = None
            self._memory_context_revision = None
            await self._flush_turns(RESTARTED, only_sent=True)
            self._bg(self._respawn())

    def _note_taint(self, ev: dict) -> None:
        """Whatever the turn's state: the text lands in the conversation anyway. At tool_use, so taint from any earlier
        message is always seen. A parallel call in the same message may not see it (the real stream sends one event
        per block, and a permission request can land between them): safe, since a model can't see a tool's result in
        the message that calls it, and the next Enter asks. A failed call's message can quote window names, whichever
        tool it was."""
        for c in (ev.get("message") or {}).get("content") or []:
            if isinstance(c, dict) and (c.get("type") == "tool_result" and c.get("is_error") or
                                        c.get("type") == "tool_use" and
                                        self._inner(c.get("name"), c.get("input"), full=True) not in catalogue.CLEAN_TOOLS):
                if self._tainted is not self.claude:
                    self._taint_by = c.get("id") if c.get("type") == "tool_use" else None
                self._tainted = self.claude

    def _inner(self, tool, inp, full: bool = False) -> str:
        """The tool a call acts as: for mcp__jv__jv_run its inner verb when that resolves, else the tool itself.
        full keeps the mcp__jv__ prefix (CLEAN_TOOLS form)."""
        tool = str(tool)
        entry, _ = catalogue.resolve(self.router.cat, catalogue.RUN, inp) if tool == "mcp__jv__" + catalogue.RUN else (None, None)
        name = "mcp__jv__" + catalogue.tool_name(entry) if entry else tool
        return name if full else name.removeprefix("mcp__jv__")

    async def _tools_off(self, bad: list) -> None:
        log.error("brain: claude listed non-jv tools %s; Claude path off", bad)
        trace.emit("brain", "claude_off", self._tid(), reason="tools", tools=bad)
        proc, self.claude, self.claude_off = self.claude, None, "tools"
        self._memory_context_revision = None
        await self._flush_turns(OFF)
        await proc.kill()

    async def _can_use_tool(self, proc: ClaudeProc, turn: Turn | None, rid, req: dict) -> None:
        """Gate every Claude call; acting calls get a grant after preflight and any spoken yes."""
        inp = req.get("input") if isinstance(req.get("input"), dict) else {}
        res = {"behavior": "deny", "message": NOT_ALLOWED}
        rc, why, readback, answer = None, "not-allowed", None, None
        tainted = self._tainted is proc
        ours = str(req.get("tool_name")).startswith("mcp__jv__")
        outer = str(req.get("tool_name")).removeprefix("mcp__jv__")
        via_run = ours and outer == catalogue.RUN
        # jv_run resolves to its inner verb BEFORE the gate: effect, taint, read-back and grant key on that verb and
        # its args alone; an unknown verb or a stray field leaves entry None and is refused, nothing spawned.
        entry, args = catalogue.resolve(self.router.cat, outer, inp) if ours else (None, {})
        name = catalogue.tool_name(entry) if entry else outer
        if via_run:
            inp = args
            res["message"] = NOT_ALLOWED if entry else UNKNOWN_VERB
        tool_id = req.get("tool_use_id")
        if tool_id is None and turn:
            tool_id = next((i for i, n in reversed(turn.tool_ids.items()) if n == name), rid)
        try:
            if req.get("tool_name") == "mcp__jv__ask_user":
                question, options = inp.get("question"), inp.get("options", [])
                valid = (not inp.keys() - {"question", "options"} and isinstance(question, str)
                         and bool(question.strip()) and len(question) <= 200 and isinstance(options, list)
                         and len(options) <= 5 and all(isinstance(s, str) and len(s) <= 40 for s in options))
                if valid and turn and not (turn.finished or turn.terminal or turn.interrupted):
                    question = textwrap.shorten(question, SENTENCE_MAX, placeholder="...")
                    if self._set_awaiting(turn.conn, Awaiting(
                            asyncio.get_running_loop().create_future(), True, uuid.uuid4().hex,
                            question, time.monotonic() + self.AWAIT_S, owner=turn,
                            kind="choice" if options else "free", source="model",
                            options={router.normalise(s): None for s in options}, original=turn.route_text or turn.text,
                            labels={router.normalise(s): s for s in options})):
                        self._terminal(turn, question, expect_reply=True)
                        res = {"behavior": "allow", "updatedInput": inp}
                trace.emit("brain", "permission", turn.tid if turn else None, tool=name, args=inp,
                           dry_run_rc=None, readback=None, answer=None, decision=res["behavior"],
                           reason="ask-user" if res["behavior"] == "allow" else "refused", tainted=tainted)
                await proc.respond(rid, res)
                return
            if ours and outer == catalogue.FIND:   # reads only our own catalogue text; spawns nothing
                if turn and not (turn.finished or turn.terminal or turn.interrupted):
                    res = {"behavior": "allow", "updatedInput": inp}
                trace.emit("brain", "permission", turn.tid if turn else None, tool=name, args=inp, dry_run_rc=None,
                           readback=None, answer=None, decision=res["behavior"], reason="find", tainted=tainted)
                await proc.respond(rid, res)
                return
            if turn and not (turn.finished or turn.terminal or turn.interrupted) and entry:
                reading = entry.get("effect") == "read" or (
                    entry["name"] in {"volume", "brightness"} and inp.get("action") == "get" or
                    entry["name"] == "media" and inp.get("action") == "status")
                if not reading:
                    if turn.acting_calls >= 4:
                        act(turn, tool_id, name).update(state="denied", reason="steps", say=STEP_LIMIT)
                        trace.emit("brain", "permission", turn.tid, tool=name, args=inp, dry_run_rc=None,
                                   readback=None, answer=None, decision="deny", reason="step-limit", tainted=tainted)
                        await proc.respond(rid, {"behavior": "deny", "message": STEP_LIMIT})
                        return
                    turn.acting_calls += 1   # before awaiting preflight: parallel calls share the budget
            if turn and not turn.finished and name in PERSONAL_WRITES:
                if turn.personal_write or turn.terminal:
                    act(turn, tool_id, name).update(state="denied", reason="refused", say=PERSONAL_SECOND)
                    await proc.respond(rid, {"behavior": "deny", "message": PERSONAL_SECOND})
                    return
                turn.personal_write = True   # before the first await: parallel writes cannot both launch
            if turn and not turn.finished and not turn.terminal and not turn.interrupted and entry:   # after "stop": no
                if tainted and self._taint_by != tool_id and (entry.get("effect") != "read" if via_run else
                                                              entry.get("confirm") is False and
                                                              entry.get("effect") not in ("read", "reversible")):
                    # No read-back exists for these, and outside text may have asked for them: refuse (review item 3).
                    act(turn, tool_id, name).update(state="denied", reason="tainted")
                    trace.emit("brain", "permission", turn.tid, tool=name, args=inp, dry_run_rc=None, readback=None,
                               answer=None, decision="deny", reason="tainted", tainted=True)
                    await proc.respond(rid, {"behavior": "deny", "message": NOT_DONE["tainted"]})
                    return
                rc, msg, source_revision = (0, "", None) if entry.get("effect") == "read" else \
                    await self.dry_run(entry, inp, turn.tid, tainted)
                if turn.finished or turn.terminal or turn.interrupted:
                    act(turn, tool_id, name).update(state="denied", reason="refused")
                    await proc.respond(rid, res)
                    return
                if entry.get("name") == "media" and rc not in (0, 3):
                    data = _json(msg)
                    question = self._media_question(turn.conn, data, owner=turn)
                    msg = question or data.get("say") or msg
                    self._terminal(turn, msg, expect_reply=bool(question))
                    if inp.get("action") != "status":
                        from jv.media import claim
                        claim()   # this real attempt ended at read-only preflight
                yes, heard = await self.ask(turn.conn, msg, turn.speaker_ok, owner=turn,
                                            outcome=act(turn, tool_id, name)) if rc == 3 else (None, "")
                other = yes is False and bool(heard) and confirmation(heard) is None
                if rc == 3:
                    readback, answer = msg, turn.confirm_reason or ("yes" if yes else "timeout" if yes is None else "other" if other else "no")
                if not turn.finished and not turn.terminal and not turn.interrupted and (rc == 0 or yes):
                    updated = dict(inp)
                    if source_revision:
                        updated["source_revision"] = source_revision
                    if entry.get("effect") != "read":
                        grants.mint(entry["name"], updated, turn.tid, turn.execution_attempt or "")
                    res = {"behavior": "allow", "updatedInput": {"verb": name, "args": updated} if via_run else updated}
                    why = "dry-run ok" if rc == 0 else "said-yes"
                else:
                    # dry-run failed: jv's reason, nobody asked
                    res["message"] = (msg if rc != 3 else NO_ANSWER if yes is None else
                                      OTHER_ANSWER if other else SAID_NO)
                    why = msg if rc != 3 else "timeout" if yes is None else "answered" if other else "said-no"
                    if rc == 3 and turn.confirm_reason == "unasked":
                        res["message"], why = NOT_DONE["unasked"], "unasked"
                    if name in PERSONAL_TOOLS and rc != 3 and not turn.terminal:
                        self._terminal(turn, msg or PERSONAL_FALLBACK[name])
        except Exception as e:
            log.exception("brain: can_use_tool failed; denying")
            trace.error("brain", turn.tid if turn else None, "can_use_tool", e)
            why = f"error: {e!r}"
        trace.emit("brain", "permission", turn.tid if turn else None,
                   tool=name, args=inp, dry_run_rc=rc,
                   readback=readback, answer=answer, decision=res["behavior"], reason=why, tainted=tainted)
        if res["behavior"] == "deny":
            if turn:
                act(turn, tool_id, name).update(state="denied", reason=(answer if answer in NOT_DONE else "refused") if rc == 3 else
                                              "preflight" if rc is not None else "refused",
                                              say=msg if rc is not None and rc != 3 else None)
            args = {k: v[:40] if k == "text" and isinstance(v, str) else v for k, v in inp.items()}
            self._logline(f"denied tool={name} rc={rc} "
                          f"reason={why!r} args={json.dumps(args, ensure_ascii=False)}")
        await proc.respond(rid, res)

    async def dry_run(self, entry: dict, args: dict, tid: str | None = None,
                      tainted: bool = False) -> tuple[int, str, str | None]:
        """(rc, read-back sentence or failure reason, optional source/target revision)."""
        rc, out, err = await catalogue.run_jv(catalogue.build_argv(entry, args, "--dry-run", "--json"),
                                              timeout=self.JV_TIMEOUT_S, turn=tid, tainted=tainted)
        data = _json(out)
        source_revision = data.get("source_revision")
        if rc in (0, 3) and args.get('attach'):
            expected_source = 'heard' if args['attach'] == 'last_heard' else args['attach']
            if not isinstance(source_revision, str) or not re.fullmatch(
                    rf'{re.escape(expected_source)}:[0-9a-f]{{64}}', source_revision):
                return 1, "couldn't bind the attachment to this request", None
        if rc in (0, 3) and (args.get("clipboard") or args.get("selection")):
            expected_source = "clipboard" if args.get("clipboard") else "selection"
            if not isinstance(source_revision, str) or not re.fullmatch(
                    rf"{expected_source}:[0-9a-f]{{64}}", source_revision):
                return 1, "couldn't bind the clipboard or selection to this request", None
        if rc == 3 and entry.get("name") == "zen.open":
            if not isinstance(source_revision, str) or not re.fullmatch(r"zen:[0-9a-f]{64}", source_revision):
                return 1, "couldn't bind the browser target to this request", None
        if rc in (0, 3) and entry.get("name") == "media":
            from jv.media import REVISION_RE
            if not isinstance(source_revision, str) or not re.fullmatch(REVISION_RE, source_revision):
                return 1, "couldn't bind the media source to this request", None
        if rc == 3 and isinstance(data.get("confirm"), str) and data["confirm"]:
            return 3, data["confirm"], source_revision
        if rc == 2 and entry.get("name") == "media":
            return rc, out, None   # retain exact player/source candidates for the owning turn
        if rc not in (0, 3) and catalogue.tool_name(entry) in PERSONAL_TOOLS:
            return rc, data.get("say") or PERSONAL_FALLBACK[catalogue.tool_name(entry)], None
        return (rc, "", source_revision) if rc == 0 else (rc if rc != 3 else 1, _reason(rc, data, err), None)

    async def ask_yes(self, conn, sentence: str, speaker_ok_default: bool) -> bool | None:
        return (await self.ask(conn, sentence, speaker_ok_default))[0]

    def _model_waiting(self) -> bool:
        return any(q.source == "model" and q.kind != "confirm" and q.expires > time.monotonic()
                   for q in self.awaiting.values())

    def _clear_awaiting(self, conn, why: str) -> None:
        if question := self.awaiting.pop(conn, None):
            trace.emit("brain", "awaiting_cleared", question.owner.tid if question.owner else None,
                       id=question.id, source=question.source, kind=question.kind, why=why)

    def _set_awaiting(self, conn, question: Awaiting) -> bool:
        if previous := self.awaiting.get(conn):
            if previous.kind == "confirm":
                return False
            self._clear_awaiting(conn, "replaced")
        self.awaiting[conn] = question
        trace.emit("brain", "awaiting_set", question.owner.tid if question.owner else None,
                   id=question.id, source=question.source, kind=question.kind)
        if question.kind != "confirm":
            asyncio.get_running_loop().call_later(max(0, question.expires - time.monotonic()),
                                                 self._expire_awaiting, conn, question)
        return True

    def _expire_awaiting(self, conn, question: Awaiting) -> None:
        if self.awaiting.get(conn) is not question:
            return
        if (left := question.expires - time.monotonic()) > 0:
            asyncio.get_running_loop().call_later(left, self._expire_awaiting, conn, question)
            return
        self._clear_awaiting(conn, "timeout")
        self._bg(self._kick())

    async def ask(self, conn, sentence: str, speaker_ok_default: bool, *, owner: Turn | None = None,
                  outcome: dict | None = None) -> tuple[bool | None, str]:
        """Replay one correlated question within a fixed absolute lifetime, retaining the caller's revision."""
        if conn is None or conn.is_closing():
            if owner:
                owner.confirm_reason = "unasked"
            if outcome is not None:
                outcome.update(state="denied", reason="unasked")
            if owner:
                self._terminal(owner)
            return False, ""
        while (question := self.awaiting.get(conn)) and question.kind == "confirm":
            # a done future is awaited without yielding: sleep(0) lets its owner run its finally and clear awaiting
            await (asyncio.sleep(0) if question.future.done() else asyncio.shield(question.future))
        if owner and (owner.finished or owner.terminal or owner.interrupted):
            return False, ""
        loop = asyncio.get_running_loop()
        lifetime = 2 * (self.CONFIRM_TIMEOUT_S + self.CONFIRM_GRACE_S)
        question = Awaiting(loop.create_future(), speaker_ok_default, uuid.uuid4().hex,
                            textwrap.shorten(sentence, SENTENCE_MAX, placeholder="..."),
                            time.monotonic() + lifetime, owner)
        question.outcome = outcome
        self._set_awaiting(conn, question)
        replay_wait = None
        try:
            while not question.future.done():
                remaining = question.expires - time.monotonic()
                if remaining <= 0:
                    self._answer(conn, None)
                    return None, ""
                question.replay.clear()
                await self._send(conn, {"type": "confirm", "text": question.question,
                                       "confirmation_id": question.id,
                                       "timeout_s": min(self.CONFIRM_TIMEOUT_S, remaining),
                                       "remaining_s": remaining})
                replay_wait = asyncio.create_task(question.replay.wait())
                done, _ = await asyncio.wait((question.future, replay_wait), timeout=remaining,
                                              return_when=asyncio.FIRST_COMPLETED)
                replay_wait.cancel()
                if not done:
                    self._answer(conn, None)
                    return None, ""
            return question.future.result()
        finally:
            if replay_wait is not None:
                replay_wait.cancel()
            if self.awaiting.get(conn) is question:
                self._clear_awaiting(conn, "closed")

    def _answer(self, conn, ok: bool | None, text: str = "", *, unasked: bool = False) -> None:
        question = self.awaiting.get(conn)
        if question is None or question.kind != "confirm" or question.future.done():
            return
        if time.monotonic() >= question.expires:
            ok, text = None, ""
        elif text and match_repeat(text):
            trace.emit("brain", "confirm_repeat", confirmation_id=question.id)
            question.replay.set()
            return
        elif ok and text and confirmation(text) is not True:
            ok = False
        if not ok:
            other = ok is False and bool(text) and confirmation(text) is None
            reason = "unasked" if unasked else "other" if other else "timeout" if ok is None else "no"
            if question.outcome is not None:
                question.outcome.update(state="denied", reason=reason)
                if other:
                    question.outcome.update(readback=question.question, answer=text)
            if question.owner:
                question.owner.confirm_reason = reason
                if other:
                    trace.emit("brain", "readback_other", question.owner.tid, text=text)
                self._terminal(question.owner)
        trace.emit("brain", "awaiting_answer", question.owner.tid if question.owner else None,
                   id=question.id, source=question.source, kind=question.kind,
                   result="yes" if ok else "timeout" if ok is None else "no")
        self._clear_awaiting(conn, "answered")
        question.future.set_result((ok, text))

    def _terminal(self, turn: Turn, sentence: str = "", *, expect_reply: bool = False) -> None:
        """Retire this turn before any await; later model tools/speech cannot reopen its question."""
        if turn.finished or turn.terminal:
            return
        turn.terminal, turn.terminal_expect_reply = sentence or True, expect_reply
        if not self._revoke_execution(turn) and self.claude:
            proc, self.claude = self.claude, None
            self._memory_context_revision = None
            self._bg(self._retire_execution(turn, proc))
        question = self.awaiting.get(turn.conn)
        if not expect_reply and question and question.kind != "confirm" and question.owner is turn:
            self._clear_awaiting(turn.conn, "terminal")

    def _revoke_execution(self, turn: Turn) -> bool:
        try:
            catalogue.block_turn(turn.tid, turn.execution_attempt)
        except OSError as e:
            # A readable allowed file may survive until transport retirement; no rollback is possible.
            trace.error("brain", turn.tid, "execution gate revocation", e)
            return False
        return True

    async def _retire_execution(self, turn: Turn, proc: ClaudeProc) -> None:
        await proc.kill()
        if self.turns and self.turns[0] is turn:
            # Detached event pumps ignore the old result; drain the head ourselves before respawning.
            self.turns.popleft()
            turn.finished = True
            self._answer(turn.conn, False)
            self._write_stream(turn)
            await self.finish(turn)
        await self.reset("execution-gate", proc)

    async def interrupt(self) -> None:
        """Stop the turn in flight with a control_request (never SIGINT); drop the turns queued behind it."""
        for c, question in list(self.awaiting.items()):
            if question.kind == "confirm":
                self._answer(c, False)
            else:
                self._clear_awaiting(c, "interrupt")
        head = self.turns[0] if self.turns and self.turns[0].sent else None
        dropped = [t for t in self.turns if t is not head]
        self.turns = deque([head] if head else [])   # before any await: a result now must not kick a dropped turn
        if head and not head.interrupted:
            head.interrupted = True
            if self.claude:
                if not self._revoke_execution(head):
                    proc, self.claude = self.claude, None
                    self._memory_context_revision = None
                    await self._retire_execution(head, proc)
                    for t in dropped:
                        await self.done(t.conn)
                    return
                proc = self.claude
                await proc.control("interrupt")
                self._bg(self._interrupt_grace(head, proc))
        for t in dropped:
            await self.done(t.conn)

    async def _interrupt_grace(self, turn: Turn, proc: ClaudeProc) -> None:
        await asyncio.sleep(self.INTERRUPT_GRACE_S)
        if self.turns and self.turns[0] is turn and self.claude is proc:   # no result: stuck
            await self._drop_head()
            await self.reset("interrupt")

    async def _drop_head(self) -> None:
        """Abandon the turn in flight: its connection gets `done`; the old process's leftovers are ignored."""
        if self.turns and self.turns[0].sent:
            turn = self.turns.popleft()
            turn.finished = True
            self._revoke_execution(turn)
            conn = turn.conn
            self._answer(conn, False)   # its read-back question, if any, is a no
            await self.done(conn)

    async def _mcp_failed(self) -> None:
        """MCP server didn't load: respawn once (resending the turn); twice in a row → Claude off."""
        self.mcp_fails += 1
        log.error("brain: claude listed no jv tools (MCP failed, %d)", self.mcp_fails)
        trace.emit("brain", "claude_off" if self.mcp_fails >= 2 else "mcp_failed", self._tid(), reason="mcp",
                   fails=self.mcp_fails)
        if self.mcp_fails < 2:
            for t in self.turns:
                if t.sent:
                    self._revoke_execution(t)
                t.sent = False
            return await self.reset("mcp")
        proc, self.claude, self.claude_off = self.claude, None, "mcp"
        self._memory_context_revision = None
        if self.turns:
            t = self.turns.popleft()
            t.finished = True
            if t.sent:
                self._revoke_execution(t)
            await self.say(t.conn, MCP_FAILED, final=True)
            await self.done(t.conn)
        await self._flush_turns(OFF)
        await proc.kill()

    async def _on_result(self, turn: Turn, ev: dict) -> None:
        revoked = self._revoke_execution(turn)
        turn.finished = True
        self.turns.popleft()
        self.backoff = self.BACKOFF_S
        reason, old, asked = None if revoked else "execution-gate", None, False
        if not revoked and self.claude:
            old, self.claude = self.claude, None
            self._memory_context_revision = None
        try:   # whatever fails below, the finally keeps the FIFO moving
            subtype, text = ev.get("subtype"), str(ev.get("result") or "")
            error = bool(ev.get("is_error")) or subtype != "success"
            signed_out = error and "login" in text.lower()
            try:
                bound_reason = self._bound(ev, turn.tools, turn)
                reason = reason or bound_reason
            except Exception as e:
                log.warning("brain: result bounds failed (%r)", e)
            if not reason and error and not signed_out and subtype != "error_max_turns" and not turn.interrupted:
                reason = f"error-{subtype}"   # budget stop or unknown error
            if not turn.terminal and not error and not turn.interrupted and asks(text):
                asked = self._set_awaiting(turn.conn, Awaiting(
                    asyncio.get_running_loop().create_future(), True, uuid.uuid4().hex,
                    textwrap.shorten(text, SENTENCE_MAX, placeholder="..."), time.monotonic() + self.AWAIT_S,
                    owner=turn, kind="free", source="model", original=turn.route_text or turn.text))
            rest = turn.splitter.flush()
            turn.reply_text = text
            turn.reply_lines = list(turn.deferred_speech) + ([rest] if rest else [])
            if not turn.reply_lines and not turn.spoken:
                turn.reply_lines = SentenceSplitter().split(text)
            if error:
                turn.reply_lines = [SIGNED_OUT if signed_out else UNFINISHED]
            asked = turn.terminal_expect_reply if turn.terminal else asked
            turn.terminal_expect_reply = asked
            if asked and not turn.reply_lines:
                turn.reply_lines = [""]   # the streamed question still needs its follow-up flag
            waiting = turn.confirm_reason == "other" and not turn.interrupted or self._model_waiting()
            if reason and reason != "execution-gate" and waiting:
                self._followup_reset = self._followup_reset or reason
                reason = None
            elif not waiting and self._followup_reset:
                reason = reason or self._followup_reset
                self._followup_reset = None
            if reason and old is None:
                old, self.claude = self.claude, None
            if not revoked and old:
                await old.kill()
        finally:
            day_warning = ""
            try:
                self._write_stream(turn)
                n = await asyncio.to_thread(usage.bump, turn.model) if turn.model is not None else 0
                if n >= usage.DAY_TURN_WARN and self.day_warned != (today := date.today()):
                    self.day_warned = today
                    day_warning = usage.day_line(n)
            except Exception as e:   # bookkeeping must never stall the conversation
                log.warning("brain: result bookkeeping failed (%r)", e)
                trace.error("brain", turn.tid, "result bookkeeping", e)
            try:
                await self.finish(turn, day_warning=day_warning)
            finally:
                if reason:
                    self._bg(self.reset(reason, old))
                else:
                    self._bg(self._kick())

    async def finish(self, turn: Turn, *, rc: int | None = None, day_warning: str = "") -> None:
        turn.finished = True
        lines = closing(turn, read_tools={catalogue.tool_name(e) for e in self.router.cat.values()
                                          if e.get("effect") == "read"})
        asked = turn.terminal_expect_reply
        question = self.awaiting.get(turn.conn)
        if asked and question and question.kind != "confirm" and question.owner is turn:
            question.expires = time.monotonic() + self.AWAIT_S
        trace.emit("brain", "reply", turn.tid,
                   text="[local context reply omitted]" if turn.sensitive_context else turn.reply_text or " ".join(lines),
                   chars=len(turn.reply_text) if turn.sensitive_context else None, asked=asked, rc=rc,
                   spoken=(["[local context reply omitted]" for _ in lines] if turn.sensitive_context else lines)
                   if not turn.spoken else None, interrupted=turn.interrupted,
                   acts=[{**a, "say": "[local context reply omitted]"} if turn.sensitive_context and a.get("say") else a
                         for a in turn.acts])
        for i, sentence in enumerate(lines, 1):
            await self.say(turn.conn, sentence, final=i == len(lines), expect_reply=asked and i == len(lines), tid=turn.tid)
        if day_warning:
            await self.say(turn.conn, day_warning, final=True)
        other = next((a for a in turn.acts if a.get("reason") == "other" and a.get("answer")), None)
        if other and not turn.interrupted:
            # The follow-up owns done, including when Claude queues it for a later result.
            await self.to_claude(correction(other["readback"], other["answer"], turn.route_text or turn.text),
                                 turn.conn, self.new_tid(), followup=True, parent=turn.tid)
        else:
            await self.done(turn.conn)

    def _bound(self, ev: dict, tools: list = (), turn: Turn | None = None) -> str | None:
        """Bookkeeping after a result; the reason to reset, if any (spec §2.4)."""
        self.proc_turns += 1
        u = ev.get("usage") or {}
        tokens = sum(int(u.get(k) or 0) for k in
                     ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        cost = float(ev.get("total_cost_usd") or 0)
        msg_cost, self.last_cost = cost - self.last_cost, cost
        trace.emit("brain", "result", turn.tid if turn else None, proc_turns=self.proc_turns,
                   num_turns=ev.get("num_turns"), tokens=tokens, context_tokens=turn.context_tokens if turn else None, output_tokens=u.get("output_tokens"),
                   cost=round(msg_cost, 5), total_cost=round(cost, 5), model=turn.model if turn else None,
                   ms=round((time.monotonic() - turn.sent_at) * 1000) if turn else None,
                   api_ms=ev.get("duration_api_ms"), stop_reason=ev.get("stop_reason"), subtype=ev.get("subtype"),
                   is_error=bool(ev.get("is_error")), tools=list(tools))
        self._logline(f"result turns={self.proc_turns} tokens={tokens} cost={msg_cost:.4f} total={cost:.4f}"
                      + (f" tools={','.join(tools)}" if tools else ""))
        if turn and turn.context_tokens is not None and turn.context_tokens > self.TOKEN_RESET:
            return "tokens"
        if cost > self.COST_RESET:
            return "cost"
        if self.proc_turns >= self.TURN_RESET:
            return "turns"
        if msg_cost > self.MSG_COST_RESET and not (turn and self.proc_turns == 1):
            return "message-cost"
        return None

    def _arm_idle(self) -> None:
        if self._idle:
            self._idle.cancel()
        self._idle = asyncio.get_running_loop().call_later(self.IDLE_RESET_S, lambda: self._bg(self._idle_reset()))

    async def _idle_reset(self) -> None:
        if self.turns:   # still talking: wait for the next silence
            return self._arm_idle()
        if self.claude and self.proc_turns:
            await self.reset("idle")   # in the background, so the next turn finds a warm process

    def _write_stream(self, turn: Turn) -> None:
        try:
            (state_dir() / "brain-stream.jsonl").write_text("".join(json.dumps(e) + "\n" for e in turn.raw_lines))
        except OSError:
            log.warning("brain: cannot write brain-stream.jsonl")

    async def _client(self, reader: asyncio.StreamReader, conn: asyncio.StreamWriter) -> None:
        """The reader handles control messages, confirm answers and "stop" at once; other utterances go to this
        connection's worker in order (a fast-path jv call never delays an interrupt)."""
        q: asyncio.Queue = asyncio.Queue()
        self._bg(self._worker(q, conn))
        try:
            while line := await reader.readline():
                try:
                    msg = json.loads(line)
                except ValueError:
                    log.warning("brain: bad json line %r", line[:200])
                    continue
                if not isinstance(msg, dict):
                    continue
                question = self.awaiting.get(conn)
                if msg.get("type") == "utterance" and not (question and question.kind == "confirm") and \
                        router.normalise(str(msg.get("text") or "")) not in STOP:
                    q.put_nowait(msg)
                    continue
                await self._handle_safe(msg, conn)
        except (ConnectionError, asyncio.IncompleteReadError, ValueError):   # ValueError: line over limit
            pass
        finally:
            q.put_nowait(None)
            self._answer(conn, False)   # Review Focus 2: a dead connection's confirm is a no, at once
            self._clear_awaiting(conn, "disconnect")
            self._bg(self._kick())
            if self.voice is conn:
                self.voice = None
            conn.close()

    async def _worker(self, q: asyncio.Queue, conn) -> None:
        while (msg := await q.get()) is not None:
            if not conn.is_closing():   # a dead connection's queued utterances are dropped
                # never handle(): an utterance read before a confirm opened must not answer it (M4 review I1)
                await self._safe(self._intake(msg, conn), msg)

    async def _intake(self, msg: dict, conn) -> None:
        """Only external trusted utterances shift history, never synthetic correction reroutes."""
        tid = str(msg.get('turn') or self.new_tid())
        text = str(msg.get('text') or '')
        speaker_ok = conn is not None and conn is self.voice and bool(msg.get('speaker_ok'))
        if speaker_ok and text.strip() and confirmation(text) is None:
            from jv import attach
            path = Path(runtime_path('heardback-heard.json'))
            data = {'current': {'turn': tid, 'text': text}, 'previous': attach.heard().get('current')}
            fd, tmp = tempfile.mkstemp(prefix='.heardback-heard-', dir=path.parent)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    json.dump(data, stream)
                os.replace(tmp, path)
            finally:
                Path(tmp).unlink(missing_ok=True)
        await self.utterance(text, speaker_ok, conn, tid, msg.get('parent'))

    async def _handle_safe(self, msg: dict, conn) -> None:
        await self._safe(self.handle(msg, conn), msg)

    async def _safe(self, coro, msg: dict) -> None:
        try:
            await coro
        except Exception as e:
            log.exception("brain: handling %r failed", msg.get("type"))
            trace.error("brain", msg.get("turn"), f"handling {msg.get('type')}", e)

    async def _send(self, conn: asyncio.StreamWriter | None, msg: dict) -> None:
        if conn is None or conn.is_closing():
            return
        try:
            conn.write((json.dumps(msg) + "\n").encode())
            await conn.drain()
        except (ConnectionError, RuntimeError):
            pass

    async def say(self, conn, text: str, final: bool = False, expect_reply: bool = False, *, tid: str | None = None) -> None:
        msg = {"type": "say", "text": text, "final": final}
        if tid or self._tid():
            msg["turn"] = tid or self._tid()
        if expect_reply:
            msg["expect_reply"] = True
        await self._send(conn, msg)

    async def done(self, conn) -> None:
        await self._send(conn, {"type": "done"})

    async def announce(self, text: str | dict, ask: dict | None = None) -> None:
        event = text if isinstance(text, dict) else {"kind": "done", "text": text}
        if ask:
            event = {**event, "ask": ask}
        self.events.add(event)
        await self.flush_events()

    async def event_policy(self, event: dict) -> str:
        muted = await asyncio.to_thread(away.is_muted, time.time())
        voice = self.voice
        return speak_policy(event, {
            "muted": muted, "busy": self.voice_busy or bool(self.turns) or bool(self._intakes),
            "connected": voice is not None and not voice.is_closing(),
        }, voice in self.awaiting)

    async def flush_events(self) -> None:
        if self._event_lock.locked():
            return
        async with self._event_lock:
            seen = {}
            while event := next((e for e in self.events.rows if id(e) not in seen), None):
                seen[id(event)] = event  # include new notices without retrying held events in this flush
                policy = await self.event_policy(event)
                if policy == "hold":
                    continue
                if policy == "drop":
                    self.events.remove(event)
                    continue
                if event["kind"] == "reminder":
                    if self.coordinator is None or not await self.coordinator.deliver(event):
                        continue
                    self.events.remove(event)
                    continue
                at_desk = event["kind"] in {"session", "back"} and not await asyncio.to_thread(away.is_away)
                if event.get("states") and not await asyncio.to_thread(self.announcer.valid, event):
                    self.events.remove(event)
                    continue
                if await self.event_policy(event) != "now":
                    continue
                # shortcut: consume before speech to avoid repeats; add voice receipts for guaranteed delivery.
                self.events.remove(event)
                if at_desk:
                    text = event.get("desk_text", "") if event["kind"] == "session" else event["text"]
                    if text:
                        if event["kind"] == "session":
                            self.announcer._cues().play("ready")
                        self.announcer._cues().notify(text)
                else:
                    await self._announce(event["text"], event.get("ask"))

    async def _announce(self, text: str, ask: dict | None = None) -> None:
        """ask: the alert read a dialog out; voice listens once after it, and the reply may answer it."""
        msg = {"type": "say", "text": text, "final": True, "announce": True}
        if ask and self.voice is not None and self.voice not in self.awaiting:
            choices = {c: c for c in ask["choices"]}
            choices.update(yes="1", no=ask["no"])
            options = {label: router.Route("tmux.answer", {"target": ask["pane"], "choice": choice}, 1.0)
                       for label, choice in choices.items()}
            self._set_awaiting(self.voice, Awaiting(
                asyncio.get_running_loop().create_future(), True, uuid.uuid4().hex,
                text, time.monotonic() + self.AWAIT_S, kind="choice", source="alert", options=options))
            msg["expect_reply"] = True
        await self._send(self.voice, msg)

    # --- messages -------------------------------------------------------------
    async def handle(self, msg: dict, conn) -> None:
        kind = msg.get("type")
        if kind == "hello":
            if msg.get("role") == "voice":
                token = msg.get("token")
                if not (self.voice_token and isinstance(token, str) and token.isascii() and
                        hmac.compare_digest(self.voice_token, token)):
                    trace.emit("brain", "hello_refused", msg.get("turn"), reason="invalid voice token")
                    if self.voice is conn:
                        self.voice = None
                    return
                self.voice = conn
                self.voice_busy = False
                if self.coordinator is not None:
                    self._bg(self.coordinator.notify_failure())
        elif kind == "delivery":
            if self.coordinator is not None:
                self.coordinator.reply(msg, conn)
        elif kind == "say":
            if isinstance(msg.get("text"), str):
                await self.announce(msg["text"])
        elif kind == "state":
            if conn is self.voice:
                self.voice_busy = bool(msg.get("busy"))
                self._bg(self.flush_events())
        elif kind == "interrupt":
            await self.interrupt()
        elif kind == "confirm_reply":
            question = self.awaiting.get(conn)
            is_voice = conn is not None and conn is self.voice
            trusted = is_voice and bool(msg.get("speaker_ok", question.speaker_default if question else False))
            trace.emit("brain", "confirm_reply", msg.get("turn"), parent=msg.get("parent"), ok=msg.get("ok"),
                       text=msg.get("text"), speaker_ok=trusted, unasked=bool(msg.get("unasked")),
                       waiting=conn in self.awaiting, awaiting_kind=question.kind if question else None)
            if question is not None and question.kind == "confirm":
                if msg.get("confirmation_id") != question.id:
                    return   # delayed/wrong-attempt voice replies cannot approve a new question
                ok, heard = bool(msg.get("ok")) and trusted, str(msg.get("text") or "").strip()
                if msg.get("unasked"):
                    self._answer(conn, False, unasked=True)
                else:
                    self._answer(conn, None if is_voice and not ok and not heard else ok,
                                 heard if trusted else "")
        elif kind == "utterance":
            text = str(msg.get("text") or "")
            speaker_ok = conn is not None and conn is self.voice and bool(msg.get("speaker_ok"))
            question = self.awaiting.get(conn)
            if question and question.kind == "confirm":   # an answer on the asking connection has no done of its own
                self._log("confirm", None, None, time.monotonic(), text)
                trace.emit("brain", "confirm_reply", msg.get("turn"), parent=msg.get("parent"), text=text,
                           typed=True)
                if msg.get("parent") and (not question.owner or msg["parent"] != question.owner.tid):
                    return   # a delayed answer to a replaced model question cannot approve this read-back
                return self._answer(conn, speaker_ok and match_confirm(text) is True, text if speaker_ok else "")
            await self._intake(msg, conn)

    async def utterance(self, text: str, speaker_ok: bool, conn, tid=None, parent=None) -> None:
        self._intakes += 1
        t0 = time.monotonic()
        tid = str(tid) if tid else self.new_tid()
        route, verb = "drop", None
        followup = False
        answer_slot = None
        model_label = None

        def chose(r: str, **f) -> None:
            nonlocal route
            route = r
            trace.emit("brain", "route", tid, route=r, text=text, parent=parent, **f)
        try:
            if not speaker_ok:
                question = self.awaiting.get(conn)
                if question and question.source == "media":
                    self._clear_awaiting(conn, "untrusted")
                if conn is None or conn is self.voice:
                    return chose("drop", why="speaker_ok false")
                cat = await asyncio.to_thread(catalogue.get)
                r = router.Router(cat).route(text)
                if r and cat[r.verb].get("effect") == "read":
                    verb = r.verb
                    chose("fast", verb=r.verb, args=r.args, why="untrusted read")
                    rc, out, err = await catalogue.run_jv(catalogue.build_argv(cat[r.verb], r.args, "--json"),
                                                          timeout=self.JV_TIMEOUT_S, turn=tid)
                    data = _json(out)
                    await self.say(conn, (data.get("say") or "Done.") if rc == 0 else spoken(rc, data, err), final=True)
                else:
                    chose("drop", why="unauthenticated request")
                    await self.say(conn, "Actions need an authenticated voice request.", final=True)
                return await self.done(conn)
            self._arm_idle()
            question = self.awaiting.get(conn)
            if question and question.kind != "confirm":
                if time.monotonic() >= question.expires:
                    self._clear_awaiting(conn, "timeout")
                elif not parent and question.source != "alert":
                    self._clear_awaiting(conn, "new_wake")
                else:
                    answer = answer_for(question, text)
                    trace.emit("brain", "awaiting_answer", tid, id=question.id,
                               source=question.source, kind=question.kind,
                               result="route" if isinstance(answer, router.Route) else answer)
                    if isinstance(answer, router.Route):
                        cat = await asyncio.to_thread(catalogue.get)
                        self.router = router.Router(cat)
                        if answer.verb in cat:
                            self._clear_awaiting(conn, "answered")
                            verb = answer.verb
                            chose("fast", verb=verb, args=answer.args, score=answer.score,
                                  why=f"{question.source} answer")
                            return await self.run_route(answer, conn, text, tid)
                    followup = answer == "model"
                    if followup:
                        answer_slot = question   # cleared after the catalogue read below
                        label = re.sub(r"^(?:on|in|use|choose|pick|option|answer|press|say) ", "", router.normalise(text))
                        if question.source == "media":
                            text = correction(question.question, text, question.original)
                        elif question.kind == "choice" and label in question.options:
                            model_label = question.labels.get(label, label)
                    else:
                        self._clear_awaiting(conn, "answered")
            norm = router.normalise(text)
            if model_label is None and (len(text.strip()) < 2 or norm in IGNORE):
                chose("ignore")
                return await self.done(conn)
            if norm in STOP:
                chose("builtin", verb="stop")
                await self.interrupt()
                return await self.done(conn)
            if norm in RESET:
                chose("builtin", verb="reset")
                await self.say(conn, FRESH, final=True)
                await self._drop_head()
                await self.reset("user")
                return await self.done(conn)
            cat = await asyncio.to_thread(catalogue.get)
            if answer_slot is not None and self.awaiting.get(conn) is answer_slot:
                self._clear_awaiting(conn, "answered")
            if cat is not self.router.cat:
                self.router = router.Router(cat)
            if model_label is not None or (answer_slot is not None and answer_slot.source == "media"):
                route = "claude"
                return await self.to_claude(model_label or text, conn, tid, followup=True, parent=parent)
            r = self.router.route(text)
            if r and r.score < 1.0 and followup:
                r = None
            if r:
                verb = r.verb
                chose("fast", verb=r.verb, args=r.args, score=round(r.score, 3))
                return await self.run_route(r, conn, text, tid)
            route = "claude"
            await self.to_claude(text, conn, tid, followup=followup,
                                 parent=parent)
        finally:
            self._intakes -= 1
            if answer_slot is not None and self.awaiting.get(conn) is answer_slot:
                self._clear_awaiting(conn, "answered")
            if route != "claude":
                self._log(route, verb, None, t0, text)
                self._bg(self._kick())

    async def run_route(self, route: router.Route, conn, text: str = "", tid: str | None = None) -> None:
        entry = self.router.cat[route.verb]
        rc, out, err = await catalogue.run_jv(catalogue.build_argv(entry, route.args, "--json"),
                                              timeout=self.JV_TIMEOUT_S, turn=tid)
        data = _json(out)
        if rc == 3 and isinstance(sentence := data.get("confirm"), str) and sentence:
            # in the background: this connection's reader must stay free to read the answer
            return self._bg(self._confirm_fast(route, entry, conn, text, sentence, tid,
                                               data.get("source_revision")))
        await self._speak_jv(route, rc, out, err, conn, text, tid)

    async def _confirm_fast(self, route: router.Route, entry: dict, conn, text: str, sentence: str,
                            tid: str | None = None, source_revision: str | None = None) -> None:
        turn = Turn(conn, text, tid=tid)
        outcome = act(turn, "fast", entry.get("name", "fast"))
        self._intakes += 1
        try:
            yes, heard = await self.ask(conn, sentence, True, owner=turn, outcome=outcome)
            if not yes:
                outcome.update(state="denied", reason=turn.confirm_reason)
                return await self.finish(turn)
            args = {**route.args, **({"source_revision": source_revision} if isinstance(source_revision, str) else {})}
            rc, out, err = await catalogue.run_jv(catalogue.build_argv(entry, args, "--json", "--yes"),
                                                  timeout=self.JV_TIMEOUT_S, turn=tid)
            await self._speak_jv(route, rc, out, err, conn, text, tid, turn=turn)
        finally:
            self._intakes -= 1
            self._bg(self._kick())

    async def _speak_jv(self, route: router.Route, rc: int, out: str, err: str, conn, text: str,
                        tid: str | None = None, *, turn: Turn | None = None) -> None:
        private = self.router.cat.get(route.verb, {}).get("personal") or route.verb.partition(".")[0] in {"reminder", "inbox", "fact", "catchup"}
        self.recent.append((time.time(), route.verb, {} if private else route.args, rc == 0))
        self.unshown = min(self.unshown + 1, self.RECENT_MAX)
        data = _json(out)
        data = observation_result(data, self.router.cat.get(route.verb, {}).get("observes", False))
        turn = turn or Turn(conn, text, tid=tid)
        act(turn, "fast", route.verb).update(state="ran" if rc == 0 else "failed",
                                             say=data.get("say") if rc == 0 else spoken(rc, data, err))
        if rc == 1 and data.get("handoff") is True and self.claude_cmd and not self.claude_off and \
                self.rate.claude_allowed(time.time()):
            return await self.to_claude(text, conn, tid, acts=turn.acts)
        if rc == 2 and route.verb == "media" and (question := self._media_question(conn, data, owner=turn)):
            turn.terminal, turn.terminal_expect_reply = question, True
            return await self.finish(turn, rc=rc)
        if rc == 2:   # carry candidate information to Claude rather than blindly repeat the same request
            return await self.to_claude(f"{text}\nTool could not choose a target: {out}. Ask one clarification; do not retry unchanged.", conn, tid, tool_output=True, acts=turn.acts)
        if rc != 0:
            turn.terminal = act(turn, "fast", route.verb)["say"]
        await self.finish(turn, rc=rc)

    def _media_question(self, conn, data: dict, *, owner: Turn | None = None) -> str | None:
        if data.get("clarification") == "music_source":
            question = data.get("say") or "Where should I play music—Spotify or YouTube?"
            options = {"youtube": None}
            for label, source in {"spotify": data.get("source"), "spotify app": "desktop", "desktop spotify": "desktop",
                                  "spotify website": "site", "spotify site": "site", "spotify in zen": "site"}.items():
                args = {"action": data.get("action") or "play", "target": "spotify"}
                if source:
                    args["source"] = source
                options[router.normalise(label)] = router.Route("media", args, 1.0)
        elif data.get("clarification") == "media_player" and data.get("action") in {
                "play", "pause", "toggle", "next", "previous", "status"} and \
                isinstance(data.get("candidates"), list) and data["candidates"] and \
                all(isinstance(p, str) and p for p in data["candidates"]) and isinstance(data.get("player_labels"), dict) and \
                all(isinstance(data["player_labels"].get(p), str) for p in data["candidates"]):
            question = data.get("say") or "Which player should I use?"
            options = {}
            labels = [router.normalise(data["player_labels"][p]) for p in data["candidates"]]
            names = [router.normalise(p) for p in data["candidates"]]
            for player, label, name in zip(data["candidates"], labels, names):
                args = {"action": data["action"], "player": player}
                args.update({k: data[k] for k in ("target", "source", "intent") if data.get(k)})
                route = router.Route("media", args, 1.0)
                if name and names.count(name) == 1:
                    options[name] = route
                # A label cannot displace an exact (or ambiguous) player name.
                if label and labels.count(label) == 1 and label not in names:
                    options[label] = route
        else:
            return None
        question = textwrap.shorten(question, SENTENCE_MAX, placeholder="...")
        if self._set_awaiting(conn, Awaiting(
                asyncio.get_running_loop().create_future(), True, uuid.uuid4().hex,
                question, time.monotonic() + self.AWAIT_S, owner=owner, kind="choice", source="media",
                options=options, original=(owner.route_text or owner.text) if owner else "")):
            return question
        return None

    async def to_claude(self, text: str, conn, tid: str | None = None, followup: bool = False,
                        parent: str | None = None, tool_output: bool = False, acts: list | None = None) -> None:
        turn = Turn(conn, text, tid=tid, followup=followup, route_text=text, parent=parent,
                    tool_output=tool_output, acts=acts or [])
        trace.emit("brain", "route", tid, parent=parent, route="claude", text=text,
                   model="haiku", followup=followup)
        self._log("claude", None, "haiku", turn.t0, text)
        if self.claude_off or not self.claude_cmd:
            trace.emit("brain", "reply", tid, text=OFF, asked=False, why=f"claude off ({self.claude_off})")
            await self.say(conn, OFF, final=True)
            return await self.done(conn)
        if not self.rate.claude_allowed(time.time()):   # fast path unaffected: it never gets here
            trace.emit("brain", "reply", tid, text=self.rate.limit(), asked=False, why="rate limit")
            await self.say(conn, self.rate.limit(), final=True)
            return await self.done(conn)
        turn.text = self.prefix(text)
        self.turns.append(turn)
        self._bg(self._kick())   # the gap wait must not hold up this connection's next (fast-path) utterance

    def prefix(self, text: str) -> str:
        """Fast-path actions Claude hasn't seen, so "close the other one" works after one (spec §2.4)."""
        if not self.unshown:
            return text
        new, self.unshown = list(self.recent)[-self.unshown:], 0
        acts = [" ".join([time.strftime("%H:%M", time.localtime(t)), verb.replace(".", " "),
                          *map(str, [] if self.router.cat.get(verb, {}).get("personal") else args.values()),
                          "→", "ok" if ok else "failed"]) for t, verb, args, ok in new]
        return f"[recent actions: {'; '.join(acts)}]\n{text}"

    def _logline(self, text: str) -> None:
        try:
            with open(state_dir() / "brain.log", "a") as f:
                f.write(f"{datetime.now().astimezone().isoformat(timespec='seconds')} {text}\n")
        except OSError:
            log.warning("brain: cannot write brain.log")

    def _log(self, route: str, verb: str | None, model: str | None, t0: float, text: str) -> None:
        ms = round((time.monotonic() - t0) * 1000)
        self._logline(f"route={route} verb={verb or '-'} model={model or '-'} ms={ms} text={text!r}")


async def serve(path: Path | None = None, *, voice_token: str | None = None) -> None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("heardbackd: ANTHROPIC_API_KEY is set; refusing to start (subscription login only)", file=sys.stderr)
        raise SystemExit(1)
    from heardback.status.announce import Announcer, run   # only the daemon runs the announcer

    ann = Announcer()
    b = Brain(claude_cmd=["claude"], announcer=ann, voice_token=voice_token)
    from heardback.dash import sources
    sources.LIVE["brain"] = b   # the dashboard reads its live state (read-only)
    task = asyncio.create_task(run(ann, b.announce))
    try:
        await b.serve(path or sock_path())
    finally:
        task.cancel()
