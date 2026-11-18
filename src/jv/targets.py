"""Shared spoken-target normalization for tmux actions and Claude session status."""
import re

CURRENT = {"this", "current", "here", "active"}
NUMBER_WORDS = {word: str(n) for n, word in enumerate("zero one two three four five six seven eight nine ten".split())}
ORDINALS = {word: n for n, word in enumerate(
    "first second third fourth fifth sixth seventh eighth ninth tenth".split(), 1)}
WINDOW_TERMS = {"the", "tmux", "window", "windows", "number"}


def normalize_window_target(target: str, *, window_indices: list[str] | None = None,
                            window_names: list[str] | tuple[str, ...] = ()) -> str:
    """Normalize labels and spoken numbers while preserving window names and stable pane targets.

    Tmux passes its inventory so ordinal phrases keep their positional meaning (including sessions
    whose window numbering starts at zero). Status lookup has no full tmux inventory, so an ordinal
    there means the corresponding numeric window index.
    """
    raw = str(target).strip()
    if name := next((name for name in window_names if raw.casefold() == name.casefold()), None):
        return name
    if re.fullmatch(r"%\d+|[^:\s]+:\d+(?:\.\d+)?", raw):
        return raw
    if m := re.fullmatch(r"(?:the )?pane(?: id)?\s+(%\d+)", raw, re.I):
        return m[1]

    words = raw.split()
    filtered = [word for word in words if word.casefold() not in WINDOW_TERMS]
    if not filtered:
        return raw
    if len(filtered) == 1:
        word = filtered[0]
        key = word.casefold()
        if key in CURRENT:
            return "current"
        if key in NUMBER_WORDS:
            return NUMBER_WORDS[key]
        if word.isdecimal():
            return str(int(word))
        if key in ORDINALS:
            rank = ORDINALS[key]
            if window_indices is None:
                return str(rank)
            return window_indices[rank - 1] if rank <= len(window_indices) else raw
        return word
    return " ".join(filtered) if filtered != words else raw
