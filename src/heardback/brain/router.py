"""Fast-path router: utterance -> (verb, args) from `jv list` phrase templates, or None (= Claude)."""
import itertools
import logging
import re
from dataclasses import dataclass, field

from heardback.brain import catalogue
from jv.calculator import math_expression
from jv.tmux import ENTER_TAIL
from jv.zen import SITE_URLS, URL_RE, result_number

MAX_WORDS = 10
CHAIN = {"and", "then", "after", "also", "but"}
QUESTION = {"what", "whats", "why", "how", "which", "should", "is", "are", "do"}

FILLERS = [("please",), ("can", "you"), ("could", "you"), ("the",), ("a",), ("number",), ("for", "me")]
SYNONYMS = {("go", "to"): ("switch", "to"), ("show",): ("switch", "to"), ("jump", "to"): ("switch", "to"),
            ("firefox",): ("zen",), ("browser",): ("zen",), ("kill",): ("close",), ("shut",): ("close",),
            ("wallpaper",): ("wall",), ("background",): ("wall",), ("wallpapers",): ("walls",),
            ("backgrounds",): ("walls",)}
_CARDINALS = ("one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
              "sixteen seventeen eighteen nineteen twenty").split()
_ORDINALS = "first second third fourth fifth sixth seventh eighth ninth tenth".split()
_SUFFIX = ["st", "nd", "rd"] + ["th"] * 17   # Whisper may write "2nd"
NUMBERS = {**{w: str(i) for i, w in enumerate(_CARDINALS, 1)}, **{w: str(i) for i, w in enumerate(_ORDINALS, 1)},
           **{f"{i}{s}": str(i) for i, s in enumerate(_SUFFIX, 1)}}
WAKE = {"jarvis", "claude"}
HOMOPHONES = {"to": "2", "too": "2", "for": "4", "won": "1"}   # only ever read in numeric slot positions
SLOT = re.compile(r"\{(\w+):(\w+)\}")
GLUED_NAVIGATION = re.compile(r"(?i)^\s*switch\s*(?:to\s*|2\s+)(workspace|window)\s*(\d+)\s*[.!?]?\s*$")
# dictated text for a confirm verb that starts like a target ("type into window 3 hi") isn't "type {text}" (→ Claude)
TARGET_START = re.compile(r"(?:in|into|on|to) (?:\S+ ){0,2}(?:window|pane|session|terminal)\b")
# ...nor is text for the current pane that ends in one ("type hello in window 3"): the fast path can't parse a name
# target, so Claude takes it. A false hit only costs that trip; a miss types into the wrong pane.
TARGET_END = re.compile(r"\b(?:in|into|on|to) (?:\S+ ){0,2}(?:window|pane|session|terminal)(?: of window)?(?: \S+){0,2}$")
TRAILING_STOP = re.compile(r"(?<!\.)[.。]$")   # one full stop, never an ellipsis
GLUED = re.compile(r"-(on|in) (?=zen\b)", re.I)   # Whisper's "WhatsApp Web-on Zen" (friction2 #24)
OPTIONAL = re.compile(r"\(([^()]*)\)\?")

log = logging.getLogger(__name__)


def _clean(text: str, template: bool = False) -> str:
    """Templates keep `{}:` for their slot placeholders; utterances lose them like any punctuation."""
    text = re.sub(r"['’]", "", text.lower())
    return re.sub(r"[^a-z0-9{}:\-]+" if template else r"[^a-z0-9\-]+", " ", text)


def strip_wake(words: list[str]) -> list[str]:
    """Drop a leading `hey`, then leading wake words. Works on raw or normalised words."""
    i = 0
    if i < len(words) and _clean(words[i]).strip() == "hey":
        i += 1
    while i < len(words) and _clean(words[i]).strip() in WAKE:
        i += 1
    return words[i:]


def _replace(words: list[str], table: dict[tuple, tuple]) -> list[str]:
    """Replace phrases; a replacement that repeats the words just before it is dropped ("zen browser" → "zen")."""
    keys = sorted(table, key=len, reverse=True)
    out, i = [], 0
    while i < len(words):
        for k in keys:
            if tuple(words[i:i + len(k)]) == k:
                rep = table[k]
                if not rep or tuple(out[-len(rep):]) != rep:
                    out += rep
                i += len(k)
                break
        else:
            out.append(words[i])
            i += 1
    return out


def normalise(text: str, wake: bool = True, template: bool = False, synonyms: bool = True) -> str:
    """wake=False keeps leading wake words (enum choices like a tmux window named `claude`)."""
    if not template and (m := GLUED_NAVIGATION.fullmatch(text)):
        # Repair only a complete numeric navigation command; never edit dictated text or a chain.
        text = f"switch to {m[1].lower()} {m[2]}"
    words = _clean(text, template).split()
    words = _replace(strip_wake(words) if wake else words, {f: () for f in FILLERS})
    if synonyms:
        words = _replace(words, SYNONYMS)
    return " ".join(NUMBERS.get(w, w) for w in words)


@dataclass
class Route:
    verb: str
    args: dict
    score: float


@dataclass
class Template:
    verb: str
    text: str                 # normalised, slots as {name:type}
    regex: re.Pattern         # full match; for free-text templates, the part before the text slot
    slots: dict[str, str]     # name -> type, in order
    tokens: list[str]         # normalised words, slots as {name}
    has_text: bool
    is_question: bool
    values: dict[str, dict[str, str]] = field(default_factory=dict)  # enum slot: normalised choice -> original
    bounds: dict[str, tuple] = field(default_factory=dict)            # int slot: (minimum, maximum)
    fixed: dict = field(default_factory=dict)   # args the phrase implies (`phrase_args`): "press enter" → enter
    tail: str = ""             # normalised literal words after the text slot ("open {query:text} in zen")
    strict: bool = False       # free text of a confirm verb, or with a tail: gates see the whole utterance

    @property
    def literals(self) -> int:
        """Words that aren't slots: an exact-match tie goes to the template with more."""
        return sum(not tok.startswith("{") for tok in self.tokens)


def _variants(phrase: str) -> list[str]:
    parts = OPTIONAL.split(phrase)   # literal, group, literal, group, ...
    opts = [[p] if i % 2 == 0 else [p, ""] for i, p in enumerate(parts)]
    return ["".join(c) for c in itertools.product(*opts)]


def _normalise_template(variant: str) -> tuple[str, list[tuple[str, str]]]:
    slots = SLOT.findall(variant)
    i = itertools.count()
    marked = SLOT.sub(lambda m: f" {{s{next(i)}}} ", variant)
    text = normalise(marked, template=True)
    for n, (name, typ) in enumerate(slots):
        text = text.replace(f"{{s{n}}}", f"{{{name}:{typ}}}", 1)
    return text, slots


def _slot_regex(t: Template, name: str) -> str:
    typ = t.slots[name]
    if typ == "int":
        body = r"\d+|" + "|".join(HOMOPHONES)
    elif typ == "enum":
        alts = sorted(t.values[name], key=len, reverse=True)
        alts += [h for h, d in HOMOPHONES.items() if d in t.values[name].values()]
        body = "|".join(map(re.escape, alts)) or r"(?!)"
    else:   # str
        body = r"\S+(?: \S+)*?"
    return f"(?P<{name}>{body})"


def enum_values(choices: list[str]) -> dict[str, str]:
    """Normalised key -> choice. A key that only comes from SYNONYMS ("Firefox" -> "zen") goes to the choice
    whose own words contain it ("Zen Browser"), and never takes a key another choice has without synonyms."""
    lit = {c: normalise(c, wake=False, synonyms=False) for c in choices}
    syn = {c: normalise(c, wake=False) for c in choices}
    out = {syn[c]: c for c in reversed(choices) if syn[c] and syn[c] == lit[c]}
    for c in choices:
        k = syn[c]
        if k and k not in out:
            out[k] = next((d for d in choices if set(k.split()) <= set(lit[d].split())), c)
    return out


def _compile_one(name: str, entry: dict, variant: str, fixed: dict | None = None) -> Template | None:
    text, slots = _normalise_template(variant)
    props = entry.get("params", {}).get("properties", {})
    for i, (slot, typ) in enumerate(slots):
        if catalogue.slot_type(entry, slot) != typ or (typ == "text" and i != len(slots) - 1):
            log.warning("router: skipping %s phrase %r (slot {%s:%s} does not match args)", name, variant, slot, typ)
            return None
    words = text.split()
    tokens = [SLOT.sub(r"{\1}", w) for w in words]
    has_text = bool(slots) and slots[-1][1] == "text"
    t = Template(name, text, re.compile("(?!)"), dict(slots), tokens, has_text,
                 bool(words) and words[0] in QUESTION, fixed=dict(fixed or {}))
    if has_text:
        at = next(i for i, w in enumerate(words) if w == f"{{{slots[-1][0]}:text}}")
        words, t.tail = words[:at + 1], " ".join(words[at + 1:])
        t.strict = not entry.get("personal", False) and (entry.get("confirm", False) is not False or bool(t.tail))
    for slot, typ in slots:
        if typ == "enum":
            t.values[slot] = enum_values(list(catalogue.choices(entry, slot)))
            if not t.values[slot]:   # no choices (e.g. `add {name:enum}` with no drafts): nothing can match
                return None
        elif typ == "int":
            p = props.get(slot, {})
            t.bounds[slot] = (p.get("minimum"), p.get("maximum"))
    head = words[:-1] if has_text else words
    t.regex = re.compile(" ".join(_slot_regex(t, SLOT.fullmatch(w)[1]) if SLOT.fullmatch(w) else re.escape(w)
                                  for w in head))
    return t


def compile_catalogue(cat: dict) -> list[Template]:
    out = []
    for name, entry in cat.items():
        for phrase in entry.get("phrases", []):
            fixed = entry.get("phrase_args", {}).get(phrase)
            for variant in _variants(phrase):
                if (t := _compile_one(name, entry, variant, fixed)) is not None:
                    out.append(t)
    return out


def slot_value(t: Template, name: str, raw: str):
    """Typed value for a matched slot, or None if invalid."""
    typ = t.slots[name]
    if typ == "int":
        n = int(HOMOPHONES.get(raw, raw)) if raw.isdigit() or raw in HOMOPHONES else None
        lo, hi = t.bounds.get(name, (None, None))
        if n is None or (lo is not None and n < lo) or (hi is not None and n > hi):
            return None
        return n
    if typ == "enum":
        return t.values[name].get(raw) or t.values[name].get(HOMOPHONES.get(raw, ""))
    return raw


def _slots_of(t: Template, m: re.Match) -> dict | None:
    args = {}
    for name, raw in m.groupdict().items():
        if (v := slot_value(t, name, raw)) is None:
            return None
        args[name] = v
    return args


def _match_text(t: Template, raw: str) -> tuple[Route, str] | None:
    """(route, normalised text the gates apply to: the head before the text slot, all of it if strict) or None."""
    words = strip_wake(raw.split())
    text_slot = next(reversed(t.slots))
    ends = [j for j in range(len(words), 0, -1) if normalise(" ".join(words[j:])) == t.tail]
    for j in ends[:1]:   # the shortest tail that matches (the text keeps everything before it)
        for k in range(1, j):
            head = normalise(" ".join(words[:k]))
            if t.verb == "calculate" and head == "whats":
                head = "what is"
            if (m := t.regex.fullmatch(head)) and normalise(words[k], wake=False):
                text = TRAILING_STOP.sub("", " ".join(words[k:j]).strip())   # Whisper ends with "."
                if t.verb == "calculate" and not math_expression(text):
                    continue
                said = normalise(ENTER_TAIL.sub("", text), wake=False)
                if t.strict and TARGET_START.match(said) or t.fixed.get("target") == "current" and TARGET_END.search(said):
                    continue
                if (args := _slots_of(t, m)) is not None:
                    return (Route(t.verb, {**t.fixed, **args, text_slot: text}, 1.0),
                            normalise(raw) if t.strict else head)
    return None


def _exact(raw: str, templates: list[Template]) -> list[tuple[Template, Route, str]]:
    """Every exact match as (template, route, the normalised text the gates apply to)."""
    norm = normalise(raw)
    out = []
    for t in templates:
        if t.has_text:
            if hit := _match_text(t, raw):
                out.append((t, *hit))
        elif (m := t.regex.fullmatch(norm)) and (a := _slots_of(t, m)) is not None:
            out.append((t, Route(t.verb, {**t.fixed, **a}, 1.0), norm))
    return out


def match_exact(raw: str, templates: list[Template]) -> list[Route]:
    out = []
    for _, r, _ in _exact(raw, templates):
        if r not in out:
            out.append(r)
    return out


def _gated(t: Template, text: str, exact: bool = False) -> bool:
    """True if the gates rule template `t` out for this (normalised) text. An exact match may hold chain words the
    template spells out ("paste my clipboard into window 3 and press enter")."""
    words = text.split()
    if len(words) > MAX_WORDS or CHAIN.intersection(words) - (set(t.tokens) if exact else set()):
        return True
    return bool(words) and words[0] in QUESTION and t.tokens[:1] != words[:1]


class Router:
    def __init__(self, cat: dict):
        self.cat = cat
        self.templates = compile_catalogue(cat)
        # fuzzy only for slot-free-text templates of verbs that never need a confirm

    def route(self, raw: str) -> Route | None:
        personal = _exact(raw, [t for t in self.templates if self.cat[t.verb].get("personal", False)])
        if personal:
            literals = _exact(raw, [t for t in self.templates if not t.slots and not self.cat[t.verb].get("personal", False)])
            hits = literals or personal
            best = max(t.literals for t, _, _ in hits)
            top = [r for t, r, _ in hits if t.literals == best]
            return top[0] if len({r.verb for r in top}) == 1 else None
        raw = GLUED.sub(r" \1 ", raw)
        # Parse this navigation modifier before the wallpaper synonym rewrites background to wall.
        suffix = re.search(r"(?:\s+in (?:the )?background|[, ]+(?:and )?(?:don['’]?t switch|do not switch|without switching))[.!?]?\s*$", raw, re.I)
        if suffix:
            base = raw[:suffix.start()].strip(" ,")
            if CHAIN.intersection(normalise(base, synonyms=False).split()):
                return None
            route = self.route(base)
            if route is None and (m := re.fullmatch(r"(?:open|show|switch to)\s+(.+)", base, re.I)):
                query = m[1].strip()
                if query.casefold().rstrip(".") in SITE_URLS or re.fullmatch(URL_RE, query, re.I):
                    route = Route("zen.open", {"query": query}, 1.0) if "zen.open" in self.cat else None
            if route and route.verb in {"zen.open", "zen.switch", "zen.search"}:
                return Route(route.verb, {**route.args, "background": True}, route.score)
        if suffix or re.search(r"\bin (?:the )?background\b|\bdon['’]?t switch\b|\bwithout switching\b", raw, re.I):
            templates = [t for t in self.templates if t.verb == "zen.play" and t.fixed.get("background")]
            hits = [r for t, r, gate in _exact(raw, templates) if not _gated(t, gate, exact=True)]
            return hits[0] if hits else None
        if pick := self._pick(raw):
            return pick
        hits = [(t, r) for t, r, gate in _exact(raw, self.templates) if not _gated(t, gate, exact=True)]
        if hits:
            best = max(t.literals for t, _ in hits)
            top = [(t, r) for t, r in hits if t.literals == best]
            return top[0][1] if len({r.verb for _, r in top}) == 1 else None   # R2: equal tie -> Claude
        return None

    def _pick(self, raw: str) -> Route | None:
        """Play / pick / open + only an ordinal result ("play second result on youtube") = zen.pick, before any
        `play {query:text}` phrase can take "second result" as a title."""
        words = normalise(raw).split()
        if "zen.pick" not in self.cat or words[:1] not in (["play"], ["pick"], ["open"], ["choose"]):
            return None
        n = result_number(" ".join(words[1:]))
        p = self.cat["zen.pick"].get("params", {}).get("properties", {}).get("n", {})
        if n is None or not p.get("minimum", 1) <= n <= p.get("maximum", 20):
            return None
        return Route("zen.pick", {"n": n}, 1.0)
