"""Local clock/date actions and a refusal for the removed screen-lock command."""
from datetime import datetime

from jv.verb import JvError, Verb


def _now() -> datetime:
    """Return one timezone-aware timestamp in the machine's configured local timezone."""
    return datetime.now().astimezone()


def _time(_: dict) -> dict:
    now = _now()
    clock = now.strftime("%I:%M %p").lstrip("0")
    return {"say": f"It's {clock}."}   # no zone: TTS read "IST" as "ist" (live 20:37); one-zone machine


def _date(_: dict) -> dict:
    now = _now()
    return {"say": f"Today is {now:%A, %B} {now.day}, {now.year}."}


def _lock(_: dict) -> dict:
    """Keep old voice/CLI requests on a deterministic refusal; never launch a locker."""
    raise JvError(1, "screen locking has been removed from heardback",
                  say="Screen locking is disabled in heardback. Please use your desktop lock shortcut.")


EMPTY = {"type": "object", "properties": {}}
VERBS = [
    Verb("local.time", _time, "Read the current local time from the system clock.", EMPTY, [],
         phrases=["what's the time right now", "what time is it", "what time is it right now", "what's the time"]),
    Verb("local.date", _date, "Read today's date in the machine's local timezone.", EMPTY, [],
         phrases=["what's today's date", "what is today's date", "what's the date today", "what date is it today"]),
    Verb("local.lock", _lock, "Screen locking is disabled. Explain that the user must use their desktop lock shortcut.", EMPTY, [],
         phrases=["lock the screen", "lock screen"]),
]
