"""Cut streamed text deltas into speakable sentences."""
import re

SENTENCE_MAX = 200
# A closing quote stays with its sentence; an ellipsis ("..." / "…") never ends one (live 22:57:34: a video title
# "Just Thinking...Retro Jazz" was cut into two says). Never inside an open quote.
_END = re.compile(r'(?<!\.)[.!?]["”]?(?=\s)')
_MARKDOWN = str.maketrans("", "", "*#`")


def _quoted(text: str) -> bool:
    return text.count('"') % 2 == 1 or text.count("“") > text.count("”")


def sentence_end(text: str) -> int | None:
    """Index just past the first sentence end in `text` (needs the whitespace after it), or None."""
    return next((m.end() for m in _END.finditer(text) if not _quoted(text[:m.end()])), None)


def clean(text: str) -> str:
    return " ".join(text.translate(_MARKDOWN).split())


class SentenceSplitter:
    def __init__(self, max_len: int = SENTENCE_MAX):
        self.buf, self.max_len = "", max_len

    def feed(self, delta: str) -> list[str]:
        """Finished sentences: cut after `.`/`!`/`?` + whitespace if within max_len, else on the last space."""
        self.buf += delta.translate(_MARKDOWN)
        out = []
        while True:
            lead = len(self.buf) - len(self.buf.lstrip())
            if (end := sentence_end(self.buf)) and end - lead <= self.max_len:
                cut = end
            elif len(self.buf) - lead > self.max_len:
                sp = self.buf.rfind(" ", lead, lead + self.max_len + 1)
                cut = sp if sp > lead else lead + self.max_len
            else:
                return out
            if s := clean(self.buf[:cut]):
                out.append(s)
            self.buf = self.buf[cut:]

    def split(self, text: str) -> list[str]:
        """A whole text at once: every piece, leftover included, within max_len."""
        return [s for s in (*self.feed(text), self.flush()) if s]

    def flush(self) -> str:
        s, self.buf = clean(self.buf), ""
        return s


def unquoted(text: str) -> str:
    return re.sub(r'"[^"]*"|“[^”]*”', " ", text)


def asks(text: str) -> bool:
    """A question for the user: a `?` in one of the reply's last two sentences."""
    return "?" in "".join(re.split(r"(?<=[.!?])\s+", unquoted(text).strip())[-2:])
