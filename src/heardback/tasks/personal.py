"""Private reminders and inbox at the existing XDG database path; no ledger migration."""
from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import unicodedata
from urllib.parse import quote
import uuid

SCHEMA_VERSION = 4
APPLICATION_ID = 0x4A565453
BUSY_TIMEOUT_MS = 3000
MAX_LIST_LIMIT = 100
MAX_REVISION = 2_147_483_647
ENTITY_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
STATES = ("pending", "delivering", "spoken", "cancelled", "unknown")


class PersonalStoreError(Exception):
    """A controlled storage or validation failure suitable for typed CLI output."""

    def __init__(self, code: str, message: str, exit_code: int = 1):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def database_path() -> Path:
    """Return the XDG-aware personal DB path without creating or resolving anything."""
    configured = os.environ.get("XDG_STATE_HOME")
    base = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / ".local" / "state"
    if ".." in base.parts:
        raise PersonalStoreError("unsafe", "personal store path is unsafe")
    return base / "heardback" / "tasks.sqlite3"


def _bad(code: str, message: str, exit_code: int = 1) -> PersonalStoreError:
    return PersonalStoreError(code, message, exit_code)


def _prepare_private_dir(path: Path, *, create: bool) -> bool:
    """Check every path component without following symlinks; create missing dirs privately."""
    if not path.is_absolute() or ".." in path.parts:
        raise _bad("unsafe", "personal store path is unsafe")
    euid = os.geteuid()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            if not create:
                return False
            try:
                os.mkdir(current, 0o700)
                os.chmod(current, 0o700, follow_symlinks=False)
                st = os.lstat(current)
            except FileExistsError:
                st = os.lstat(current)
            except OSError as e:
                raise _bad("storage", "cannot create personal storage safely") from e
        except OSError as e:
            raise _bad("storage", "cannot inspect personal storage safely") from e

        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise _bad("unsafe", "personal store path contains an unsafe directory")
        # A writable non-sticky parent can let another account replace the private leaf.
        if st.st_mode & 0o022 and not st.st_mode & stat.S_ISVTX:
            raise _bad("unsafe", "personal store path has a shared writable directory")
        if current == path:
            if st.st_uid != euid or st.st_mode & 0o077:
                raise _bad("unsafe", "heardback personal directory must be owned by this user and private")
    return True


def _validate_regular_private_file(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as e:
        raise _bad("storage", "cannot inspect personal database safely") from e
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077
            or st.st_nlink != 1):
        raise _bad("unsafe", "personal database or SQLite sidecar is not a private regular file")
    return True


def _prepare_database(path: Path) -> None:
    try:
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        pass
    except OSError as e:
        raise _bad("storage", "cannot create personal database safely") from e
    else:
        try:
            os.fchmod(fd, 0o600)
            os.fsync(fd)
        except OSError as e:
            raise _bad("storage", "cannot secure personal database") from e
        finally:
            os.close(fd)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as e:
            raise _bad("storage", "cannot secure personal database directory entry") from e

    _validate_regular_private_file(path)
    for suffix in ("-journal", "-wal", "-shm"):
        _validate_regular_private_file(path.with_name(path.name + suffix))


def _personal_schema_statements() -> tuple[str, ...]:
    return (
        """CREATE TABLE reminders (
            reminder_id TEXT PRIMARY KEY,
            request_key TEXT NOT NULL UNIQUE,
            text TEXT NOT NULL,
            due_at INTEGER NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('pending','delivering','spoken','cancelled','unknown')),
            revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            spoken_at INTEGER
        )""",
        "CREATE INDEX reminders_state_due ON reminders(state, due_at)",
        """CREATE TABLE inbox (
            item_id TEXT PRIMARY KEY,
            request_key TEXT NOT NULL UNIQUE,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
    )


def _check_schema(conn):
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION or conn.execute(
            "PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
        raise _bad("schema", "personal database schema is missing or unsupported")
    # Ignore retired ledger objects, while retaining exact checks on the tables we use.
    actual = {row[0]: " ".join(row[1].split()).casefold().rstrip(";") for row in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE tbl_name IN ('reminders', 'inbox') "
        "AND name NOT LIKE 'sqlite_%'")}
    expected = {sql.split()[2]: " ".join(sql.split()).casefold() for sql in _personal_schema_statements()}
    if actual != expected:
        raise _bad("schema", "personal database schema is incomplete or unsupported")


@contextmanager
def _open(*, write=False):
    conn = None
    try:
        path = database_path()
        if not _prepare_private_dir(path.parent, create=write):
            yield None
            return
        if write:
            _prepare_database(path)
        elif not _validate_regular_private_file(path):
            yield None
            return
        for suffix in ("-journal", "-wal", "-shm"):
            _validate_regular_private_file(path.with_name(path.name + suffix))
        uri = f"file:{quote(str(path), safe='/')}?mode={'rw' if write else 'ro'}"
        conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        conn.row_factory = sqlite3.Row
        if write:
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
                if conn.execute("PRAGMA application_id").fetchone()[0] not in (0, APPLICATION_ID) or conn.execute(
                        "SELECT 1 FROM sqlite_master").fetchone():
                    raise _bad("schema", "personal database schema is missing or unsupported")
                for sql in _personal_schema_statements():
                    conn.execute(sql)
                conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            _check_schema(conn)
            conn.commit()
            if conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise _bad("storage", "personal database could not enable WAL")
            conn.execute("BEGIN IMMEDIATE")
        else:
            conn.execute("PRAGMA query_only=ON")
            try:
                _check_schema(conn)
            except sqlite3.Error as exc:
                if getattr(exc, "sqlite_errorcode", None) != sqlite3.SQLITE_READONLY_ROLLBACK:
                    raise
                # Recover a pre-WAL hot journal without creating or migrating a schema.
                recovery = sqlite3.connect(uri.replace("?mode=ro", "?mode=rw"), uri=True,
                                           timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
                try:
                    recovery.execute("SELECT count(*) FROM sqlite_master").fetchone()
                finally:
                    recovery.close()
                _check_schema(conn)
        yield conn
        if write:
            conn.commit()
    except sqlite3.Error as exc:
        code = getattr(exc, "sqlite_errorcode", None)
        if code is not None and code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise _bad("busy", "personal store is busy; retry the command", 3) from exc
        raise _bad("storage", "personal database is unavailable or corrupt") from exc
    except OSError as exc:
        raise _bad("storage", "personal database is unavailable") from exc
    finally:
        if conn is not None:
            conn.close()  # Rolls back an unfinished write or read snapshot.


def verify_store():
    """Initialize the personal tables and check their integrity before reminder recovery."""
    with _open(write=True) as conn:
        for table in ("reminders", "inbox"):
            if conn.execute(f"PRAGMA quick_check({table})").fetchone()[0] != "ok":
                raise _bad("corrupt", "personal database failed its integrity check")


def _clean_text(value: object, name: str, max_bytes: int) -> str:
    if not isinstance(value, str):
        raise _bad("invalid", f"{name} must be text", 2)
    text = value.strip()
    if not text:
        raise _bad("invalid", f"{name} is required", 2)
    try:
        size = len(text.encode("utf-8", errors="strict"))
    except UnicodeError as e:
        raise _bad("invalid", f"{name} is not valid UTF-8 text", 2) from e
    if size > max_bytes:
        raise _bad("invalid", f"{name} exceeds its {max_bytes}-byte limit", 2)
    if any(unicodedata.category(char) == "Cc" for char in text):
        raise _bad("invalid", f"{name} contains a control character", 2)
    return text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _validate_revision(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_REVISION:
        raise _bad("invalid", f"{name} must be a positive whole number", 2)
    return value


def _valid_record_id(value: object, name: str) -> str:
    if not isinstance(value, str) or not ENTITY_ID_RE.fullmatch(value):
        raise _bad("invalid", f"{name} must be a 32-character lowercase hexadecimal ID", 2)
    return value


def _limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_LIST_LIMIT:
        raise _bad("invalid", f"limit must be between 1 and {MAX_LIST_LIMIT}", 2)
    return value


def _epoch(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2**63:
        raise _bad("invalid", f"{name} must be nonnegative integer epoch seconds", 2)
    return value


def _row(row):
    return dict(row) if row is not None else None


def add_reminder(*, request_key: object, text: object, due_at: object) -> dict:
    """Add once per request key; a retry returns the original committed row."""
    key = _clean_text(request_key, "request key", 256)
    text = _clean_text(text, "reminder text", 300)
    due_at = _epoch(due_at, "due_at")
    now = _now()
    with _open(write=True) as conn:
        conn.execute(
            "INSERT INTO reminders VALUES (?, ?, ?, ?, 'pending', 1, ?, ?, NULL) "
            "ON CONFLICT(request_key) DO NOTHING",
            (uuid.uuid4().hex, key, text, due_at, now, now),
        )
        return dict(conn.execute("SELECT * FROM reminders WHERE request_key=?", (key,)).fetchone())


def get_reminder(reminder_id: object) -> dict | None:
    reminder_id = _valid_record_id(reminder_id, "reminder ID")
    with _open() as conn:
        return None if conn is None else _row(conn.execute(
            "SELECT * FROM reminders WHERE reminder_id=?", (reminder_id,)).fetchone())


def list_reminders(*, state: str | None = None, due_before: object = None,
                   due_after: object = None, limit: int = 20) -> list[dict]:
    """Return oldest due first, breaking equal-time ties by stable ID."""
    if state not in (None, *STATES):
        raise _bad("invalid", "unsupported reminder state", 2)
    limit = _limit(limit)
    filters, values = [], []
    if state is not None:
        filters.append("state=?")
        values.append(state)
    if due_before is not None:
        filters.append("due_at<=?")
        values.append(_epoch(due_before, "due_before"))
    if due_after is not None:
        filters.append("due_at>=?")
        values.append(_epoch(due_after, "due_after"))
    where = " WHERE " + " AND ".join(filters) if filters else ""
    with _open() as conn:
        if conn is None:
            return []
        return [dict(row) for row in conn.execute(
            "SELECT * FROM reminders" + where + " ORDER BY due_at, reminder_id LIMIT ?",
            (*values, limit),
        )]


def catchup_reminders(*, now: object) -> dict:
    """Read the complete reminder projection needed by catch-me-up."""
    now = _epoch(now, "now")
    with _open() as conn:
        if conn is None:
            return {"overdue": 0, "spoken_count": 0, "spoken": [], "unknown_count": 0,
                    "unknown": [], "next": None}
        conn.execute("BEGIN")
        overdue = conn.execute(
            "SELECT count(*) FROM reminders WHERE state='pending' AND due_at<=?", (now,)
        ).fetchone()[0]
        spoken_count = conn.execute(
            "SELECT count(*) FROM reminders WHERE state='spoken' AND spoken_at>=? AND spoken_at<=?",
            (now - 12 * 3600, now),
        ).fetchone()[0]
        spoken = [dict(r) for r in conn.execute(
            "SELECT * FROM reminders WHERE state='spoken' AND spoken_at>=? AND spoken_at<=? "
            "ORDER BY spoken_at DESC, reminder_id LIMIT 3", (now - 12 * 3600, now),
        )]
        unknown_window = tuple(
            datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            for t in (now - 12 * 3600, now)
        )
        unknown_count = conn.execute(
            "SELECT count(*) FROM reminders WHERE state='unknown' AND updated_at>=? AND updated_at<=?",
            unknown_window,
        ).fetchone()[0]
        unknown = [dict(r) for r in conn.execute(
            "SELECT * FROM reminders WHERE state='unknown' AND updated_at>=? AND updated_at<=? "
            "ORDER BY updated_at DESC, reminder_id LIMIT 3",
            unknown_window,
        )]
        next_row = conn.execute(
            "SELECT * FROM reminders WHERE state='pending' AND due_at>? ORDER BY due_at, reminder_id LIMIT 1",
            (now,),
        ).fetchone()
        return {"overdue": overdue, "spoken_count": spoken_count, "spoken": spoken,
                "unknown_count": unknown_count, "unknown": unknown, "next": _row(next_row)}


def newest_pending(*, now: object = None) -> dict | None:
    """Newest pending reminder created within the preceding two minutes."""
    now = time.time() if now is None else _epoch(now, "now")
    try:
        end = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        start = datetime.fromtimestamp(now - 120, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError) as exc:
        raise _bad("invalid", "now is outside the supported date range", 2) from exc
    with _open() as conn:
        return None if conn is None else _row(conn.execute(
            "SELECT * FROM reminders WHERE state='pending' AND created_at>=? AND created_at<=? "
            "ORDER BY created_at DESC, reminder_id LIMIT 1", (start, end),
        ).fetchone())


def _move(reminder_id, expected_revision, from_state, to_state):
    reminder_id = _valid_record_id(reminder_id, "reminder ID")
    revision = _validate_revision(expected_revision, "expected revision")
    with _open(write=True) as conn:
        return _row(conn.execute(
            "UPDATE reminders SET state=?, revision=revision+1, updated_at=?, spoken_at=? "
            "WHERE reminder_id=? AND state=? AND revision=? RETURNING *",
            (to_state, _now(), int(time.time()) if to_state == "spoken" else None,
             reminder_id, from_state, revision),
        ).fetchone())


def edit_reminder(reminder_id: object, *, expected_revision: object, text: object, due_at: object) -> dict | None:
    """Change a still-pending row's text and time at the revision the caller observed; the poller re-reads rows each tick."""
    reminder_id = _valid_record_id(reminder_id, "reminder ID")
    revision = _validate_revision(expected_revision, "expected revision")
    text = _clean_text(text, "reminder text", 300)
    due_at = _epoch(due_at, "due_at")
    with _open(write=True) as conn:
        return _row(conn.execute(
            "UPDATE reminders SET text=?, due_at=?, revision=revision+1, updated_at=? "
            "WHERE reminder_id=? AND state='pending' AND revision=? RETURNING *",
            (text, due_at, _now(), reminder_id, revision)).fetchone())


def claim_reminder(reminder_id: object, *, expected_revision: object) -> dict | None:
    """Claim pending speech; None means another move won or the row is absent."""
    return _move(reminder_id, expected_revision, "pending", "delivering")


def cancel_reminder(reminder_id: object, *, expected_revision: object) -> dict | None:
    """Cancel only a still-pending row at the revision the caller observed."""
    return _move(reminder_id, expected_revision, "pending", "cancelled")


def finish_reminder(reminder_id: object, *, expected_revision: object, result: str) -> dict | None:
    """Record spoken, retryable pending, or uncertain unknown for this claim."""
    if result not in ("spoken", "pending", "unknown"):
        raise _bad("invalid", "unsupported reminder delivery result", 2)
    return _move(reminder_id, expected_revision, "delivering", result)


def recover_delivering() -> int:
    """At startup, mark interrupted deliveries unknown without replaying them."""
    with _open(write=True) as conn:
        return conn.execute(
            "UPDATE reminders SET state='unknown', revision=revision+1, updated_at=? "
            "WHERE state='delivering'", (_now(),),
        ).rowcount


def add_inbox(*, request_key: object, text: object) -> dict:
    key = _clean_text(request_key, "request key", 256)
    text = _clean_text(text, "inbox text", 1000)
    with _open(write=True) as conn:
        conn.execute(
            "INSERT INTO inbox VALUES (?, ?, ?, ?) ON CONFLICT(request_key) DO NOTHING",
            (uuid.uuid4().hex, key, text, _now()),
        )
        return dict(conn.execute("SELECT * FROM inbox WHERE request_key=?", (key,)).fetchone())


def list_inbox(*, limit: int = 20) -> list[dict]:
    limit = _limit(limit)
    with _open() as conn:
        return [] if conn is None else [dict(row) for row in conn.execute(
            "SELECT * FROM inbox ORDER BY created_at DESC, item_id LIMIT ?", (limit,),
        )]


def remove_inbox(item_id: object) -> dict | None:
    item_id = _valid_record_id(item_id, "inbox item ID")
    with _open(write=True) as conn:
        return _row(conn.execute("DELETE FROM inbox WHERE item_id=? RETURNING *", (item_id,)).fetchone())
