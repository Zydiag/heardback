"""Spoken sentences about sessions: templates only, no Claude (fast path)."""
import re

from heardback.status.store import epoch
from heardback.status.who import name

OVERVIEW_MAX = 4
RANK = {"waiting": 0, "working": 1, "idle": 2}


def ago(seconds: float) -> str:
    if seconds < 60:
        return "under a minute"
    n, unit = (int(seconds // 60), "minute") if seconds < 3600 else (int(seconds // 3600), "hour")
    return f"{n} {unit}{'s' * (n != 1)}"


def need_phrase(waiting_for: str | None) -> str:
    # Hook Notification: "Claude needs your permission to use Bash" or just "...your permission";
    # registry waitingFor: "permission prompt".
    m = re.search(r"permission to use (\w+)", waiting_for or "")
    if m:
        return f"your permission for {m[1]}"
    return "your permission" if "permission" in (waiting_for or "").casefold() else "your input"


def _cap(t: str) -> str:
    return t[:1].upper() + t[1:]


def _end(t: str) -> str:
    t = t.strip()
    return t if t.endswith((".", "!", "?")) else t + "."


def _since(s: dict, now: float) -> str:
    return ago(now - epoch(s.get("turn_started_at") or s.get("updated_at")))


def _clause(s: dict, sessions: list[dict], now: float) -> str:
    n = _cap(name(s, sessions))
    if s["state"] == "waiting":
        return f"{n} is waiting for {'your permission' if 'permission' in need_phrase(s.get('waiting_for')) else 'you'}."
    if s["state"] == "working":
        return f"{n} is working, {_since(s, now)} in."
    return f"{n} finished: {_end(s['last_reply'])}" if s.get("last_reply") else f"{n} is idle."


def overview(sessions: list[dict], now: float) -> str:
    live = sorted((s for s in sessions if s.get("state") != "ended"),
                  key=lambda s: (RANK.get(s.get("state"), 2), -epoch(s.get("updated_at"))))
    if not live:
        return "No live local Claude Code sessions running."
    out = [f"{len(live)} live local Claude Code session{'s' * (len(live) != 1)}."]
    out.extend(_clause(s, sessions, now) for s in live[:OVERVIEW_MAX])
    rest = live[OVERVIEW_MAX:]
    if rest:
        out.append(f"and {len(rest)} more{' idle' * all(s.get('state') == 'idle' for s in rest)}.")
    return " ".join(out)


def one(s: dict, sessions: list[dict], now: float) -> str:
    """One wording for both "is X done?" and "what is X doing?": no Yes/No prefix (sysctl task 0)."""
    n, st = _cap(name(s, sessions)), s.get("state")
    if st == "ended":
        return f"{n} has ended."
    if st == "working":
        return _clause(s, sessions, now)
    if st == "waiting":
        return f"{n} is waiting for {need_phrase(s.get('waiting_for'))}."
    if not s.get("last_reply"):
        return f"{n} is idle."
    return f"{n} finished {ago(now - epoch(s.get('updated_at')))} ago: {_end(s['last_reply'])}"


def waiting(sessions: list[dict]) -> str:
    from heardback.status.announce import pane_question
    w = [s for s in sessions if s.get("state") == "waiting" and not (
        s.get("tmux_pane") and "permission" in need_phrase(s.get("waiting_for")) and not pane_question(s))]
    if not w:
        return "Nothing's waiting."
    return "Yes, " + " and ".join(f"{name(s, sessions)} needs {need_phrase(s.get('waiting_for'))}" for s in w) + "."


def announce(kind: str, s: dict, sessions: list[dict], detail: str | None) -> str:
    n = _cap(name(s, sessions))
    if kind == "waiting":
        return f"{n} needs {need_phrase(s.get('waiting_for'))}."
    if kind == "finished":
        return f"{n} finished: {_end(detail)}" if detail else f"{n} finished."
    return f"{n} {_end(detail or 'stopped with an error')}"


def back(mine: list[dict], sessions: list[dict]) -> str:
    """'Back. Window 3 needs your permission. Window 2 finished.' for sessions that were waiting or finished across a restart."""
    parts = [announce("waiting" if s["state"] == "waiting" else "finished", s, sessions, None) for s in mine]
    return "Back. " + " ".join(parts)
