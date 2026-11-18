"""Session store: one JSON file per Claude Code session plus an events log."""
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from heardback.migrate import LEGACY_NAME

LINE_MAX = 4096
LIST_ENDED_WINDOW_S = 3600
GC_ENDED_DAYS = 7
EVENTS_ROTATE_BYTES = 1_000_000
FIELDS = ("session_id", "tmux_pane", "tmux_target", "cwd", "project", "state", "waiting_for", "last_prompt",
          "last_reply", "turn_started_at", "claude_pid", "background", "transcript_path", "updated_at", "end_reason",
          "last_hook", "internal", "started_at")


def state_dir(*, create: bool = True) -> Path:
    d = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "heardback"
    if create:
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def session_path(sid: str) -> Path:
    return state_dir() / "sessions" / f"{sid}.json"


def read_raw(sid: str) -> dict | None:
    try:
        data = json.loads(session_path(sid).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_session(rec: dict) -> None:
    p = session_path(rec["session_id"])
    p.parent.mkdir(mode=0o700, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(rec, f)
        os.replace(tmp, p)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def append_event(ev: dict) -> None:
    line = json.dumps(ev) + "\n"
    # ponytail: only `detail` is shortened; other fields are short by construction (ids, states, a path).
    while len(line.encode()) >= LINE_MAX and ev.get("detail"):
        ev = {**ev, "detail": ev["detail"][: len(ev["detail"]) // 2]}
        line = json.dumps(ev) + "\n"
    fd = os.open(state_dir() / "events.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)


def now_iso(t: float | None = None) -> str:
    return (datetime.fromtimestamp(t) if t is not None else datetime.now()).astimezone().isoformat(timespec="milliseconds")


def epoch(iso: str | None) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp() if iso else 0.0
    except (TypeError, ValueError):  # a hand-edited or foreign file must not break readers or the hook
        return 0.0


def alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip() == "claude"
    except (OSError, RuntimeError):
        return False


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def brain_cwd() -> Path:
    """The private working directory used by heardback's persistent brain process."""
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "heardback" / "brain-cwd"


def _is_brain_cwd(cwd: object) -> bool:
    if not isinstance(cwd, str) or not cwd:
        return False
    try:
        path = Path(cwd).expanduser().resolve()
        return path.is_relative_to(brain_cwd().resolve()) or path.parts[-2:] in (
            ("heardback", "brain-cwd"), (LEGACY_NAME, "brain-cwd"))
    except (OSError, RuntimeError):
        return False


def _is_brain_process(pid: object) -> bool:
    """Read only the heardback marker from /proc; never expose the process environment."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return False
    return b"HEARDBACK_BRAIN=1" in env


def is_internal(rec: dict) -> bool:
    """True for the heardback brain, including old registry and hook records."""
    return rec.get("internal") is True or _is_brain_cwd(rec.get("cwd")) or _is_brain_process(rec.get("claude_pid"))


def user_sessions(sessions: list[dict], *, include_ended: bool = False) -> list[dict]:
    """User-visible Claude sessions. History remains opt-in for normal status views."""
    return [s for s in sessions if not is_internal(s) and (include_ended or s.get("state") != "ended")]


def _from_registry(d: dict) -> dict:
    # Claude Code's own registry, undocumented; key shape probed 2026-10-30. "shell" = idle at the prompt.
    sid, cwd, pane = d["sessionId"], d["cwd"], (d.get("tmux") or "").rpartition(".")[2]
    if not isinstance(sid, str):
        raise TypeError("sessionId")
    started_ms = d.get("startedAt")
    updated_ms = d.get("statusUpdatedAt") or d.get("updatedAt") or started_ms
    if not isinstance(updated_ms, (int, float)) or isinstance(updated_ms, bool):
        raise ValueError("registry timestamps")
    started_at = now_iso(started_ms / 1000) if isinstance(started_ms, (int, float)) and not isinstance(started_ms, bool) else None
    t = claude_dir() / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd) / f"{sid}.jsonl"
    if not t.exists() and re.fullmatch(r"[\w-]+", sid):  # cwd moved since start, or a shortened long-path slug
        t = next((claude_dir() / "projects").glob(f"*/{sid}.jsonl"), t)
    return {**dict.fromkeys(FIELDS), "session_id": sid, "claude_pid": d["pid"], "cwd": cwd,
            "project": os.path.basename(cwd.rstrip("/")), "tmux_pane": pane if pane.startswith("%") else None,
            "state": {"busy": "working", "waiting": "waiting"}.get(d.get("status"), "idle"),
            "waiting_for": d.get("waitingFor"), "updated_at": now_iso(updated_ms / 1000),
            "transcript_path": str(t) if t.exists() else None,
            "started_at": started_at,
            "internal": _is_brain_cwd(cwd) or _is_brain_process(d.get("pid")), "source": "registry"}


def registry_sessions() -> list[dict]:
    out = []
    for p in sorted((claude_dir() / "sessions").glob("*.json")):
        try:
            d = json.loads(p.read_text())
            if alive(d["pid"]):
                out.append(_from_registry(d))
        except Exception:
            continue
    return out


def resolve_target(pane: str | None, socket: str | None = None) -> str | None:
    import subprocess
    if not pane or not isinstance(pane, str):
        return None
    socket = socket or os.environ.get("JV_TMUX_SOCKET")
    # display -p on a gone pane exits 0 with blank fields, so check the pane id comes back.
    argv = ["tmux", *(["-L", socket] if socket else []), "display", "-p", "-t", pane, "#{pane_id} #S:#I.#P"]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=1)
    except (OSError, subprocess.SubprocessError):
        return None
    pid, _, target = r.stdout.strip().partition(" ")
    return target if r.returncode == 0 and pid == pane and target else None


def pane_targets(socket: str | None = None) -> dict[str, tuple[str, str]]:
    """{pane_id: ("session:window.pane", window_name)} for every pane, in one tmux call; {} if tmux isn't reachable."""
    import subprocess
    socket = socket or os.environ.get("JV_TMUX_SOCKET")
    argv = ["tmux", *(["-L", socket] if socket else []), "list-panes", "-a", "-F", "#{pane_id} #S:#I.#P #W"]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=1)
    except (OSError, subprocess.SubprocessError):
        return {}
    rows = (line.split(" ", 2) for line in r.stdout.splitlines()) if r.returncode == 0 else ()
    return {row[0]: (row[1], row[2] if len(row) > 2 else "") for row in rows if len(row) > 1 and row[0] and row[1]}


def _ts(rec: dict) -> float:
    try:
        return epoch(rec.get("updated_at"))
    except (TypeError, ValueError):
        return 0.0


def _identity_started(rec: dict) -> float:
    """When this session identity began, preferring registry/session-start evidence."""
    if rec.get("source") == "registry":
        return epoch(rec.get("started_at"))
    return epoch(rec.get("started_at") or rec.get("updated_at"))


def _norm(d: object) -> dict | None:
    """A hook file with missing keys defaulted and a dead claude process marked ended."""
    if not isinstance(d, dict) or not isinstance(d.get("session_id"), str):
        return None
    rec = {**dict.fromkeys(FIELDS), **d, "source": "hook"}
    if not isinstance(rec["claude_pid"], int) or isinstance(rec["claude_pid"], bool):
        rec["claude_pid"] = None
    if not isinstance(rec["tmux_pane"], str):
        rec["tmux_pane"] = None
    rec["internal"] = is_internal(rec)
    if rec["state"] != "ended" and not alive(rec["claude_pid"]):
        rec.update(state="ended", end_reason="gone")
    return rec


def get_by_id(sid: str) -> dict | None:
    return _norm(read_raw(sid))


def list_sessions(now: float | None = None, resolve: bool = True, *, create_state_dir: bool = True) -> list[dict]:
    now = time.time() if now is None else now
    recs = []
    for p in (state_dir(create=create_state_dir) / "sessions").glob("*.json"):
        try:
            rec = _norm(json.loads(p.read_text()))
        except (OSError, ValueError):
            continue
        if rec:
            recs.append(rec)
    recs.sort(key=_ts, reverse=True)
    registry = registry_sessions()
    for candidate in registry:
        if any(r["session_id"] == candidate["session_id"] for r in recs):
            continue  # a hook file for this identity wins
        if is_internal(candidate):
            # Internal records are diagnostic only; they must not replace or end user records.
            recs.append(candidate)
            continue
        live_users = [r for r in recs if not is_internal(r) and r["state"] != "ended"]
        same_process_pane = [r for r in live_users if candidate.get("tmux_pane")
                             and r.get("claude_pid") == candidate.get("claude_pid")
                             and r.get("tmux_pane") == candidate.get("tmux_pane")
                             and r.get("session_id") != candidate.get("session_id")]
        if same_process_pane:
            if _identity_started(candidate) > max(_identity_started(r) for r in same_process_pane):
                for old in same_process_pane:
                    old.update(state="ended", end_reason="replaced")  # read-time reconciliation; no hook is invented
                recs.append(candidate)
            continue
        if any(r.get("claude_pid") == candidate.get("claude_pid") for r in live_users):
            continue  # same PID without the same stable pane does not prove an identity rotation
        recs.append(candidate)

    # Resolve duplicate live identities across hook and registry sources, independently for user and internal rows.
    recs.sort(key=lambda r: (_identity_started(r), _ts(r)), reverse=True)
    seen_panes: dict[bool, set[str]] = {False: set(), True: set()}
    for r in recs:
        pane = r.get("tmux_pane")
        internal = is_internal(r)
        if r["state"] != "ended" and pane:
            if pane in seen_panes[internal]:
                r.update(state="ended", end_reason="replaced")
            else:
                seen_panes[internal].add(pane)
    recs = [r for r in recs if r["state"] != "ended" or now - _ts(r) <= LIST_ENDED_WINDOW_S]
    recs.sort(key=_ts, reverse=True)
    if resolve:
        targets = pane_targets() if any(r["tmux_pane"] for r in recs) else {}
        for r in recs:
            r["tmux_target"], r["tmux_window"] = targets.get(r["tmux_pane"], (None, None))
    observed_at = now_iso(now)
    for r in recs:
        r["identity"] = {"session_id": r["session_id"], "pane_id": r.get("tmux_pane")}
        r["observation"] = {"source": r.get("source"), "observed_at": observed_at,
                            "state_updated_at": r.get("updated_at"),
                            "state_age_s": round(max(0.0, now - _ts(r)), 3)}
    return recs


def gc(now: float | None = None) -> dict:
    """Delete ended session files older than GC_ENDED_DAYS; rotate a big events.jsonl to events.jsonl.1."""
    now = time.time() if now is None else now
    deleted = 0
    for p in (state_dir() / "sessions").glob("*.json"):
        try:
            rec = _norm(json.loads(p.read_text()))
        except (OSError, ValueError):
            continue  # unreadable: leave it for a human
        if rec and rec["state"] == "ended" and now - _ts(rec) > GC_ENDED_DAYS * 86400:
            p.unlink(missing_ok=True)
            deleted += 1
    ev = state_dir() / "events.jsonl"
    rotated = ev.exists() and ev.stat().st_size > EVENTS_ROTATE_BYTES
    if rotated:
        os.replace(ev, ev.with_name("events.jsonl.1"))
    return {"deleted": deleted, "rotated": rotated}
