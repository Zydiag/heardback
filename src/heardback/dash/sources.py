"""Dashboard observations: JSON-able panels from files and two tools, plus private append-only turn notes.

`LIVE` is filled by heardbackd (the Brain) and the voice daemon (models, mic, wake, speaker) at startup, so the service
panel shows what the process really loaded. Slow tools are cached and time-limited; see docs/plans/06-dashboard.md."""
import json
import os
import re
import subprocess
import threading
import time
from datetime import date
from functools import lru_cache

from heardback.status import store
from jv import trace

LIVE: dict = {}          # brain, voice, mic, models, cfg: set by heardbackd / the voice daemon
START = time.time()      # heardbackd imports this module at startup: service uptime
TOOL_TIMEOUT_S = 3
TAIL_BYTES = 256 * 1024
# ponytail: window by model name; the transcript doesn't say. Knob if a model's window differs.
WINDOWS = {"haiku": 200_000}
DEFAULT_WINDOW = 1_000_000
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
TURN_ID = re.compile(r"[0-9]{8}-[0-9]{6}-(?:[0-9]+c?|b[0-9]+)")
NOTE_LOCK = threading.Lock()


def notes_path():
    return trace.path().with_name("turn-notes.jsonl")


def append_note(turn: str | None, text: str) -> None:
    """Append private observations; the HTTP handler validates input before this boundary."""
    with NOTE_LOCK:
        p = notes_path()
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with os.fdopen(os.open(p, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600), "ab+") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            # A damaged final line must not swallow the next appended note.
            if size:
                f.seek(size - 1)
                if f.read() != b"\n":
                    f.write(b"\n")
            f.write((json.dumps({"ts": time.time(), "turn": turn, "text": text}, ensure_ascii=False) + "\n").encode())


def reminders() -> dict:
    """Every pending reminder, soonest first, through the store the `jv reminder` verbs use."""
    from jv import reminder
    return {"rows": [{k: r[k] for k in ("reminder_id", "revision", "text", "due_at")}
                     for r in reminder._store(reminder._pending)]}


def change_reminder(data: dict) -> tuple[int, str]:
    """Apply one add/edit/cancel from the dashboard with the voice limits; returns (status, reason)."""
    from heardback.tasks import personal
    from uuid import uuid4
    op, rid, due = data.get("op"), data.get("id"), data.get("due_at")
    try:
        if op not in ("add", "edit", "cancel"):
            return 400, "op must be add, edit or cancel"
        if op != "add":
            row = personal.get_reminder(rid)
            if row is None or row["state"] != "pending":
                return 409, "that reminder is no longer pending"
        if op == "cancel":
            done = personal.cancel_reminder(rid, expected_revision=row["revision"])
        else:
            if isinstance(due, bool) or not isinstance(due, int) or due <= time.time():
                return 400, "pick a time in the future"
            if op == "add":
                done = personal.add_reminder(request_key="dash:" + uuid4().hex, text=data.get("text"), due_at=due)
            else:
                done = personal.edit_reminder(rid, expected_revision=row["revision"], text=data.get("text"), due_at=due)
        return (204, "") if done else (409, "that reminder changed; reload")
    except personal.PersonalStoreError as e:
        return (400 if e.exit_code == 2 else 500), e.message


def notes() -> list[dict]:
    """Valid observations, newest first; damaged lines remain on disk and are skipped."""
    with NOTE_LOCK:
        p = notes_path()
        try:
            st = p.stat()
        except FileNotFoundError:
            return []
        rows = _events(str(p), (st.st_size, st.st_mtime_ns))
    return [r for r in reversed(rows) if "turn" in r and type(r.get("ts")) in (int, float) and 0 <= r["ts"] < 253402300800
            and isinstance(r.get("text"), str) and 1 <= len(r["text"].strip()) <= 2000
            and (r.get("turn") is None or isinstance(r["turn"], str) and TURN_ID.fullmatch(r["turn"]))]


def cached(ttl: float):
    """Keep a result (or the exception) for `ttl` s; one caller at a time runs the tool."""
    def deco(fn):
        lock, memo = threading.Lock(), {}

        def wrapper(*a):
            with lock:
                hit = memo.get(a)
                if hit is None or time.monotonic() - hit[0] > ttl:
                    try:
                        hit = (time.monotonic(), fn(*a), None)
                    except Exception as e:
                        hit = (time.monotonic(), None, e)
                    memo[a] = hit
            if hit[2] is not None:
                raise hit[2]
            return hit[1]
        wrapper.clear = memo.clear
        return wrapper
    return deco


def _run(argv: list[str]) -> str:
    r = subprocess.run(argv, capture_output=True, text=True, timeout=TOOL_TIMEOUT_S,
                       env={**os.environ, "NO_COLOR": "1"}, stdin=subprocess.DEVNULL)
    if r.returncode:
        raise RuntimeError(f"{argv[0]} rc={r.returncode}: {(r.stderr or r.stdout).strip()[:200]}")
    return ANSI.sub("", r.stdout)


# --- sessions ---------------------------------------------------------------------------------------------------
@lru_cache(maxsize=64)
def _context(path: str, _mtime_ns: int) -> tuple[int | None, str | None]:
    """(tokens in context, model) from the last main-thread assistant `usage`: the status line's maths."""
    with open(path, "rb") as f:
        start = max(0, os.fstat(f.fileno()).st_size - TAIL_BYTES)
        f.seek(start)
        lines = f.read().split(b"\n")[1 if start else 0:]
    for line in reversed(lines):
        if b'"usage"' not in line:
            continue
        try:
            d = json.loads(line)
            m = d["message"]
            u = m["usage"]
        except (ValueError, KeyError, TypeError):
            continue
        if d.get("type") != "assistant" or d.get("isSidechain"):
            continue
        tok = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        if tok:
            return tok, m.get("model")
    return None, None


def window(model: str | None) -> int:
    return next((n for k, n in WINDOWS.items() if k in (model or "")), DEFAULT_WINDOW)


@cached(60)
def spend() -> dict:
    out = _run(["codeburn", "status"])
    m = re.search(r"Today\s+\$([\d,.]+)\s+([\d,]+)\s+calls", out)
    if not m:
        raise ValueError(f"codeburn: no 'Today' line in {out.strip()[:120]!r}")
    mo = re.search(r"Month\s+\$([\d,.]+)", out)
    num = lambda s: float(s.replace(",", ""))
    return {"today_usd": num(m[1]), "calls": int(num(m[2])), "month_usd": num(mo[1]) if mo else None}


def limits() -> dict:
    """The 5-hour window. The status line only gets it on stdin; the brain's stream-json has the same account's."""
    rate = getattr(LIVE.get("brain"), "rate", None)
    info, at = getattr(rate, "last_info", None), getattr(rate, "last_at", None)
    if not info:
        return {"source": None, "note": "status line gets rate_limits on stdin only; no brain turn yet"}
    w = (info.get("unifiedWindows") or {}).get("five_hour") or {}
    util, resets = w.get("utilization"), w.get("resetsAt", info.get("resetsAt"))
    return {"source": "brain", "as_of": at, "status": info.get("status"),
            "five_hour_pct": None if util is None else round(float(util) * 100, 1), "resets_at": resets}


def _label(s: dict) -> str:
    return "heardback brain" if (s.get("cwd") or "").endswith("/heardback/brain-cwd") else s.get("project") or "?"


def sessions() -> dict:
    rows = []
    for s in store.list_sessions():
        if s["state"] == "ended":
            continue
        tok = model = None
        if p := s.get("transcript_path"):
            try:
                tok, model = _context(p, os.stat(p).st_mtime_ns)
            except OSError:
                pass
        rows.append({"id": s["session_id"][:8], "target": s.get("tmux_target"), "window": s.get("tmux_window"),
                     "project": _label(s), "state": s.get("state"), "waiting_for": s.get("waiting_for"),
                     "updated": store.epoch(s.get("updated_at")) or None, "source": s.get("source"),
                     "model": model, "ctx_tokens": tok, "ctx_window": window(model) if tok else None})
    try:
        sp = spend()
    except Exception as e:
        sp = {"error": f"{type(e).__name__}: {e}"[:300]}
    return {"rows": rows, "limits": limits(), "spend": sp}


# --- brain ------------------------------------------------------------------------------------------------------
@lru_cache(maxsize=2)
def _events(path: str, _key: tuple) -> list[dict]:
    out = []
    with open(path, errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict):
                out.append(e)
    return out


def events() -> list[dict]:
    """trace.jsonl parsed once per change (keyed on size + mtime); [] when it doesn't exist yet."""
    p = trace.path()
    try:
        st = p.stat()
    except FileNotFoundError:
        return []
    return _events(str(p), (st.st_size, st.st_mtime_ns))


LIFECYCLE = ("claude_spawn", "claude_reset", "claude_exit", "claude_off", "mcp_failed")
RESULT_KEYS = ("ts", "turn", "model", "tokens",
               "output_tokens", "cost", "ms", "api_ms", "tools", "subtype", "is_error")


def brain() -> dict:
    evs = [e for e in events() if e.get("src") == "brain"]
    today = date.today().isoformat()
    res = [e for e in evs if e.get("ev") == "result" and str(e.get("ts", "")).startswith(today)]
    b = LIVE.get("brain")
    proc = getattr(b, "claude", None)
    from heardback.brain import usage
    return {"live": None if b is None else {"model": getattr(b, "model", None),
                                            "off": b.claude_off, "pid": getattr(proc, "pid", None),
                                            "proc_turns": b.proc_turns, "proc_cost": round(b.last_cost, 4)},
            "today": {"turns": len(res), "usage_json_turns": usage.today_turns(),
                      "tokens": sum(int(e.get("tokens") or 0) for e in res),
                      "output_tokens": sum(int(e.get("output_tokens") or 0) for e in res),
                      "cost": round(sum(float(e.get("cost") or 0) for e in res), 4)},
            "recent": [{k: e.get(k) for k in RESULT_KEYS} for e in res[-12:]][::-1],
            "lifecycle": [{k: e.get(k) for k in ("ts", "ev", "reason", "pid", "code", "turn")}
                          for e in evs if e.get("ev") in LIFECYCLE][-6:][::-1]}


# --- gpu / models / service -------------------------------------------------------------------------------------
@cached(5)
def gpu(pid: int) -> dict:
    name, util, used, total = (x.strip() for x in _run(
        ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total",
         "--format=csv,noheader,nounits"]).splitlines()[0].split(","))
    mine = 0
    for line in _run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"]).splitlines():
        p, _, mib = line.partition(",")
        if p.strip() == str(pid):
            mine += int(mib.strip() or 0)
    return {"name": name, "util_pct": int(util), "used_mib": int(used), "total_mib": int(total), "ours_mib": mine}


def service() -> dict:
    v, mic, cfg = LIVE.get("voice"), LIVE.get("mic"), LIVE.get("cfg")
    try:
        g = gpu(os.getpid())
    except Exception as e:
        g = {"error": f"{type(e).__name__}: {e}"[:300]}
    return {"pid": os.getpid(), "uptime_s": round(time.time() - START), "gpu": g,
            "voice": v is not None, "models": LIVE.get("models", []),
            "mic": None if mic is None else {"node": mic.node, "running": mic.running,
                                             "on": getattr(v, "mic_on", None), "automute": getattr(v, "automute", None)},
            "wake_word": LIVE.get("wake"), "speaker_check": LIVE.get("speaker"),
            "voice_state": getattr(v, "state", None), "config": None if cfg is None else
            {k: getattr(cfg, k) for k in ("mic", "stt_device", "tts_voice", "speaker_check", "follow_up")}}


# --- voice turns ------------------------------------------------------------------------------------------------
TURNS = 20           # at least this many rows; all of today's when there are more
MAX_TURNS = 150
# Friction kinds, in the order the page lists them. A turn can have several.
FRICTION = ("reject", "empty_stt", "no_speech", "stt_timeout", "confirm_timeout", "confirm_no", "denied", "jv_rc",
            "tool_error", "stuck", "error", "claude_limit")
ERROR_EVENTS = {"error", "brain_lost", "claude_exit", "claude_off", "mcp_failed"}


def _route(evs: list[dict]) -> str | None:
    r = next((e for e in evs if e.get("ev") == "route"), None)
    return None if r is None else r.get("route")


def _rc_bad(e: dict) -> bool:
    """A real (not dry-run) jv call that failed; rc 3 = 'not found / declined' is a normal answer."""
    return e.get("rc") not in (0, 3, None) and not e.get("dry_run") and "--dry-run" not in (e.get("argv") or [])


def kinds(evs: list[dict]) -> list[str]:
    """What went wrong in a turn, as FRICTION kinds. Barge-in and a follow-up nobody answered are not friction."""
    k, denied = set(), set()
    for e in evs:
        ev = e.get("ev")
        if ev == "reject":
            k.add("empty_stt" if e.get("why") == "empty_stt" else "reject")
        elif ev == "stt_timeout":
            k.add("stt_timeout")
        elif ev == "confirm_heard" and e.get("answer") in ("no", "timeout"):
            k.add("confirm_" + e["answer"])
        elif ev == "permission" and e.get("decision") == "deny":
            denied.add(e.get("tool"))
            k.add({"said-no": "confirm_no", "timeout": "confirm_timeout"}.get(e.get("reason"), "denied"))
        elif ev in ("jv", "run") and _rc_bad(e):
            k.add("jv_rc")
        elif ev == "tool_result" and e.get("is_error") and e.get("name") not in denied:
            k.add("tool_error")
        elif ev == "stuck":
            k.add("stuck")
        elif ev in ERROR_EVENTS or (ev == "result" and e.get("is_error")):
            k.add("error")
        elif ev == "reply" and e.get("why") == "rate limit":
            k.add("claude_limit")
    end = next((e for e in evs if e.get("ev") == "turn_end"), None)
    listen = next((e for e in evs if e.get("ev") == "listen"), {})
    if end and end.get("outcome") == "nothing" and not k and not listen.get("followup") and \
            not any(e.get("ev") in ("heard", "followup") for e in evs):
        k.add("no_speech")
    return [x for x in FRICTION if x in k]


def story(evs: list[dict]) -> dict:
    """One turn as heard → route → actions (with rc) → spoken reply, plus its latencies and cost."""
    first = lambda k: next((e for e in evs if e.get("ev") == k), None)
    heard, route, end, res = first("heard"), first("route"), first("turn_end"), first("result")
    acts, denied, tool = [], set(), None
    for e in evs:
        ev = e.get("ev")
        if ev == "tool_use":
            tool = {"name": e.get("name"), "ok": True, "rc": None, "note": None}
        elif ev == "run" and tool is not None and not e.get("dry_run"):
            tool["rc"] = e.get("rc")
        elif ev == "permission" and e.get("decision") == "deny":
            denied.add(e.get("tool"))
            acts.append({"name": e.get("tool"), "ok": False, "rc": e.get("dry_run_rc"), "note": f"denied: {e.get('reason')}"})
        elif ev == "tool_result" and e.get("name") not in denied:
            t = tool if tool and tool["name"] == e.get("name") else {"name": e.get("name"), "ok": True, "rc": None, "note": None}
            if e.get("is_error"):
                t.update(ok=False, note=e.get("text"))
            elif _rc_bad({**t, "dry_run": False}):
                t["ok"] = False
            acts.append(t)
            tool = None
        elif ev == "jv" and "--dry-run" not in (e.get("argv") or []):    # the brain's own jv: fast path
            acts.append({"name": " ".join((e.get("argv") or [])[1:3]), "ok": not _rc_bad(e), "rc": e.get("rc"),
                         "note": e.get("stderr") if _rc_bad(e) else None})
        elif ev == "confirm_asked":
            acts.append({"name": "asked", "ok": True, "rc": None, "note": e.get("text")})
        elif ev == "confirm_heard":
            acts.append({"name": f"answer {e.get('answer')}", "ok": e.get("answer") == "yes", "rc": None,
                         "note": e.get("text")})
    said = " ".join(e["text"] for e in evs if e.get("ev") == "say" and not e.get("announce") and e.get("text"))
    if not said and (r := first("reply")):
        said = " ".join(r.get("spoken") or []) or r.get("text") or ""
    stt, fa = (end or {}).get("stt_s"), (end or {}).get("first_audio_s")
    parent = next((e.get("parent") for e in evs if e.get("parent")), None)
    return {"heard": heard.get("text") if heard else (route or {}).get("text"), "route": (route or {}).get("route"),
            "model": (route or {}).get("model"), "verb": (route or {}).get("verb"), "args": (route or {}).get("args"),
            "acts": acts, "said": said or None, "parent": parent,
            "asked": any(e.get("expect_reply") for e in evs if e.get("ev") == "say") or
            any(e.get("asked") for e in evs if e.get("ev") == "reply"),
            "outcome": end.get("outcome") if end else None, "stt_s": stt, "first_audio_s": fa,
            "think_s": round(fa - stt, 3) if fa is not None and stt is not None else None,
            "total_s": (end or {}).get("total_s"), "cost": (res or {}).get("cost"), "tokens": (res or {}).get("tokens")}


def _fold_confirms(by: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """A confirm reply (`<HHMMSS-n>c`, with `parent`) belongs to the turn that asked: show it there, not as a row."""
    for tid in [t for t in by if t.endswith("c")]:
        parent = next((e.get("parent") for e in by[tid] if e.get("parent")), None)
        if parent in by:
            by[parent] = sorted(by[parent] + by.pop(tid), key=lambda e: str(e.get("ts", "")))
    return by


def turns() -> dict:
    """Today's turns (at least the last TURNS), newest first, each as a story + friction kinds; plus today's friction
    counts and success rate, and fast-vs-Claude latency medians."""
    from statistics import median

    from heardback.voice import traceview
    by = _fold_confirms(traceview.turns(events()))
    today = date.today().strftime("%Y%m%d")
    ids = list(by)
    n = min(MAX_TURNS, max(TURNS, sum(t.startswith(today) for t in ids)))
    live = live_turn()
    by_note = {}
    recent_notes = notes()
    for note in reversed(recent_notes):
        by_note.setdefault(note["turn"], []).append(note)
    rows, friction, ok = [], {k: [] for k in FRICTION}, 0
    for tid in ids[-n:][::-1]:
        evs = by[tid]
        st, ks = story(evs), kinds(evs)
        rows.append({"id": tid, "ts": evs[0].get("ts"), "bad": bool(ks), "kinds": ks, "live": tid == live,
                     "lines": traceview.render(tid, evs), "route": st["route"], "outcome": st["outcome"],
                     "stt_s": st["stt_s"], "first_audio_s": st["first_audio_s"], "total_s": st["total_s"],
                     "story": st, "events": evs,
                     "notes": sorted([note for t in {tid, *(e.get("turn") for e in evs)} - {None}
                                      for note in by_note.get(t, [])], key=lambda note: note["ts"])})
        if tid.startswith(today) and tid != live:
            for k in ks:
                friction[k].append(tid)
            ok += not ks
    done_today = sum(r["id"].startswith(today) and not r["live"] for r in rows)
    lat = {}
    for r in ("fast", "claude"):
        fa = [x["first_audio_s"] for x in rows if x["route"] == r and x["first_audio_s"] is not None]
        stt = [x["stt_s"] for x in rows if x["route"] == r and x["stt_s"] is not None]
        lat[r] = {"n": len(fa), "first_audio_s": round(median(fa), 2) if fa else None,
                  "stt_s": round(median(stt), 2) if stt else None}
    return {"rows": rows, "latency": lat, "notes": recent_notes[:100],
            "today": {"turns": done_today, "ok": ok, "success_pct": round(100 * ok / done_today) if done_today else None,
                      "friction": friction}}


# --- live voice state -------------------------------------------------------------------------------------------
def live_turn() -> str | None:
    """The turn in flight: the voice daemon's current trace id while it isn't idle."""
    v = LIVE.get("voice")
    return getattr(v, "tid", None) if v is not None and getattr(v, "state", "IDLE") != "IDLE" else None


def live() -> dict:
    """The voice daemon's state right now, read from its attributes (set at each state change; nothing polled)."""
    v = LIVE.get("voice")
    if v is None:
        return {"state": None}
    preview = getattr(v, "dictation_dashboard", None)
    return {"state": str(getattr(v, "state", "")).lower() or None, "since": getattr(v, "state_since", None),
            "turn": live_turn(), "followup": bool(getattr(v, "followup", False)),
            "mic_on": getattr(v, "mic_on", None), "automute": getattr(v, "automute", None),
            "dictation": preview() if callable(preview) else None}
