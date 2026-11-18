"""Daily Claude turn counter and persistent rate-limit guard (state_dir()/usage.json)."""
import json
import logging
import math
import os
import time
from datetime import date, datetime

from heardback.status.store import state_dir

WARN_UTIL = 0.80
STOP_UTIL = 0.95
DAY_TURN_WARN = 150
WARN = "Heads up, you've used 80 percent of your five-hour Claude limit."
log = logging.getLogger(__name__)


def _read(*, create: bool = True) -> dict:
    try:
        value = json.loads((state_dir(create=create) / "usage.json").read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(value: dict) -> None:
    path = state_dir() / "usage.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value))
    os.replace(tmp, path)


def bump(model: str, today: str | None = None) -> int:
    """+1 turn for `model` today (a new date starts from zero); returns the day's turns."""
    today = today or date.today().isoformat()
    u = _read()
    try:
        if u.get("date") != today:
            raise ValueError
        u["turns"] = int(u["turns"]) + 1
        u["by_model"][model] = int(u["by_model"].get(model, 0)) + 1
    except (OSError, ValueError, TypeError, AttributeError, KeyError):   # missing, old or mis-shaped: start over
        u = {"off_until": u.get("off_until", 0), "haiku_until": u.get("haiku_until", 0),
             "date": today, "turns": 1, "by_model": {model: 1}}
    _write(u)
    return u["turns"]


def today_turns() -> int:
    """Claude turns counted today; 0 when usage.json is missing, unreadable or from another day."""
    try:
        u = json.loads((state_dir() / "usage.json").read_text())
        return int(u["turns"]) if u.get("date") == date.today().isoformat() else 0
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return 0


def day_line(turns: int) -> str:
    return f"You've used {turns} Claude turns today."


def spoken(t: float) -> str:
    """Local time as said aloud: "6 pm", "6:30 pm"."""
    d = datetime.fromtimestamp(t)
    h, ampm = d.hour % 12 or 12, "am" if d.hour < 12 else "pm"
    return f"{h} {ampm}" if d.minute == 0 else f"{h}:{d.minute:02d} {ampm}"


def _epoch(v) -> float:
    return datetime.fromisoformat(v).timestamp() if isinstance(v, str) else float(v or 0)


class RateGuard:
    """Reads all stream-json rate windows; preserves the stop across process restarts."""

    def __init__(self):
        saved_state = _read()
        for key in ("off_until", "haiku_until"):
            setattr(self, key, 0.0)
            try:
                saved = float(saved_state.get(key, 0))
                if math.isfinite(saved) and saved > time.time():
                    datetime.fromtimestamp(saved)
                    setattr(self, key, saved)
            except (OSError, ValueError, TypeError, OverflowError):
                pass
        self._warned = set()   # (window name, resetsAt) already announced
        self.last_info, self.last_at = None, None   # the latest rate_limit_info + when (epoch), for the dashboard

    def update(self, info: dict) -> str | None:
        self.last_info, self.last_at = info, time.time()
        windows = info.get("unifiedWindows") or {}
        stops, haiku, warnings = [], [], []
        named = list(windows.items()) if isinstance(windows, dict) else []
        for name, w in [*named, (None, info)]:
            if not isinstance(w, dict):
                continue
            try:
                util = float(w.get("utilization") or 0)
                resets = _epoch(w.get("resetsAt", info.get("resetsAt")))
                datetime.fromtimestamp(resets)
            except (OSError, ValueError, TypeError, OverflowError):
                continue
            if not math.isfinite(resets) or not math.isfinite(util):
                continue
            rejected = w.get("status", "allowed") not in ("allowed", "allowed_warning")
            window = name or info.get("rateLimitType", "five_hour")
            if rejected or util >= STOP_UTIL:
                if (name is None and rejected) or window == "five_hour":
                    stops.append(resets)
                else:
                    haiku.append((resets, window))
            elif util >= WARN_UTIL:
                warnings.append((resets, name or info.get("rateLimitType")))
        old_haiku = self.haiku_until
        if stops:
            self.off_until = max(self.off_until, *stops)
        if haiku:
            self.haiku_until = max(self.haiku_until, *(reset for reset, _ in haiku))
        if stops or haiku:
            saved = _read()
            saved["off_until"] = self.off_until
            saved["haiku_until"] = self.haiku_until
            try:
                _write(saved)
            except OSError as e:
                log.warning("cannot persist Claude rate stop (%s)", e)
        if stops:
            return self.limit()
        if not self.claude_allowed(time.time()):
            return None
        if self.haiku_until > max(old_haiku, time.time()):
            reset, name = max(haiku)
            label = "Weekly Claude limit" if name == "seven_day" else "One of your Claude limits"
            return f"{label} nearly used. Using Haiku until {spoken(reset)}."
        for reset, name in warnings:
            if (name, reset) not in self._warned:
                self._warned.add((name, reset))
                label = "your weekly Claude limit" if name == "seven_day" else "one of your Claude limits"
                return WARN if name == "five_hour" else f"Heads up, you've used 80 percent of {label}."
        return None

    def claude_allowed(self, now: float) -> bool:
        return now >= self.off_until

    def limit(self) -> str:
        return f"Claude limit reached until {spoken(self.off_until)}."
