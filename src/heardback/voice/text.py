"""Transcript filters (spec §2.5), yes/no matching, sentence split, STT hotwords. Stdlib only (+ the brain's sentence ends)."""
import re
from typing import Iterable

from heardback.brain.speech import sentence_end

GHOSTS = frozenset(
    "you|thank you|thanks|thanks for watching|thank you for watching|thanks for watching everyone|"
    "bye|goodbye|bye bye|please subscribe|subscribe|the|uh|um|hmm|mm|mhm|ah|oh|so|and|i|a|it|he|we|"
    "yes yes yes".split("|"))
# friction2 #2, #20: credit lines Whisper invents on noise; one short phrase said ≥3 times ("watch it watch it watch it")
GHOST_START = re.compile(r"^(?:copyright|subtitles by|transcribed by|amara org)\b")
GHOST_REPEAT = re.compile(r"^(\S.{0,19}?)(?: \1){2,}$")
# yesfix (live 2026-11-01): "Yes, close it." and "Close it" were not yes, so a tmux.close read-back went nowhere 3 times.
# yes = a yes phrase, optionally + "please" + a short echo of the action, or the echo alone. A yes word followed by
# anything else ("yes but window 2") is None: destructive read-backs need a clear yes.
_YESW = r"(?:yes|yeah|yep|yup|sure|ok|okay|confirm|go ahead|do it)"
_ECHO = (r"(?:close|kill|run|send|do|press|open|delete|remove|start|play|type|enter|go|quit|exit|submit|answer)"
         r" (?:it|that|them|this|(?:the )?(?:window|tab|session|pane))")
_PL = r"(?:please|go ahead|do it)"
_YES = re.compile(rf"(?:{_PL} )*(?:{_YESW}(?: (?:{_YESW}|please))*(?: {_ECHO})?|{_ECHO})(?: {_PL})*")
# no = only no phrases ("no", "no leave it", "no don't", "don't close it"); "no, window 2 instead" is None (an alternative)
_NOW = (rf"(?:no|nope|cancel|stop|abort|no thanks|not now|never mind|nevermind|leave it|keep it|"
        rf"don't|dont|do not|don't do it|don't do that|(?:don't|dont|do not) {_ECHO})")
_NO = re.compile(rf"{_NOW}(?: (?:{_NOW}|please))*")
STATIC_HOTWORDS = ["tmux", "Hyprland", "Zen", "Claude", "wallpaper", "workspace",
                   "selected text", "clipboard", "copied", "selection", "paste", "lo-fi"]   # live 18:57: "Slector Test"
SITES = "chess.com YouTube WhatsApp Gmail GitHub Reddit Google Netflix Spotify Wikipedia Amazon LinkedIn".split()
HOTWORDS_MAX = 500   # chars; Whisper keeps only the last ~223 prompt tokens, so an overflow would drop the user's words

DEFAULT_STT_FIXES = {"cloud code": "Claude Code"}


def fix_mishear(text: str, configured: dict | None = None) -> str:
    fixes = {**DEFAULT_STT_FIXES, **(configured or {})}
    pairs = sorted(((" ".join(k.split()), v.strip()) for k, v in fixes.items()), key=lambda x: -len(x[0]))
    if not pairs:
        return text
    values = {key.casefold(): value for key, value in pairs}
    keys = (re.escape(k).replace(r"\ ", r"\s+") for k, _ in pairs)
    pattern = re.compile(r"(?<!\w)(?:" + "|".join(keys) + r")(?!\w)", re.I)
    return pattern.sub(lambda m: values[" ".join(m.group().split()).casefold()], text)


def norm(text: str) -> str:
    """Lowercase, drop punctuation (apostrophes kept: "don't"), collapse spaces."""
    return " ".join(re.sub(r"[^\w\s']", " ", text.lower()).split())


def clean(segments: Iterable) -> str:
    kept = [s.text for s in segments
            if not ((s.no_speech_prob > 0.6 and s.avg_logprob < -1.0) or s.compression_ratio > 2.4)]
    t = " ".join(re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", " ".join(kept)).split())
    n = norm(t)
    if sum(c.isalnum() for c in t) < 2 or n in GHOSTS or GHOST_START.match(n) or GHOST_REPEAT.match(n):
        return ""
    t = re.sub(r"^\s*hey,?\s+claude[\s,.!?]*", "", t, flags=re.I).strip()
    return t if sum(c.isalnum() for c in t) >= 2 else ""


def clean_dictation(segments: Iterable) -> str:
    """Normalize spacing only; dictation keeps lexical words, fillers, wake phrases and short utterances."""
    return " ".join(" ".join(s.text for s in segments if isinstance(getattr(s, "text", None), str)).split())


def match_confirm(text: str) -> bool | None:
    """True = yes, False = no, None = something else (an alternative request: the caller passes it on)."""
    n = norm(text)
    return True if _YES.fullmatch(n) else False if _NO.fullmatch(n) else None


def match_repeat(text: str) -> bool:
    return re.fullmatch(r"(?:please )?(?:repeat(?: that)?|say that again|what was that)(?: please)?", norm(text)) is not None


_N = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|first|second|third|fourth|fifth)"
_TARGET = rf"(?:in|into|to|on) (?:(?:this|the|that|my|current|the current) )?(?:tmux )?(?:window|pane|session|terminal)(?: {_N})?"
_DANGLING = re.compile(rf"(?:(?:type|typing|write|send|dictate)(?: (?:this|the following))?(?: {_TARGET})?|{_TARGET})"
                       r"(?: please)?")


def dangling(text: str) -> bool:
    """Only a command head ("type", "typing", "type in this window", "in window 1"): the rest is still coming.
    Live 2026-10-31 (friction #19-20, #36, #38): the user pauses after the head and the 0.7 s endpoint cuts it."""
    return _DANGLING.fullmatch(norm(text)) is not None


def split_sentences(text: str) -> list[str]:
    """Same sentence ends as the brain's splitter (no cut at an ellipsis or inside quotes)."""
    out, text = [], text.strip()
    while (end := sentence_end(text)) is not None:
        out.append(text[:end])
        text = text[end:].strip()
    return out + [text] if text else out


def hotwords(catalogue: dict | None, user: Iterable[str] = (), local: Iterable[str] = ()) -> str:
    """Whisper hint string, most wanted first: voice.toml `stt_hotwords`, static, sites, projects/apps, verb names."""
    words = [*user, *STATIC_HOTWORDS, *SITES, *local]
    for v in (catalogue or {}).get("verbs", []):
        words += re.split(r"[\s.]+", v.get("name", ""))  # built names are dotted ("tmux.switch")
    out, seen, size = [], set(), 0
    for w in (w.strip() for w in words):
        if not w or w.lower() in seen:
            continue
        if size + len(w) + 1 > HOTWORDS_MAX:
            break
        seen.add(w.lower())
        out.append(w)
        size += len(w) + 1
    return " ".join(out)
