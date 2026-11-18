"""Verb definition, fuzzy matching and argument validation for jv."""
import difflib
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

TEXT_MAX = 4000
MIN_SCORE = 0.5
TIE_GAP = 0.1
READBACK_MAX = 200   # = brain SENTENCE_MAX: a yes must cover every word
UNCONFIRMED = "I couldn't confirm that."


def observation_result(data: dict, observes: bool) -> dict:
    """One receipt contract for CLI output and both brain paths; legacy verbs opt out."""
    if observes and data.get("ok") is True and not data.get("dry_run") and data.get("observed") is not True:
        return {**data, "say": UNCONFIRMED}
    return data


def refuse_wake(text: str, kind: str) -> None:
    compact = "".join(c for c in unicodedata.normalize("NFKD", text.casefold())
                      if not unicodedata.combining(c) and c.isalnum())
    if re.search(r"h[ae]y(cl|kl)(au|aw|ou|o)e?d|h[ae]yjarvis", compact):
        say = f"I can't read a wake phrase in a {kind}."
        raise JvError(4, say, say=say)


def _readback(sentence: str) -> str:
    sentence = re.sub(r"[.!?]+\?$", "?", sentence)   # quoted text often ends in its own stop
    if len(sentence) > READBACK_MAX:
        message = f"heardback didn't send it: too long to read back ({len(sentence)} characters, the most is {READBACK_MAX})"
        raise JvError(4, message, say=message)
    return sentence


def runtime_path(name: str) -> str:
    return os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}", name)


class JvError(Exception):
    def __init__(self, code: int, msg: str, **extra):
        super().__init__(msg)
        self.code, self.msg, self.extra = code, msg, extra


# Effect class per verb (review §4 step 2). Only `read` skips preflight for Claude; in a tainted conversation
# (Claude has read outside text) a no-confirm verb outside read/reversible is refused. Unlisted = "unknown".
_READ = """bt.status calculate catchup ctx do.check hypr.list local.date local.lock local.time reminder.list
    settings.outputs settings.power_get status.detail status.get status.list status.summary status.waiting
    system.battery system.disk system.gpu system.memory system.online system.tailscale system.temperature
    system.updates tmux.list tmux.prompt tmux.read wall.current zen.list zen.ping zen.read"""
_REVERSIBLE = """brightness bt.connect bt.disconnect bt.power hypr.focus hypr.launch hypr.move hypr.open hypr.workspace
    settings.dnd settings.mic settings.night settings.output settings.power settings.theme settings.window shot
    status.mute status.unmute status.watch tmux.focus tmux.move tmux.rename tmux.select volume wall.next
    wall.random wall.set wall.show zen.pick zen.switch zen.video"""
EFFECTS = {**dict.fromkeys(_READ.split(), "read"), **dict.fromkeys(_REVERSIBLE.split(), "reversible"),
           **dict.fromkeys("zen.search zen.play zen.ask".split(), "outbound"),
           **dict.fromkeys("do.draft tmux.clear".split(), "destructive")}


@dataclass
class Verb:
    name: str
    fn: Callable[[dict], dict]
    description: str
    params: dict
    argv: list[str]
    confirm: bool | str = False
    check: Callable[[dict], str | None] | None = None
    args: dict = field(default_factory=dict)
    phrases: list[str] = field(default_factory=list)
    phrase_args: dict[str, dict] = field(default_factory=dict)   # phrase -> args it implies ("press enter")
    examples: list[str] = field(default_factory=list)
    voice: bool = True
    personal: bool = False
    claude: bool = True
    observes: bool = False

    def __post_init__(self):
        # A dotless name is a top-level command: domain = verb = name.
        self.domain, _, verb = self.name.partition(".")
        self.verb = verb or self.domain
        if self.confirm in (True, "auto") and self.check is None:
            raise ValueError(f"{self.name}: confirm={self.confirm!r} needs a check")


def fuzzy(query: str, choices: list[str], min_score: float = MIN_SCORE) -> str:
    q = query.strip().lower()
    low = {c.lower(): c for c in choices}
    if q in low:
        return low[q]
    words = [c for c in choices if re.search(rf"(?<!\w){re.escape(q)}(?!\w)", c.lower())]
    if len(words) == 1:   # "obs" -> "OBS Studio", not a tie with "Obsidian"
        return words[0]
    pool = [c for c in choices if q in c.lower()] or list(choices)
    scored = sorted(((difflib.SequenceMatcher(None, q, c.lower()).ratio(), c) for c in pool), reverse=True)
    if not scored or (len(pool) == len(choices) and scored[0][0] < min_score):
        raise JvError(1, f"no match for {query!r}", closest=[c for _, c in scored[:3]])
    ties = [c for s, c in scored if scored[0][0] - s < TIE_GAP]
    if len(ties) > 1:
        raise JvError(2, f"which one: {', '.join(ties)}?", candidates=ties)
    return scored[0][1]


def validate(verb: Verb, args: dict) -> dict:
    props = verb.params.get("properties", {})
    unknown = set(args) - set(props)
    if unknown:
        raise JvError(2, f"unknown argument: {', '.join(sorted(unknown))}")
    missing = [k for k in verb.params.get("required", []) if args.get(k) is None]
    if missing:
        raise JvError(2, f"missing argument: {', '.join(missing)}")
    out = {}
    for k, v in args.items():
        p = props[k]
        t = p.get("type")
        if t == "integer":
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise JvError(2, f"{k} must be a whole number")
            if "minimum" in p and v < p["minimum"] or "maximum" in p and v > p["maximum"]:
                raise JvError(2, f"{k} out of range")
        elif t == "string":
            v = str(v)
            if len(v) > p.get("maxLength", TEXT_MAX):
                raise JvError(2, f"{k} too long")
            if "pattern" in p and not re.search(p["pattern"], v):
                raise JvError(2, f"{k} is not valid")
        if "enum" in p and v not in p["enum"]:
            raise JvError(2, f"{k} must be one of: {', '.join(map(str, p['enum']))}")
        out[k] = v
    return out
