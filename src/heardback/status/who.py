"""Resolve a spoken/typed <who> to one session, and name a session for speech."""
import re

from jv.verb import JvError, fuzzy
from jv.targets import normalize_window_target


class NotFound(Exception):
    pass


class Ambiguous(Exception):
    def __init__(self, matches: list[dict]):
        super().__init__(f"{len(matches)} sessions match")
        self.matches = matches


def window(s: dict) -> str | None:
    t = s.get("tmux_target")
    return t.partition(":")[2].partition(".")[0] if t else None


def _fuzzy(q: str, pool: list[dict]) -> list[dict]:
    try:
        names = [fuzzy(q, sorted({n for s in pool for n in (s.get("project"), s.get("tmux_window")) if n}))]
    except JvError as e:
        names = e.extra.get("candidates", []) if e.code == 2 else []
    return [s for s in pool if s.get("project") in names or s.get("tmux_window") in names]


def _direct_hits(q: str, pool: list[dict], *, allow_id_prefix: bool = True) -> list[dict]:
    rules = (
        lambda s: re.fullmatch(r"\d+", q) and window(s) == q,
        lambda s: re.fullmatch(r"[^:]+:\d+(\.\d+)?", q) and f"{s.get('tmux_target')}.".startswith(q + "."),
        lambda s: re.fullmatch(r"%\d+", q) and s.get("tmux_pane") == q,
        lambda s: (s.get("project") or "").casefold() == q.casefold(),
        lambda s: (s.get("tmux_window") or "").casefold() == q.casefold(),
        lambda s: allow_id_prefix and len(q) >= 4 and s["session_id"].startswith(q),
    )
    for rule in rules:
        hits = [s for s in pool if rule(s)]
        if hits:
            return hits
    return []


def _hits(q: str, pool: list[dict]) -> list[dict]:
    raw = q.strip()
    hits = _direct_hits(raw, pool)
    if hits:
        return hits
    normalized = normalize_window_target(raw)
    stable_target = re.fullmatch(r"\d+|%\d+|[^:\s]+:\d+(?:\.\d+)?", normalized)
    if stable_target:
        hits = _direct_hits(normalized, pool, allow_id_prefix=normalized == raw)
        if hits:
            return hits
        return []
    if normalized != raw:
        hits = _direct_hits(normalized, pool)
        if hits:
            return hits
        return _fuzzy(normalized, pool)
    return _fuzzy(raw, pool)


def resolve(who: str, sessions: list[dict]) -> dict:
    query = who.strip()
    for pool in ([s for s in sessions if s.get("state") != "ended"], sessions):
        hits = _hits(query, pool)
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise Ambiguous(hits)
    raise NotFound(normalize_window_target(query))


def _base(s: dict) -> str:
    p = s["project"]
    if s.get("tmux_pane") is None:
        return f"the {p} session outside tmux"
    return f"{p} in window {window(s)}" if window(s) else p


def name(s: dict, sessions: list[dict]) -> str:
    p = s.get("project") or (f"window {window(s)}" if window(s) else "a Claude session")
    dups = [o for o in sessions if o.get("project") == s.get("project") and o["session_id"] != s["session_id"]
            and o.get("state") != "ended"]
    if not dups or not s.get("project"):
        return p
    n = _base(s)
    return f"{n}, id {s['session_id'][:4]}" if any(_base(o) == n for o in dups) else n
