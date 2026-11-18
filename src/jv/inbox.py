"""Local capture verbs; reading notes stays on the local router."""
import os

from heardback.tasks import personal
from jv.reminder import _request_key, _store
from jv.verb import JvError, Verb, _readback, refuse_wake


def _add_check(a):
    text = _store(personal._clean_text, a["text"], "inbox text", 1000)
    if os.environ.get("HEARDBACK_TAINTED") == "1":
        refuse_wake(text, "note")
        return _readback(f"Capture this note: {text}?")
    return None


def _add(a):
    row = _store(personal.add_inbox, request_key=_request_key("inbox"), text=a["text"])
    return {"item": row, "observed": bool(row), "say": f"Noted: {row['text'][:120]}"}


def _notes():
    with personal._open() as conn:
        if conn is None:
            return 0, []
        count = conn.execute("SELECT count(*) FROM inbox").fetchone()[0]
        rows = [dict(r) for r in conn.execute("SELECT * FROM inbox ORDER BY created_at DESC, item_id LIMIT 3")]
        return count, rows


def _list(a):
    count, rows = _store(_notes)
    return {"say": f"You have {count} notes." + (" " + "; ".join(r["text"] for r in rows) if rows else "")}


def _remove(a):
    row = _store(personal.remove_inbox, a["id"])
    if row is None:
        raise JvError(1, "I found no note with that ID.", say="I found no note with that ID.")
    return {"observed": True, "say": "Removed."}


VERBS = [
    Verb("inbox.add", _add, "Capture a local note."
         " Keep the meaning, add nothing, one call per request. You may repair misheard words and drop filler. Speak only the tool's say. Notes can only be read by saying read my notes on the local router.",
         {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
         ["text"], confirm="auto", check=_add_check, personal=True,
         args={"text": {"type": "text"}}, phrases=["capture {text:text}", "take a note {text:text}", "note that {text:text}"], observes=True),
    Verb("inbox.list", _list, "Read the newest three local notes.",
         {"type": "object", "properties": {}}, [], personal=True, claude=False, phrases=["read my notes"]),
    Verb("inbox.remove", _remove, "Remove one local note by ID (typed only).",
         {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
         ["id"], voice=False, claude=False, personal=True, observes=True),
]
