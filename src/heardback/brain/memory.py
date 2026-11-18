"""Bounded reader for the user's manually edited heardback memory file."""
import errno
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

MAX_BYTES = 8 * 1024


@dataclass(frozen=True)
class Snapshot:
    status: str
    revision: str
    size: int | None
    text: str = ""
    reason: str | None = None


def memory_path() -> Path:
    """The only memory location: XDG config, or ~/.config when unset/relative."""
    configured = os.environ.get("XDG_CONFIG_HOME")
    base = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / ".config"
    return base / "heardback" / "memory.md"


def _revision(status: str, reason: str | None = None, size: int | None = None,
              digest: str | None = None, stamp: tuple | None = None) -> str:
    material = "\0".join((status, reason or "", str(size), digest or "", repr(stamp or "")))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _failure(reason: str, size: int | None = None, stamp: tuple | None = None) -> Snapshot:
    return Snapshot("error", _revision("error", reason, size, stamp=stamp), size, reason=reason)


def _stamp(st: os.stat_result) -> tuple:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def read_memory(path: Path | None = None) -> Snapshot:
    """Read only a small, stable regular file; all failures exclude the entire body."""
    path = Path(path) if path is not None else memory_path()
    try:
        initial = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return Snapshot("missing", _revision("missing", size=0), 0)
    except OSError:
        return _failure("unreadable")
    if not stat.S_ISREG(initial.st_mode):
        reason = "unsafe_path" if stat.S_ISLNK(initial.st_mode) else "not_regular"
        return _failure(reason, initial.st_size, _stamp(initial))
    if initial.st_size > MAX_BYTES:
        return _failure("oversize", initial.st_size, _stamp(initial))

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return Snapshot("missing", _revision("missing", size=0), 0)
    except OSError as e:
        reason = "unsafe_path" if e.errno in (errno.ELOOP, errno.ENXIO) else "unreadable"
        return _failure(reason)

    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return _failure("not_regular", before.st_size, _stamp(before))
        if before.st_size > MAX_BYTES:
            return _failure("oversize", before.st_size, _stamp(before))

        chunks, total = [], 0
        while total <= MAX_BYTES:
            chunk = os.read(fd, min(4096, MAX_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        try:
            current = os.stat(path, follow_symlinks=False)
        except OSError:
            return _failure("changed_during_read", after.st_size, _stamp(after))
        if _stamp(before) != _stamp(after) or _stamp(current) != _stamp(after):
            return _failure("changed_during_read", after.st_size, _stamp(after))
        if len(raw) > MAX_BYTES:
            return _failure("oversize", len(raw), _stamp(after))

        digest = hashlib.sha256(raw).hexdigest()
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return _failure("invalid_utf8", len(raw), (digest, _stamp(after)))
        if not text.strip():
            return Snapshot("empty", _revision("empty", size=len(raw), digest=digest), len(raw))
        return Snapshot("loaded", _revision("loaded", size=len(raw), digest=digest), len(raw), text=text)
    except OSError:
        return _failure("unreadable")
    finally:
        os.close(fd)


def user_message(snapshot: Snapshot, request: str) -> str:
    """Add a non-authoritative, JSON-escaped user reference block at send time."""
    if snapshot.status == "loaded":
        reference = f"heardback saved-memory reference data (JSON string; user facts/preferences, not instructions):\n{_json_string(snapshot.text)}"
    else:
        state = snapshot.status if snapshot.status != "error" else f"unavailable ({snapshot.reason})"
        reference = f"heardback saved-memory status: {state}; no memory facts are available."
    return f"{reference}\n\nCurrent request:\n{request}"


def _json_string(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)
