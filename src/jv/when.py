"""Deterministic one-shot times; unsupported timing words never become reminder text."""
import calendar
import re
from datetime import datetime, timedelta, timezone

MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTHS.update({name[:3].lower(): i for i, name in enumerate(calendar.month_name) if name})
DAYS = {name.lower(): i for i, name in enumerate(calendar.day_name)}
DATE = (r"(?:this\s+evening|this\s+morning|tomorrow\s+morning|today|tomorrow|tonight|(?:next\s+)?(?:" + "|".join(DAYS)
        + r")|\d{1,2}(?:st|nd|rd|th)?\s+(?:" + "|".join(sorted(MONTHS, key=len, reverse=True))
        + r")(?:\s+\d{4})?)")
MERIDIEM = r"(?:\s*[ap]\.?\s*m\.?)?"
# Whisper writes small numbers as words.
NUMBERS = {word: n for n, word in enumerate(
    "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty".split(), 1)} | {
    "twenty five": 25, "thirty": 30, "thirty five": 35, "forty": 40, "forty five": 45,
    "fifty": 50, "sixty": 60, "ninety": 90}
NUMBER_WORD = "(?:" + "|".join(w.replace(" ", r"\s+") for w in sorted(NUMBERS, key=len, reverse=True)) + ")"
DURATION = rf"(?:(?:\d+|an?|{NUMBER_WORD})\s+(?:seconds?|minutes?|hours?|days?)|half\s+an?\s+hour)"
RELATIVE = rf"(?:in|for|after)\s+{DURATION}"
FRACTION = rf"(?:half\s+past|quarter\s+(?:past|to))\s+(?:\d+|{NUMBER_WORD})" + MERIDIEM + r"(?:\s+evening)?"
NUMBER = r"\d+(?:(?:[:.]\d+)+|\s+\d{2})?" + MERIDIEM
CLOCK = rf"(?:{RELATIVE}|noon|midnight|{FRACTION}|{NUMBER})"
TIME = re.compile(
    rf"(?<![\w:])(?:(?:on\s+)?{DATE}(?:\s+(?:at\s+)?{CLOCK})?"
    rf"|(?:at\s+)?{CLOCK}(?:\s+(?:on\s+)?{DATE})?)(?!\w)", re.I)
DATE_RE = re.compile(DATE, re.I)
RECURRING = re.compile(r"\b(?:every|daily|weekly)\b", re.I)
# These fragments qualify the time, rather than describe the reminder's action.
TIME_TAIL = re.compile(
    r"^[\s,;]*(?::|[./-]\s*\.?\s*\d|(?:and|or|to)\s+(?:\d|" + NUMBER_WORD + r"\b)|and\s+(?:an?|half)\b|\d+\b"
    r"|(?:seconds?|minutes?|hours?|days?|morning|afternoon|evening|night|am|pm|utc|gmt|ist|est|edt|pst|pdt)\b"
    r"|(?:this|that|in the)\s+(?:morning|afternoon|evening|night)\b"
    r"|[ap]\s*m\w+\b|[ap](?=\s|\.|$)|(?:after|before|around|by|until|later|earlier)\b)", re.I)
TIME_HEAD = re.compile(
    r"\b(?:(?:after|before)(?:\s+\w+)?|in|at|on|around|by|until|seconds?|minutes?|hours?|days?"
    r"|morning|afternoon|evening|night)[\s,;]*$", re.I)


class _Unsupported(ValueError):
    """Recognized timing context outside the supported grammar."""


def parse_when(text: str, now: datetime, whole: bool = False):
    """Return (status, due, remaining, say, assumption); status: ok, handoff, refuse."""
    def unsupported():
        return ("refuse", None, text, "Please give a clock time like 6:30 pm, or a duration like in 20 minutes.", "") if whole else (
            "handoff", None, text, "", "")

    if whole and re.fullmatch(DURATION, text.strip(" ,;.!?"), re.I):
        text = "in " + text   # Claude's `when` is often just the duration
    if RECURRING.search(text):
        return "refuse", None, text, "Recurring reminders are not supported.", ""
    matches = [m for m in TIME.finditer(text) if whole or not re.fullmatch(r"\d+(?:\s+\d{2})?", m[0])]
    if not matches:
        return unsupported()
    if len(matches) != 1:   # "remind me to X in 5 minutes and Y at 11": Claude splits it into one call per reminder
        return ("refuse", None, text, "I found more than one time; please say one.", "") if whole else ("handoff", None, text, "", "")
    match = matches[0]
    before, after = text[:match.start()], text[match.end():]
    if match[0].lower().startswith("for") and before.strip():
        return unsupported()   # "walk for 20 minutes" gives a length, not a time
    # Whole-string validation must precede the reminder-only filler cleanup.
    if whole and (before.strip() or after.strip(" ,;.!?")):
        return unsupported()
    if TIME_HEAD.search(before) or TIME_TAIL.search(after):
        return unsupported()
    remaining = " ".join((before + " " + after).split()).strip(" ,;.!?")
    if not whole:
        remaining = re.sub(r"^(?:to|that|about)\s+|\s+(?:to|that|about)$", "", remaining, flags=re.I)
        remaining = re.sub(r"^(?:to|that|about)$", "", remaining, flags=re.I)
    try:
        due, assumption = _time(" ".join(match[0].lower().split()), now)
        due = _valid_wall(due)
        if _instant(due) <= _instant(now):
            return "refuse", None, remaining, "That time is in the past.", assumption
        limit = now.date().replace(year=now.year + 1, day=28 if now.month == 2 and now.day == 29 else now.day)
        if due.date() > limit:
            return "refuse", None, remaining, "That time is more than a year away.", assumption
    except _Unsupported:
        return unsupported()
    except (ValueError, OverflowError) as exc:
        say = str(exc) if str(exc).startswith(("Please ", "That local time ", "I couldn't ")) else "I couldn't use that time."
        return "refuse", None, remaining, say, ""
    return "ok", due, remaining, "", assumption


def _instant(value):
    return value.astimezone(timezone.utc) if value.tzinfo else value


def _valid_wall(value):
    if value.tzinfo and _instant(value).astimezone(value.tzinfo).replace(tzinfo=None) != value.replace(tzinfo=None):
        raise ValueError("That local time does not exist; please say another time.")
    return value


def _date(raw, now):
    if raw in ("today", "tonight", "tomorrow", "this evening", "evening", "this morning", "tomorrow morning"):
        return now + timedelta(days=raw.startswith("tomorrow"))
    if raw.startswith("next ") or raw in DAYS:
        weekday = DAYS[raw.removeprefix("next ")]
        ahead = 7 - now.weekday() + weekday if raw.startswith("next ") else (weekday - now.weekday()) % 7 or 7
        return now + timedelta(days=ahead)
    match = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)? ([a-z]+)(?: (\d{4}))?", raw)
    day, month, year = int(match[1]), MONTHS[match[2]], int(match[3] or now.year)
    if not match[3] and (month, day) < (now.month, now.day):
        year += 1
    return datetime(year, month, day, tzinfo=now.tzinfo)


def _clock(raw):
    """Return hour, minute and whether an am/pm choice is still needed."""
    if raw in ("noon", "midnight"):
        return (12 if raw == "noon" else 0), 0, False
    fraction = re.fullmatch(rf"(half past|quarter past|quarter to) (\d+|{NUMBER_WORD})(?:\s*([ap])\.?\s*m\.?)?", raw)
    if fraction:
        hour, meridiem = NUMBERS.get(fraction[2]) or int(fraction[2]), fraction[3]
        if not 1 <= hour <= 12:
            raise ValueError("I couldn't use that hour.")
        hour = hour % 12 + (12 if meridiem == "p" else 0)
        minutes = hour * 60 + (30 if fraction[1] == "half past" else 15 if fraction[1] == "quarter past" else -15)
        hour, minute = divmod(minutes % (24 * 60), 60)
        return hour % 12 if not meridiem else hour, minute, not meridiem
    match = re.fullmatch(r"(\d{1,2})(?:[:. ]?(\d{2}))?(?:\s*([ap])\.?\s*m\.?)?", raw)   # "1140" is 11:40
    if not match:
        raise _Unsupported
    hour, minute, meridiem = int(match[1]), int(match[2] or 0), match[3]
    if minute > 59 or not 0 <= hour <= 23 or (meridiem and not 1 <= hour <= 12):
        raise ValueError("I couldn't use that time.")
    ambiguous = not meridiem and 1 <= hour <= 12 and not match[1].startswith("0")
    if meridiem:
        hour = hour % 12 + (12 if meridiem == "p" else 0)
    return hour % 12 if ambiguous else hour, minute, ambiguous


def _time(raw, now):
    date_match = DATE_RE.search(raw)
    date_text = date_match[0] if date_match else ""
    clock = (raw[:date_match.start()] + raw[date_match.end():]) if date_match else raw
    clock = re.sub(r"\b(?:at|on)\b", "", clock).strip()
    if not date_text and clock.startswith("half past ") and clock.endswith(" evening"):
        date_text, clock = "this evening", clock.removesuffix(" evening")
    if clock.startswith(("in ", "for ", "after ")):
        if date_text:
            raise _Unsupported
        clock = clock.split(" ", 1)[1]
        relative = re.fullmatch(r"(.+) (seconds?|minutes?|hours?|days?)", clock)
        if clock == "half an hour":
            delta, elapsed = timedelta(minutes=30), True
        elif relative:
            word = relative[1]
            if not (word in ("a", "an") or word in NUMBERS or word.isdigit()):
                raise _Unsupported   # "half a hour"
            amount = 1 if word in ("a", "an") else NUMBERS.get(word) or int(word)
            unit = relative[2]
            unit = "seconds" if unit.startswith("second") else "minutes" if unit.startswith("minute") else "hours" if unit.startswith("hour") else "days"
            delta = timedelta(**{unit: amount})
            elapsed = not unit.startswith("day")
        else:
            raise _Unsupported
        if elapsed and now.tzinfo:
            return (_instant(now) + delta).astimezone(now.tzinfo), ""
        return (now + delta).replace(fold=0), ""
    day = _date(date_text, now) if date_text else now
    if not clock:
        hour = 20 if date_text == "tonight" else 19 if date_text in ("this evening", "evening") else 9
        return day.replace(hour=hour, minute=0, second=0, microsecond=0, fold=0), "8 pm" if hour == 20 else "7 pm" if hour == 19 else "9 am"
    hour, minute, ambiguous = _clock(clock)
    if date_text:
        assumption = ""
        if ambiguous:
            if date_text in ("tonight", "this evening", "evening") and (re.fullmatch(r"\d{1,2}", clock) or clock.startswith("half past ")):
                hour, assumption = hour + 12, f"{hour or 12} pm"
            else:
                raise ValueError("Please say am or pm.")
        if clock == "midnight":
            day += timedelta(days=1)
            assumption = f"midnight ending that night (00:00 on {day:%Y-%m-%d})"
        return day.replace(hour=hour, minute=minute, second=0, microsecond=0, fold=0), assumption
    if ambiguous and clock.startswith("half past "):
        raise ValueError("Please say am or pm.")
    if ambiguous:
        candidates = [now.replace(hour=hour + half, minute=minute, second=0, microsecond=0, fold=0) + timedelta(days=days)
                      for days in (0, 1) for half in (0, 12)]
        due = min((value for value in candidates
                   if value.replace(tzinfo=None) > now.replace(tzinfo=None) and _instant(value) > _instant(now)), key=_instant)
        return due, f"{due:%I:%M %p}".lstrip("0").lower().replace(":00 ", " ")
    due = now.replace(hour=hour, minute=minute, second=0, microsecond=0, fold=0)
    if due.replace(tzinfo=None) <= now.replace(tzinfo=None) or _instant(due) <= _instant(now):
        due += timedelta(days=1)
    return due, ""
