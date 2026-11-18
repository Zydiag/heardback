"""jv-hook: Claude Code hook entry point. Updates one session file, logs real state changes."""
import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path
from collections.abc import Mapping

from heardback.status import store

REPLY_MAX = 120
PROMPT_MAX = 80
ERRLOG_MAX = 100_000
LOCK_WAIT_S = 1.0
WAITING_TYPES = ("permission_prompt", "elicitation_dialog", "agent_needs_input")


def first_sentence(text: str | None) -> str:
    """Plain first sentence of a markdown reply, at most REPLY_MAX chars (+ "…")."""
    s = re.sub(r"```.*?(```|$)", " ", text or "", flags=re.S)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"^\s*(#+|[-*+]|\d+\.|>)\s*", "", s, flags=re.M)
    s = re.sub(r"[*_~]{1,3}", "", s)
    s = " ".join(s.split())
    m = re.search(r"(.+?[.!?])(\s|$)", s)
    s = m.group(1) if m else s
    if len(s) > REPLY_MAX:
        s = s[:REPLY_MAX].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return s


def _base(ev: dict) -> dict:
    cwd = ev.get("cwd") or ""
    return {"session_id": ev["session_id"], "cwd": cwd, "project": os.path.basename(cwd.rstrip("/")),
            "transcript_path": ev.get("transcript_path"), "state": "idle"}


def transition(ev: dict, cur: dict | None, now: float) -> dict | None:
    name, state = ev.get("hook_event_name"), (cur or {}).get("state")
    new = dict(cur) if cur else _base(ev)
    if name == "SessionStart":  # also fires on compact/resume, possibly mid-turn: keep a busy state
        new.update(_base(ev), background=0, state=state if state in ("working", "waiting") else "idle")
    elif name == "UserPromptSubmit":
        new.update(state="working", last_prompt=(ev.get("prompt") or "")[:PROMPT_MAX],
                   turn_started_at=store.now_iso(now), waiting_for=None)
    elif name == "Notification" and ev.get("notification_type") in WAITING_TYPES:
        new.update(state="waiting", waiting_for=ev.get("message"))
    elif name == "Notification" and ev.get("notification_type") == "idle_prompt" and state == "working":
        new["state"] = "idle"
    elif name == "PostToolUse" and state == "waiting":
        new.update(state="working", waiting_for=None)
    elif name == "Stop":
        new.update(state="idle", last_reply=first_sentence(ev.get("last_assistant_message")),
                   background=len(ev.get("background_tasks") or []), waiting_for=None)
    elif name == "StopFailure":
        new.update(state="idle", last_reply="stopped with an error: " + str(ev.get("error") or "unknown").replace("_", " "),
                   waiting_for=None)
    elif name == "SessionEnd":
        new.update(state="ended", end_reason=ev.get("reason"))
    else:
        return None
    # This is evidence from the hook that actually ran. Tmux actions never synthesize hook events.
    new["last_hook"] = name
    if name == "SessionStart" and not new.get("started_at"):
        new["started_at"] = store.now_iso(now)
    new["updated_at"] = store.now_iso(now)
    return new


def find_claude_pid(start: int) -> int | None:
    pid = start
    for _ in range(5):
        try:
            if Path(f"/proc/{pid}/comm").read_text().strip() == "claude":
                return pid
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return None
        pid = int(stat[stat.rfind(")") + 2:].split()[1])
        if pid <= 1:
            return None
    return None


def _locked(fn) -> None:
    """Run fn under sessions.lock; after 1 s without the lock run it anyway (the Stop hook times out at 2 s)."""
    fd = os.open(store.state_dir() / "sessions.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        end = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= end:
                    break
                time.sleep(0.01)
        fn()
    finally:
        os.close(fd)  # closing drops the lock


def handle(ev: dict, now: float, env: Mapping[str, str]) -> None:
    if env.get("HEARDBACK_BRAIN"):
        return
    _locked(lambda: _handle(ev, now, env))


def _handle(ev: dict, now: float, env: Mapping[str, str]) -> None:
    cur = store.read_raw(ev["session_id"])
    if cur and store.epoch(cur.get("updated_at")) > now:
        return
    new = transition(ev, cur, now)
    if new is None:
        return
    pane = env.get("TMUX_PANE")
    pid = find_claude_pid(os.getppid())
    if (cur is None or (pane is not None and pane != new.get("tmux_pane"))
            or (pid is not None and pid != new.get("claude_pid"))):
        new.update(tmux_pane=pane, tmux_target=store.resolve_target(pane, env.get("JV_TMUX_SOCKET")),
                   claude_pid=pid if pid is not None else new.get("claude_pid"))
    store.write_session(new)
    old = (cur or {}).get("state")
    started = new.get("turn_started_at")
    if old != new["state"]:
        store.append_event({
            "t": new["updated_at"], "session_id": new["session_id"], "project": new.get("project"),
            "tmux_target": new.get("tmux_target"), "from": old, "to": new["state"],
            "detail": new.get("waiting_for") if new["state"] == "waiting" else new.get("last_reply"),
            "turn_s": int(now - store.epoch(started)) if old == "working" and started else None,
            "hook": ev.get("hook_event_name")})


def _log_error() -> None:
    import traceback
    try:
        p = store.state_dir() / "hook-errors.log"
        big = p.exists() and p.stat().st_size > ERRLOG_MAX
        with open(os.open(p, os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if big else os.O_APPEND), 0o600), "w") as f:
            f.write(f"{store.now_iso()} {traceback.format_exc()}\n")
    except BaseException:
        pass


def main() -> None:
    now = time.time()
    try:
        if os.environ.get("HEARDBACK_BRAIN"):
            return
        ev = json.loads(sys.stdin.read())
        sid = ev.get("session_id") if isinstance(ev, dict) else None
        if isinstance(sid, str) and sid not in ("", ".", "..") and "/" not in sid:
            handle(ev, now, os.environ)
    except BaseException:
        _log_error()
    finally:
        sys.exit(0)


if __name__ == "__main__":
    main()
