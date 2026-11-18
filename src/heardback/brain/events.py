"""The unsolicited-speech policy and its private, durable queue."""
import json
import os
import re
import tempfile

from heardback.status.store import state_dir


def speak_policy(event: dict, voice_state: dict, awaiting: bool) -> str:
    if voice_state["muted"] and event["kind"] != "answer":
        return "hold" if event["kind"] == "reminder" else "drop"
    if awaiting or voice_state["busy"] or not voice_state["connected"]:
        return "hold"
    return "now"


def _valid(row) -> bool:
    if not isinstance(row, dict) or not isinstance(row.get("kind"), str):
        return False
    if row["kind"] == "reminder":
        return isinstance(row.get("id"), str) and bool(re.fullmatch(r"[0-9a-f]{32}", row["id"]))
    if row["kind"] not in {"done", "answer", "session", "back"} or not isinstance(row.get("text"), str):
        return False
    states = row.get("states", {})
    if not isinstance(states, dict) or any(not isinstance(sid, str) or state not in ("idle", "waiting")
                                           for sid, state in states.items()):
        return False
    ask = row.get("ask")
    if ask is not None and (not isinstance(ask, dict) or not isinstance(ask.get("pane"), str)
            or not isinstance(ask.get("no"), str) or not isinstance(ask.get("choices"), list)
            or not all(isinstance(c, str) for c in ask["choices"])):
        return False
    return isinstance(row.get("desk_text", ""), str)


class EventQueue:
    def __init__(self):
        self.path = state_dir() / "brain-events.json"
        try:
            rows = json.loads(self.path.read_text())
            self.rows = [r for r in rows if _valid(r)] if isinstance(rows, list) else []
        except (OSError, ValueError):
            self.rows = []

    def save(self) -> None:
        fd, tmp = tempfile.mkstemp(prefix=".brain-events-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.rows, f)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def add(self, event: dict) -> None:
        if event["kind"] == "reminder" and event in self.rows:
            return
        self.rows.append(event)
        self.save()

    def remove(self, event: dict) -> None:
        self.rows.remove(event)
        self.save()
