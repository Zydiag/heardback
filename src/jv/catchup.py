"""Read-only local summary of recent heardback and Claude state."""
from datetime import datetime, timezone
import re
from textwrap import shorten
import time

from heardback.brain import usage
from heardback.status import speak, store
from heardback.tasks import personal
from jv.reminder import _time
from jv.verb import Verb


def _name(text):
    # Reminder punctuation must not add sentences to the summary.
    return shorten(re.sub(r"[.!?]+", ",", text), width=40, placeholder="…")


def _reminders():
    now = int(time.time())
    summary = personal.catchup_reminders(now=now)
    attention, recent = [], []
    if summary["overdue"]:
        n = summary["overdue"]
        attention.append(f"{n} overdue or held reminder{'s' * (n != 1)}")
    if summary["unknown_count"]:
        names = ", ".join(_name(r["text"]) for r in summary["unknown"])
        attention.append(f"{summary['unknown_count']} reminder{'s' * (summary['unknown_count'] != 1)} may not have played: {names}")
    if summary["spoken_count"]:
        names = ", ".join(_name(r["text"]) for r in summary["spoken"])
        recent.append(f"{summary['spoken_count']} spoken recently: {names}")
    if row := summary["next"]:
        recent.append(f"Next reminder: {_name(row['text'])}, {_time(row['due_at'])}")
    return " ".join("; ".join(parts) + "." for parts in (attention, recent) if parts)


def _notes():
    cutoff = datetime.fromtimestamp(time.time() - 24 * 3600, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    with personal._open() as conn:
        if conn is None:
            return ""
        n = conn.execute("SELECT count(*) FROM inbox WHERE created_at>=?", (cutoff,)).fetchone()[0]
    return f"{n} new note{'s' * (n != 1)}." if n else ""


def _session_overview():
    return speak.overview(store.user_sessions(store.list_sessions(create_state_dir=False)), time.time())


def _sessions():
    text = _session_overview()
    return "" if text == "No live local Claude Code sessions running." else text.split(".", 1)[0] + "."


def _claude():
    state = usage._read(create=False)
    now = time.time()
    if state.get("off_until", 0) > now:
        return f"Claude is unavailable until {usage.spoken(state['off_until'])}."
    if state.get("haiku_until", 0) > now:
        return f"Claude is using Haiku until {usage.spoken(state['haiku_until'])}."
    return ""


def _catchup(_args):
    parts, failed = [], []
    for name, section in (("reminders", _reminders), ("notes", _notes), ("sessions", _sessions),
                          ("Claude", _claude)):
        try:
            if text := section():
                parts.append(text)
        except Exception:
            failed.append(name)
    if failed:
        names = ", ".join(failed[:-1]) + " and " + failed[-1] if len(failed) > 1 else failed[0]
        parts.append(f"I couldn't check {names}.")
    return {"say": " ".join(parts) or "Nothing new."}


VERBS = [Verb("catchup", _catchup, "Read a fixed local summary of recent activity."
         " Speak only the tool's say.",
              {"type": "object", "properties": {}}, [], personal=True,
              phrases=["catch me up", "what did I miss"])]
