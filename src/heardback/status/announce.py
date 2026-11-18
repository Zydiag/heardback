"""Batch session and watch changes into events for the brain's speech queue."""
import asyncio
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Awaitable, Callable

from heardback.status import speak, store, watch
from heardback.status.who import name, window

FINISHED_MIN_S = 30
DEBOUNCE_S = 60
BATCH_S = 5
REMIND_S = 600
BACK_WINDOW_S = 600
POLL_S = 0.5
UNSTICK_S = 5.0
GC_EVERY_S = 3600
NUMBERS = {2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six", 7: "Seven", 8: "Eight", 9: "Nine"}


def kind(ev: dict) -> str | None:
    if ev.get("to") == "waiting":
        return "waiting"
    if ev.get("hook") == "StopFailure":
        return "error"
    if ev.get("hook") == "Stop" and ev.get("from") in ("working", "waiting") and (
            ev.get("from") == "waiting" or (ev.get("turn_s") or 0) >= FINISHED_MIN_S):
        return "finished"
    return None


def pane_question(s: dict) -> tuple[str, dict] | None:
    """(spoken 'run x? 1 yes, 2 …', answer target) for a session's open permission dialog; never a trust screen."""
    if not s.get("tmux_pane"):
        return None
    from jv import tmux
    try:
        d = tmux.dialog(s["tmux_pane"])
    except Exception:   # tmux gone, pane closed: the plain alert still goes out
        return None
    if not d or d["kind"] != "permission" or not d["options"]:
        return None
    no = next((n for n, t in d["options"] if t.lower().startswith("no")), None)
    return tmux.ask_line(d), {"pane": s["tmux_pane"], "no": str(no) if no else "esc",
                              "choices": [str(n) for n, _ in d["options"]]}


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


class Announcer:
    def __init__(self, *, now=time.time, sessions=store.list_sessions, question=pane_question, cues=None):
        self.now, self.sessions = now, sessions
        self.question = question
        self.cues = cues
        self.ask: dict | None = None   # the last due() line's answer target: voice listens, brain routes the reply
        self.pending: dict[str, tuple[str, dict, float]] = {}
        self.pending_path = store.state_dir() / "pending-announcements.json"
        try:
            saved = json.loads(self.pending_path.read_text())
            if isinstance(saved, dict):
                self.pending = {sid: (row[0], row[1], row[2]) for sid, row in saved.items()
                                if isinstance(sid, str) and isinstance(row, list) and len(row) == 3
                                and row[0] in ("waiting", "finished", "error") and isinstance(row[1], dict)
                                and isinstance(row[2], (int, float)) and 0 <= self.now() - row[2] <= 600
                                and row[1].get("session_id") == sid}
        except (OSError, ValueError, TypeError):
            pass
        self.back_path = store.state_dir() / "back-said.json"
        self.back = self._restart_sessions()
        for s in self.back:   # the one "Back." line covers them; no second announcement from the saved queue
            self.pending.pop(s["session_id"], None)
        self.last_said: dict[str, float] = {}
        self.remind: dict[str, float] = {}
        self.watched: list[str] = []
        self.watch_finished: set[str] = set()
        self.status_lines: list[str] = []
        self.free_requests: list[dict] = []
        self.next_unstick = 0.0

    def _restart_sessions(self) -> list[dict]:
        """Live sessions waiting, or finished within BACK_WINDOW_S, that no earlier start already mentioned."""
        try:
            said = json.loads(self.back_path.read_text())
        except (OSError, ValueError):
            said = {}
        now = self.now()
        out = [s for s in store.user_sessions(self.sessions())
               if store.alive(s.get("claude_pid")) and said.get(s["session_id"]) != s.get("updated_at")
               and (s.get("state") == "waiting" or (s.get("state") == "idle" and s.get("last_hook") == "Stop"
                    and 0 <= now - store.epoch(s.get("updated_at")) <= BACK_WINDOW_S))]
        if out:
            self.back_path.write_text(json.dumps({**(said if isinstance(said, dict) else {}),
                                                  **{s["session_id"]: s.get("updated_at") for s in out}}))
        return out

    def _cues(self):
        if self.cues is None:
            from heardback.voice.cues import Cues
            self.cues = Cues(True)
        return self.cues

    def feed(self, ev: dict) -> None:
        sid = ev.get("session_id")
        if not isinstance(sid, str):
            return
        stopped = ev.get('hook') in ('Stop', 'StopFailure') or ev.get('to') == 'ended'
        if not stopped:
            self.watch_finished.discard(sid)
        if stopped and watch.pending():
            sessions = store.user_sessions(self.sessions(), include_ended=True)
            s = next((s for s in sessions if s['session_id'] == sid), None)
            taken = watch.take(s.get('tmux_pane'), ev.get('t'), now=self.now()) if s else None
            if taken:
                self.watch_finished.add(sid)
                self.pending.pop(sid, None)
                self.remind.pop(sid, None)
                if taken['kind'] != 'done' and not taken.get('session_id'):
                    return  # pre-Task-5 records never authorized a status submit or reply
                n = f"window {window(s)}" if window(s) else name(s, sessions)
                if taken.get('session_id') and taken['session_id'] != sid:
                    self.status_lines.append(f"{speak._cap(n)}'s Claude session changed; I didn't ask.")
                    return
                if taken['kind'] == 'done':
                    if ev.get('to') == 'ended':
                        line = f"{speak._cap(n)} ended before it finished."
                    else:
                        k = 'error' if ev.get('hook') == 'StopFailure' else 'finished'
                        line = speak.announce(k, {**s, 'project': n}, [], ev.get('detail'))
                        if k == 'finished':
                            line = line.replace(' finished', ' is done', 1)
                    self.watched.append(line)
                elif taken['kind'] == 'status':
                    if ev.get('to') == 'ended':
                        self.status_lines.append(f"{speak._cap(n)} ended before it finished.")
                    else:
                        from heardback.status import transcript
                        from heardback.status.hook import first_sentence
                        turns = transcript.last_turns(s.get('transcript_path'), 1)
                        reply = next((t['text'] for t in reversed(turns) if t['role'] == 'assistant'), '')
                        if not reply or first_sentence(reply) != ev.get('detail'):
                            reply = ev.get('detail') or ''
                        text = watch.speakable(reply)
                        self.status_lines.append(f"{speak._cap(n)} says: {text}" if text else
                            f"{speak._cap(n)} answered with a table or code I can't read out; it's on screen there.")
                elif taken['kind'] == 'when_free' and ev.get('to') != 'ended':
                    self.free_requests.append(taken)
                return
            if s and s.get('tmux_pane') and watch.pending(s['tmux_pane']):
                return  # an older Stop cannot announce or consume this pane's newer job
        if stopped and sid in self.watch_finished:
            return
        k = kind(ev)
        self.remind.pop(sid, None)  # any newer state change ends the waiting episode
        if k:
            self.pending[sid] = (k, ev, self.now())
        else:
            self.pending.pop(sid, None)
        self._save_pending()

    def _save_pending(self) -> None:
        path = self.pending_path
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with open(tmp, "w") as f:
                json.dump({sid: list(row) for sid, row in self.pending.items()}, f)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    async def unstick(self) -> None:
        """An interrupted turn fires no Stop, so a when_free ask would wait out its deadline (live 10:27, window 2)."""
        if self.now() < self.next_unstick or not watch.pending():
            return
        self.next_unstick = self.now() + UNSTICK_S
        from jv import tmux
        for s in store.user_sessions(self.sessions()):
            pane = s.get('tmux_pane')
            if s.get('state') != 'working' or not pane or (watch.peek(pane) or {}).get('kind') != 'when_free':
                continue
            if not await asyncio.to_thread(tmux.busy, s):
                taken = watch.take(pane, store.now_iso(self.now()), now=self.now())
                if taken:
                    self.free_requests.append(taken)

    async def ask_free(self) -> None:
        """Dispatch consumed offers through the allowlist, keeping results out of Claude."""
        from heardback.brain import catalogue
        from jv import tmux
        requests, self.free_requests = self.free_requests, []
        for record in requests:
            sessions = store.user_sessions(self.sessions(), include_ended=True)
            s = next((s for s in sessions if s.get('tmux_pane') == record['pane'] and s.get('state') != 'ended'), None)
            n = f"Window {window(s)}" if s and window(s) else 'That session'
            if not s or s['session_id'] != record.get('session_id'):
                self.status_lines.append(f"{n}'s Claude session changed; I didn't ask.")
                continue
            if s.get('state') != 'idle' and await asyncio.to_thread(tmux.busy, s):
                self.status_lines.append(f"{n} is busy again; I didn't ask.")
                continue
            rc, out, _ = await catalogue.run_jv(['jv', 'status', 'ask', record['pane'], '--yes', '--json',
                                                '--expect-session', record['session_id']],
                                               turn=record['turn'])
            try:
                result = json.loads(out)
            except ValueError:
                result = {}
            line = result.get('say') if isinstance(result, dict) else None
            self.status_lines.append(line or f"I couldn't ask {n.lower()} for its status.")

    def due(self) -> list[dict]:
        self.ask = None
        self._due_states = {}
        self._desk_lines = []
        for record in watch.expire(self.now()):
            self.watch_finished.add(record['session_id'])
            self.pending.pop(record['session_id'], None)
            self.remind.pop(record['session_id'], None)
            s = next((s for s in self.sessions() if s['session_id'] == record['session_id']), {})
            n = f"Window {window(s)}" if window(s) else 'That session'
            self.status_lines.append(f"{n} hasn't answered in 90 seconds; I've stopped waiting."
                                     if record['kind'] == 'status' else
                                     f"{n} is still busy after 30 minutes; I didn't ask it.")
        now = self.now()
        out = [{"kind": "done", "text": " ".join(self.watched)}] if self.watched else []
        self.watched = []
        if self.back:
            sessions = store.user_sessions(self.sessions(), include_ended=True)
            line, self.back = speak.back(self.back, sessions), []
            out.append({"kind": "back", "text": line})
        if self.status_lines:
            out.append({"kind": "answer", "text": " ".join(self.status_lines)})
        self.status_lines = []
        flush = (self.pending and now - min(t for _, _, t in self.pending.values()) >= BATCH_S
                 and any(now - self.last_said.get(sid, -DEBOUNCE_S) >= DEBOUNCE_S for sid in self.pending))
        if not flush and not any(t <= now for t in self.remind.values()):
            return out
        sessions = store.user_sessions(self.sessions(), include_ended=True)
        by_id = {s["session_id"]: s for s in sessions}
        lines = self._reminders(now, by_id, sessions)
        if flush:
            lines[:0] = self._flush(now, by_id, sessions)
        if lines:
            out.insert(0, {"kind": "session", "text": " ".join(lines), "ask": self.ask,
                           "states": self._due_states, "desk_text": " ".join(self._desk_lines)})
        return out

    def valid(self, event: dict) -> bool:
        by_id = {s["session_id"]: s for s in store.user_sessions(self.sessions())}
        for sid, state in event["states"].items():
            s = by_id.get(sid)
            if not s or s.get("state") != state:
                return False
            if state == "waiting" and not self._permission_open(s):
                return False
            if event.get("ask") and s.get("tmux_pane") == event["ask"]["pane"]:
                question = self.question(s)
                if not question or question[1] != event["ask"]:
                    return False
        return True

    def _flush(self, now: float, by_id: dict, sessions: list[dict]) -> list[str]:
        waiting, other = [], []
        for sid, (k, ev, _) in list(self.pending.items()):
            s = by_id.get(sid)
            if not s or s.get("state") != ev.get("to"):
                del self.pending[sid]
                self._save_pending()
            elif now - self.last_said.get(sid, -DEBOUNCE_S) >= DEBOUNCE_S:
                del self.pending[sid]
                self._save_pending()
                self.last_said[sid] = now
                if k == "waiting":
                    if self._permission_open(s):
                        self._due_states[sid] = s["state"]
                        self._desk_lines.append(speak.announce(k, s, sessions, None))
                        self.remind[sid] = now + REMIND_S
                        waiting.append(s)
                else:
                    self._due_states[sid] = s["state"]
                    if k == "finished":
                        self._desk_lines.append(speak.announce(k, s, sessions, None))
                    other.append(speak.announce(k, s, sessions, ev.get("detail")))
        if len(waiting) == 1:
            other.insert(0, self._waiting_line(waiting[0], sessions))
        elif waiting:
            n = len(waiting)
            other.insert(0, f"{NUMBERS.get(n, n)} sessions need you: {_and([name(s, sessions) for s in waiting])}.")
        return other

    def _reminders(self, now: float, by_id: dict, sessions: list[dict]) -> list[str]:
        out = []
        for sid, t in list(self.remind.items()):
            if t > now:
                continue
            del self.remind[sid]  # one reminder per episode: a new waiting event re-arms it
            s = by_id.get(sid)
            if s and s.get("state") == "waiting" and self._permission_open(s):
                line = self._waiting_line(s, sessions)
                if line:
                    self._due_states[sid] = s["state"]
                    out.append("Reminder: " + line)
        return out

    def _permission_open(self, s: dict) -> bool:
        return not (s.get("tmux_pane") and "permission" in speak.need_phrase(s.get("waiting_for"))
                    and self.question(s) is None)

    def _waiting_line(self, s: dict, sessions: list[dict]) -> str:
        """'heardback asks: run x? 1 yes, 2 no.' + arms self.ask; else the plain 'needs your permission'."""
        q = self.question(s)
        if not q and s.get("tmux_pane") and "permission" in speak.need_phrase(s.get("waiting_for")):
            return None
        if not q:
            return speak.announce("waiting", s, sessions, None)
        self.ask = q[1]
        return f"{speak._cap(name(s, sessions))} asks: {q[0]}."


class Tail:
    """New complete JSON lines of a file; starts at the end, reopens from 0 when the file is rotated."""

    def __init__(self, path: Path):
        self.path, self.buf = Path(path), b""
        try:
            st = self.path.stat()
            self.ino, self.offset = st.st_ino, st.st_size
        except OSError:
            self.ino, self.offset = None, 0

    def read_new(self) -> list[dict]:
        try:
            with open(self.path, "rb") as f:
                st = os.fstat(f.fileno())
                if st.st_ino != self.ino or st.st_size < self.offset:
                    self.ino, self.offset, self.buf = st.st_ino, 0, b""
                f.seek(self.offset)
                data = f.read()
        except OSError:
            return []
        self.offset += len(data)
        *lines, self.buf = (self.buf + data).split(b"\n")
        out = []
        for line in lines:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict):
                out.append(ev)
        return out


async def run(ann: Announcer, say: Callable[[dict], Awaitable[None]], tail: Tail | None = None) -> None:
    tail = tail or Tail(store.state_dir() / "events.jsonl")
    next_gc = 0.0
    while True:
        try:
            evs = tail.read_new()
            if time.time() >= next_gc:  # right after a read, so a rotation loses as few lines as possible
                store.gc()
                from jv.attach import prune
                from jv.verb import JvError
                try:
                    prune()
                except (OSError, JvError):
                    traceback.print_exc(file=sys.stderr)
                next_gc = time.time() + GC_EVERY_S
                evs += tail.read_new()
            for ev in evs:
                ann.feed(ev)
            await ann.unstick()
            await ann.ask_free()
            for event in ann.due():
                await say(event)
        except Exception:  # one bad read must not silence announcements for the rest of the day
            traceback.print_exc(file=sys.stderr)
        await asyncio.sleep(POLL_S)


async def _print(event: dict) -> None:
    print(event["text"], flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(run(Announcer(), _print))
    except KeyboardInterrupt:
        pass
