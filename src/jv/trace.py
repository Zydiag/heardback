"""trace.jsonl: one JSON object per line, appended by every heardback process (voice, brain, each `jv` run).

Each event is one `os.write` on an O_APPEND fd, so lines from several processes never interleave. Only heardbackd
rotates (`rotate()`). A write failure never raises: it warns once on stderr. See docs/observability.md."""
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

MAX_STR = 500
ROTATE_BYTES = 20 * 1024 * 1024
KEEP = 5

_fd: int | None = None
_fd_path: Path | None = None
_warned = False


def path() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "heardback" / "trace.jsonl"


TAIL_KEYS = {"text", "stdout"}   # screen reads, tool results: the bottom is the part that matters
HEAD, TAIL = 150, 350


def cut(v, key: str | None = None):
    """Strings over MAX_STR chars are cut (recursively through dicts and lists); `text`/`stdout` keep
    head 150 + "…" + tail 350, others the first 500 + "…"."""
    if isinstance(v, str):
        if len(v) <= MAX_STR:
            return v
        return v[:HEAD] + "…" + v[-TAIL:] if key in TAIL_KEYS else v[:MAX_STR] + "…"
    if isinstance(v, dict):
        return {str(k): cut(x, str(k)) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [cut(x, key) for x in v]
    return v


def _close() -> None:
    global _fd, _fd_path
    if _fd is not None:
        try:
            os.close(_fd)
        except OSError:
            pass
    _fd = _fd_path = None


def _open(p: Path) -> int:
    global _fd, _fd_path
    if _fd is None or _fd_path != p:   # tests (and rotate) change the path
        _close()
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _fd, _fd_path = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600), p
    return _fd


def emit(src: str, ev: str, turn: str | None = None, **fields) -> None:
    global _warned
    try:
        rec = {"ts": datetime.now().astimezone().isoformat(timespec="milliseconds"), "src": src, "ev": ev,
               "turn": turn, **cut(fields)}
        os.write(_open(path()), (json.dumps(rec, ensure_ascii=False, default=str) + "\n").encode())
    except Exception as e:   # tracing must never break a turn
        if not _warned:
            _warned = True
            print(f"heardback trace: cannot write {path()} ({e!r}); later failures are silent", file=sys.stderr)


def error(src: str, turn: str | None, where: str, exc: BaseException) -> None:
    emit(src, "error", turn, where=where, error=repr(exc),
         traceback="".join(traceback.format_exception(exc))[-MAX_STR:])   # the tail holds the raising frame


def rotate(max_bytes: int = ROTATE_BYTES, keep: int = KEEP) -> bool:
    """heardbackd only: over `max_bytes` → trace.jsonl.1 … .{keep}, then this process reopens. True if rotated."""
    p = path()
    try:
        if p.stat().st_size <= max_bytes:
            return False
        for i in range(keep - 1, 0, -1):
            old = p.with_name(f"{p.name}.{i}")
            if old.exists():
                os.replace(old, p.with_name(f"{p.name}.{i + 1}"))
        os.replace(p, p.with_name(f"{p.name}.1"))
    except OSError:
        return False
    _close()
    return True
