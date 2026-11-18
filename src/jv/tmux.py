"""tmux verbs: list, select, new, read, type, answer. Every call goes through tmux()."""
import os
import re
import shlex
import subprocess
import threading
import textwrap
import time
import unicodedata
from uuid import uuid4
from pathlib import Path

from heardback.config import load as desktop_config
from heardback.status import store as status_store
from jv.status import load_sessions
from jv.targets import normalize_window_target
from jv.verb import JvError, Verb, fuzzy, _readback

# Flags first, free text last: a tab in a title or path can't shift the flags, and `split("\t", N)` keeps any
# tabs inside the last field (the title, which Claude Code sets).
FIELDS = ["id", "target", "window_index", "active", "in_mode", "alternate", "window_active", "left", "top", "right",
          "bottom", "cmd", "window_name", "cwd", "started", "title"]
FORMAT = "\t".join(["#{pane_id}", "#{session_name}:#{window_index}.#{pane_index}", "#{window_index}",
                    "#{pane_active}", "#{pane_in_mode}", "#{alternate_on}", "#{window_active}",
                    "#{pane_left}", "#{pane_top}", "#{pane_right}", "#{pane_bottom}",
                    "#{pane_current_command}", "#{window_name}", "#{pane_current_path}", "#{@jv_started}", "#{pane_title}"])
RECORD = re.compile(r"%\d+\t")   # every record starts with its pane id
HERE = {"this", "current", "here", "active"}   # the pane the user is looking at
# Real dialog shapes, matched per stripped line on the bottom of the visible screen only: a Claude reply in
# scrollback (or quoting these mid-line) is not a prompt. Permission dialog: "Do you want to proceed?" /
# "❯ 1. Yes" / "Esc to cancel"; the session survey: "1: Bad  2: Fine  3: Good  0: Dismiss".
# A dialog replaces the input box: its lines sit below the last ─ rule. An idle pane's last rule is the input
# box's bottom edge, so a reply quoting these lines (above it) never matches (live 2026-10-31, rc 4).
DIALOG_LINES = [re.compile(r) for r in (r"^Do you want to proceed\?$", r"^❯\s*1\.\s+Yes", r"^Esc to cancel\b",
                                        r"^Enter to confirm · Esc to cancel$")]
SURVEY = re.compile(r"^(?:●\s*)?.*\b1: Bad\b.*\b0: Dismiss\b")   # sits above the input box; takes digit keys
RULE = re.compile(r"^─{20,}$")
# Claude's folder-trust screen: its default is "No, exit", so keys or an answer by voice would quit claude
# (probe 2026-10-31, claude in an untrusted dir). Only the user answers it, on screen.
TRUST = re.compile(r"^(?:❯\s*)?Yes, I trust this folder$")
TRUSTING = "Claude is asking whether to trust this folder; answer it on screen (voice never trusts a folder)"
STARTUP_S = 5   # a claude window jv opened takes keys only after this long and once its session hooked in
SUBMIT_POLLS = 20  # x 50 ms
BOTTOM = 10   # non-blank lines: a dialog's options sit right at the bottom
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_DICTATION_DEADLINE = threading.local()


def tmux(*args: str, input: str | None = None) -> str:
    sock = os.environ.get("JV_TMUX_SOCKET")
    # -u: without a UTF-8 locale (the MCP server env has PATH only) tmux prints the -F tab separators as "_".
    argv = ["tmux", "-u", *(["-L", sock] if sock else []), *args]
    timeout = 10.0
    deadline = getattr(_DICTATION_DEADLINE, "deadline", None)
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            raise JvError(1, "tmux target resolution timed out")
    try:
        r = subprocess.run(argv, input=input, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise JvError(1, "tmux not installed")
    except subprocess.TimeoutExpired:   # its str() holds the argv, i.e. typed text
        raise JvError(1, "tmux timed out")
    if r.returncode:
        err = r.stderr.strip()
        if "no server running" in err or "error connecting" in err:
            raise JvError(1, "tmux not running")
        raise JvError(1, f"tmux: {err}")
    return r.stdout


def session() -> str:
    if name := desktop_config().session:
        return name
    if os.environ.get("TMUX"):
        return tmux("display-message", "-p", "#{session_name}").strip()
    names = tmux("list-sessions", "-F", "#{session_name}").splitlines()
    if not names:
        raise JvError(1, "no tmux sessions")
    return names[0]


def _pane_inventory() -> set[str] | None:
    """Return a complete non-empty pane-ID inventory; failure or ambiguity is unknown."""
    try:
        raw = tmux("list-panes", "-s", "-t", session(), "-F", "#{pane_id}")
    except JvError:
        return None
    ids = raw.splitlines()
    if not ids or any(not re.fullmatch(r"%\d+", pane_id) for pane_id in ids) or len(ids) != len(set(ids)):
        return None
    return set(ids)


def _window_pane_inventory() -> dict[str, set[str]] | None:
    """Complete tmux window-ID → pane-ID inventory; empty or malformed output is unknown."""
    try:
        raw = tmux("list-panes", "-s", "-t", session(), "-F", "#{pane_id}\t#{window_id}")
    except JvError:
        return None
    lines = raw.splitlines()
    if not lines:
        return None
    grouped: dict[str, set[str]] = {}
    seen: set[str] = set()
    for line in lines:
        pane_id, sep, window_id = line.partition("\t")
        if not sep or not re.fullmatch(r"%\d+", pane_id) or not re.fullmatch(r"@\d+", window_id) \
                or pane_id in seen:
            return None
        seen.add(pane_id)
        grouped.setdefault(window_id, set()).add(pane_id)
    return grouped


def _session_for_pane(pane_id: str | None, *, include_ended: bool = False) -> dict | None:
    if not pane_id:
        return None
    sessions = status_store.user_sessions(status_store.list_sessions(resolve=False), include_ended=True)
    matches = [s for s in sessions if s.get("tmux_pane") == pane_id]
    live = [s for s in matches if s.get("state") != "ended"]
    pool = live or (matches if include_ended else [])
    return max(pool, key=lambda s: status_store.epoch(s.get("updated_at")), default=None)


def _stale_ended_claude(pane: dict, sessions: list[dict]) -> bool:
    """Never type to a Claude-looking pane without a current live user session record."""
    if _session(pane, sessions):
        return False
    return pane.get("cmd") == "claude" or pane.get("title", "").startswith("✳")


def _unobserved_claude_message(pane: dict, sessions: list[dict]) -> str:
    ended = any(s.get("tmux_pane") == pane["id"] and s.get("state") == "ended" for s in sessions)
    if ended:
        return f"The Claude session in window {pane['window_index']} has ended; I won't send keys to its old pane"
    return f"I can't confirm a live Claude session in window {pane['window_index']}; no keys sent"


def _guard(p: dict, sessions: list[dict], prompt: bool | None = False) -> None:
    """Refuse a pane that can't take keys now. prompt: False refuses a showing prompt, True needs one, None skips it."""
    if trusting(p):
        raise JvError(4, TRUSTING)
    if starting(p, sessions):
        raise JvError(4, STARTING)
    if _stale_ended_claude(p, sessions):
        raise JvError(4, _unobserved_claude_message(p, sessions))
    if prompt is not None and showing_prompt(p, sessions) != prompt:
        raise JvError(4, "no prompt showing" if prompt else "pane is showing a prompt; use tmux answer")


def parse_panes(raw: str) -> list[dict]:
    """`list-panes -F FORMAT` output → pane dicts. Split on "\n" only (str.splitlines also splits on \x1c-\x1e,
    \x85, \u2028 …, which can sit in a title); a line that doesn't start a record continues the previous title
    (a newline inside it); a record that is still short is skipped and traced, never raised."""
    recs: list[str] = []
    for line in raw.rstrip("\n").split("\n"):
        if RECORD.match(line) or not recs:
            recs.append(line)
        else:
            recs[-1] += " " + line
    out = []
    for rec in recs:
        if not rec:
            continue
        vals = rec.split("\t", len(FIELDS) - 1)
        if len(vals) != len(FIELDS):
            from jv import trace
            trace.emit("jv", "tmux_bad_line", os.environ.get("HEARDBACK_TURN") or None, line=rec)
            continue
        p = dict(zip(FIELDS, vals))
        for k in ("active", "in_mode", "alternate", "window_active"):
            p[k] = p[k] == "1"
        for k in ("left", "top", "right", "bottom"):
            p[k] = int(p[k]) if p[k].isdigit() else 0
        out.append(p)
    return out


def panes() -> list[dict]:
    return parse_panes(tmux("list-panes", "-s", "-t", session(), "-F", FORMAT))


def _session(pane: dict, sessions: list[dict]) -> dict | None:
    return next((s for s in sessions if s.get("tmux_pane") == pane["id"] and s.get("state") != "ended"), None)


def is_claude(pane: dict, sessions: list[dict]) -> bool:
    return bool(_session(pane, sessions)) or pane["cmd"] == "claude" or pane["title"].startswith("✳")


def _windows(ps: list[dict]) -> str:
    seen = {p["window_index"]: p["window_name"] for p in ps}
    return ", ".join(f"{i} {n}" for i, n in seen.items())


def _spoken(t: str, ps: list[dict]) -> str:
    """Normalize spoken window labels, number words, and positional ordinals."""
    names = list(dict.fromkeys(p["window_name"] for p in ps))
    wins = list(dict.fromkeys(p["window_index"] for p in ps))
    return normalize_window_target(t, window_indices=wins, window_names=names)


PANE = re.compile(r"^(?:the )?(?:(this|current|active)|(left|right|top|bottom|upper|lower)) pane(?: (?:of|in|on) (.+))?$",
                  re.I)
EDGE = {"left": ("left", min, "right"), "right": ("right", max, "left"), "top": ("top", min, "bottom"),
        "upper": ("top", min, "bottom"), "bottom": ("bottom", max, "top"), "lower": ("bottom", max, "top")}


def _pane(m: re.Match) -> dict:
    """'left pane of window 2' / 'this pane' → the pane on that edge of the window (default the one on screen)."""
    p = resolve(m[3] or "current")
    if m[1]:
        return p
    k, pick, far = EDGE[m[2].lower()]
    win = [q for q in panes() if q["window_index"] == p["window_index"]]
    edge = [q for q in win if q[k] == pick(q[k] for q in win)]
    other = {"left": min, "right": max, "top": min, "bottom": max}[far]
    # a full-height left pane is on the bottom edge too, but "bottom pane" means the one below something
    edge = [q for q in edge if q[far] != other(q[far] for q in win)] or edge
    # ponytail: still two (left | top/bottom right, "right pane") → the active one, else the top-left
    return next((q for q in edge if q["active"]), min(edge, key=lambda q: (q["top"], q["left"])))


def resolve(target: str) -> dict:
    if m := PANE.match(target.strip()):
        return _pane(m)
    ps = panes()
    raw = target.strip()
    t = _spoken(raw, ps)
    is_window_name = any(raw.casefold() == p["window_name"].casefold() for p in ps)
    if t.lower() in HERE and not is_window_name:
        hit = [p for p in ps if p["window_active"] and p["active"]]
    elif t.isdigit():
        hit = [p for p in ps if p["window_index"] == t and p["active"]]
    elif re.fullmatch(r"[^:\r\n]+:\d+\.\d+", t):
        hit = [p for p in ps if p["target"] == t]
    elif re.fullmatch(r"[^:\r\n]+:\d+", t):
        hit = [p for p in ps if p["target"].rsplit(".", 1)[0] == t and p["active"]]
    elif t.startswith("%"):
        hit = [p for p in ps if p["id"] == t]
    else:
        hit = []
        if t.lower() == "claude":
            sessions = load_sessions()
            cl = [p for p in ps if is_claude(p, sessions)]
            if len(cl) == 1:
                return cl[0]
            if cl:
                raise JvError(2, f"which claude: {', '.join(p['target'] for p in cl)}?",
                              candidates=[p["target"] for p in cl])
            # no Claude pane: fall through to window names
        try:
            name = fuzzy(t, list(dict.fromkeys(p["window_name"] for p in ps)))
        except JvError as e:
            if e.code != 1:
                raise
            raise JvError(1, f"no window {t}; windows: {_windows(ps)}", **e.extra)
        wins = list(dict.fromkeys(p["window_index"] for p in ps if p["window_name"] == name))
        if len(wins) > 1:
            raise JvError(2, f"which {name} window: {', '.join(wins)}?", candidates=wins)
        hit = [p for p in ps if p["window_index"] == wins[0] and p["active"]]
    if not hit:
        raise JvError(1, f"no window {t}; windows: {_windows(ps)}")
    return hit[0]


def window_choices() -> list[str]:
    ps = panes()
    return list(dict.fromkeys([p["window_index"] for p in ps] + [p["window_name"] for p in ps]))


def _list(a: dict) -> dict:
    sessions, ps = load_sessions(), panes()
    rows = [{"target": p["target"], "window": int(p["window_index"]), "name": p["window_name"], "cmd": p["cmd"],
             "cwd": p["cwd"], "claude": is_claude(p, sessions),
             "state": (_session(p, sessions) or {}).get("state"),
             **({"active": True} if p["window_active"] and p["active"] else {})} for p in ps]
    n = len({p["window_index"] for p in ps})
    on = next((p["window_index"] for p in ps if p["window_active"]), None)
    return {"panes": rows, "say": f"{n} window{'s' * (n != 1)}" + (f", you're on {on}" if on else "")}


def _select(a: dict) -> dict:
    p = resolve(a["window"])
    tmux("select-window", "-t", f"{session()}:{p['window_index']}")
    res = {"target": p["target"], "say": f"window {p['window_index']}"}
    if not a.get("no_focus"):
        try:
            focused = _focus_kitty()
        except Exception:   # the tmux part is done; a Hyprland hiccup never fails select
            focused = False
        if not focused:
            res["focused"] = False
    try:
        res["observed"] = any(q["window_index"] == p["window_index"] and q["window_active"] for q in panes())
    except Exception:
        res["observed"] = False
    return res


def _ppid(pid: int) -> int | None:
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return None


def _focus_kitty() -> bool:
    from jv import hypr
    try:
        pids = [int(x) for x in tmux("list-clients", "-t", session(), "-F", "#{client_pid}").split() if x.isdigit()]
        if not pids:
            return False
        kitty = {c["pid"]: c["address"] for c in hypr.clients() if c.get("class") == "kitty"}
        for pid in pids:
            while pid and pid > 1:
                if pid in kitty:
                    hypr.focus_window(kitty[pid])
                    return True
                pid = _ppid(pid)
    except (JvError, OSError, KeyError):
        return False
    return False


def _dir(d: str) -> Path:
    if "/" in d or d.startswith("~"):
        p = Path(d).expanduser().resolve()
    else:
        kids = {}
        for root in desktop_config().project_roots:
            folder = Path(root).expanduser()
            try:
                children = [child for child in folder.iterdir() if child.is_dir()]
            except OSError:
                continue
            for child in children:
                kids.setdefault(child.name.casefold(), []).append(child)
        # A case-insensitive spoken alias is safe only when it identifies one real directory.
        matches = kids.get(d.casefold(), [])
        if not matches:
            try:
                name = fuzzy(d, sorted(kids))
            except JvError as e:
                if e.code == 2:
                    candidates = sorted(str(p) for key in e.extra.get("candidates", []) for p in kids.get(key, []))
                    raise JvError(2, f"which folder matches {d}: {', '.join(candidates)}?", candidates=candidates) from None
                closest = sorted({p.name for key in e.extra.get("closest", []) for p in kids.get(key, [])})
                raise JvError(1, f"no folder matches {d}", closest=closest) from None
            matches = kids[name]
        paths = list(dict.fromkeys(matches))
        if len(paths) > 1:
            candidates = sorted(str(path) for path in paths)
            raise JvError(2, f"{d} matches multiple folders: {', '.join(candidates)}", candidates=candidates)
        p = paths[0].resolve()
    if not p.is_dir() or not p.is_relative_to(Path.home().resolve()):
        raise JvError(2, f"{d} is not a folder under your home")
    return p


def _new_dir(a: dict) -> Path:
    """--dir as given; else the configured Claude home (when it exists), else home."""
    if a.get("dir"):
        return _dir(a["dir"])
    home = desktop_config().claude_home
    if a.get("run") == "claude" and Path(home).expanduser().is_dir():
        return _dir(home)
    return _dir("~")


def claude_dirs() -> list[Path]:
    """Folders (and everything under them) a voice-opened claude may start in: `tmux.claude_dirs` in config.toml."""
    return [Path(x).expanduser().resolve() for x in desktop_config().claude_dirs]


def _new_check(a: dict) -> str | None:
    _new_attachment(a)
    return _claude_dir_check(_new_dir(a), a) if a.get("run") == "claude" else None


def _new_attachment(a: dict) -> tuple[str, str] | None:
    kind = a.get("attach")
    if a.get("attach_target") is not None and kind != "pane":
        raise JvError(2, "attach_target needs --attach pane")
    if not kind:
        if a.get("source_revision"):
            raise JvError(2, "source_revision needs --attach")
        return None
    if a.get("run") != "claude":
        raise JvError(2, "--attach needs --run claude")
    if kind == "pane" and not a.get("attach_target"):
        raise JvError(2, "--attach pane needs --attach-target")
    instruction = a.get("prompt") or "Look at this."
    if instruction.lstrip().startswith(("!", "/")):
        raise JvError(4, "an attachment instruction cannot start with ! or /")
    if CONTROL.search(instruction):
        raise JvError(2, "prompt has control characters")
    from jv import attach
    content, revision, label = attach.read(kind, a.get("attach_target"), None)
    expected = a.get("source_revision") or a.get("_source_revision")
    if expected and expected != revision:
        message = f"the {kind} changed after read-back; ask again"
        raise JvError(4, message, say=message)
    a.update(_source_revision=revision, _attach_label=label)
    return content, label


def _claude_dir_check(d: Path, a: dict) -> str | None:
    outside = not any(d.is_relative_to(r) for r in claude_dirs())
    home = Path.home().resolve()
    where = "your home folder" if d == home else "your claude folder" if d == home / "claude" else d.name
    note = " It's outside your work folders." if outside else ""
    if a.get("attach") and (_tainted() or outside):
        return _readback(f"Open Claude in {where} and send '{a.get('prompt') or 'Look at this.'}' "
                         f"with {a['_attach_label']}?{note}")
    if a.get("prompt") and _tainted():   # a first prompt is submitted on start: Enter into a Claude pane by another name
        return _readback(f"Open Claude in {where} and send '{a['prompt']}'?{note}")
    return f"Open Claude in {where}?{note}" if outside else None


def _run_cmd(a: dict) -> list[str]:
    """--run claude [--prompt]: the shell command for a new window or pane; [] = the default shell."""
    claude, prompt = a.get("run") == "claude", a.get("prompt")
    if prompt and not claude:
        raise JvError(2, "--prompt needs --run claude")
    if prompt and CONTROL.search(prompt):
        raise JvError(2, "prompt has control characters")
    if a.get("run") == "nvim":
        return ['nvim; exec "${SHELL:-/bin/sh}"']
    # The first prompt rides on claude's argv: keys typed into a starting claude hit its startup/trust screen.
    # Then the user's shell, so the window (and any error) outlives claude.
    line = shlex.join(["claude", *(["--add-dir", a['_attach_inbox']] if a.get('_attach_inbox') else []),
                       *(["--", prompt] if prompt else [])])
    return [f'{line}; exec "${{SHELL:-/bin/sh}}"'] if claude else []


def _started(target: str, a: dict) -> None:
    if a.get("run") == "claude":
        tmux("set-option", "-p", "-t", target, "@jv_started", str(time.time()))


def _arm_watch(pane_id: str, requested_at: str) -> str:
    if not os.environ.get('HEARDBACK_TURN'):
        return ''
    from heardback.status import watch
    # shortcut: queued sends use the next Stop, bind to a specific Claude turn if live checks require it.
    return '' if watch.arm(pane_id, now=status_store.epoch(requested_at)) else '; no watch was armed for completion'


def _new(a: dict) -> dict:
    d = _new_dir(a)
    name = a.get("name") or d.name
    source = _new_attachment(a)
    command_args = a
    path = None
    if source:
        from jv import attach
        content, label = source
        instruction = a.get("prompt") or "Look at this."
        if a['attach'] == "tab":
            command_args = {**a, "prompt": attach.compose(instruction, "tab", content, label)}
        else:
            path = attach.write_screenshot(content) if a['attach'] == "screenshot" else attach.write(a['attach'], content)
            command_args = {**a, "_attach_inbox": str(path.parent),
                            "prompt": f"{instruction} (from heardback: {label}, saved at {path}; read it first)"}
    cmd = _run_cmd(command_args)
    requested_at = status_store.now_iso()
    try:
        created = tmux("new-window", "-P", "-F", "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}",
                      "-t", f"{session()}:", "-c", str(d).replace("#", "##"), "-n", name.replace("#", "##"),
                      *cmd).strip()
    except JvError as e:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                raise JvError(e.code, f"{e.msg}; could not remove the private attachment file", **e.extra) from None
        raise
    pane_id, sep, target = created.partition("\t")
    if not sep or not re.fullmatch(r"%\d+", pane_id) or not re.fullmatch(r"[^:\r\n]+:\d+\.\d+", target):
        return {"target": None, "observed": False, "say": f"asked tmux to open {name}; the returned pane identity is malformed, so its status is unknown"}
    _started(pane_id, a)
    say = f"opened {name}"
    if a.get("run") == "claude":
        _focus_kitty()
        watch_note = _arm_watch(pane_id, requested_at) if command_args.get("prompt") else ""
        readiness = _readiness(pane_id)
        where = f"window {target.split(':', 1)[-1].split('.', 1)[0]}"
        say += (f"; Claude's readiness isn't confirmed in {where}" if readiness == "pending"
                else f"; Claude is {readiness} in {where}") + watch_note
    try:
        observed = any(q["id"] == pane_id for q in panes())
    except Exception:
        observed = False
    return {"target": target, "say": say, "observed": observed}

SPLIT = {"horizontal": "-h", "vertical": "-v"}   # horizontal = side by side (tmux -h), vertical = stacked


def _split_check(a: dict) -> str | None:
    """Same safety as tmux.new: claude in a pane outside claude_dirs reads back."""
    if a.get("run") != "claude":
        return None
    return _claude_dir_check(Path(resolve(a.get("window") or "current")["cwd"]).expanduser().resolve(), a)


def _split(a: dict) -> dict:
    """A new pane next to the window's active pane, in that pane's folder; it takes the focus, like a key split."""
    p = resolve(a.get("window") or "current")
    cmd = _run_cmd(a)
    requested_at = status_store.now_iso()
    new = tmux("split-window", "-P", "-F", "#{pane_id}", SPLIT[a["dir"]], "-t", p["id"],
               "-c", p["cwd"].replace("#", "##"), *cmd).strip()
    if not re.fullmatch(r"%\d+", new):
        return {"target": None, "observed": False, "say": f"split window {p['window_index']}; the returned pane identity is malformed, so its status is unknown"}
    _started(new, a)
    say = f"split window {p['window_index']}"
    if a.get("run") == "claude":
        watch_note = _arm_watch(new, requested_at) if a.get("prompt") else ""
        readiness = _readiness(new)
        say += ("; Claude's readiness isn't confirmed in the new pane" if readiness == "pending"
                else f"; Claude is {readiness} in the new pane") + watch_note
    try:
        observed = any(q["id"] == new for q in panes())
    except Exception:
        observed = False
    return {"target": new, "say": say, "observed": observed}

def starting(pane: dict, sessions: list[dict]) -> bool:
    """A claude window jv opened that can't take keys yet: under STARTUP_S old, or no session hooked in."""
    session = _session(pane, sessions)
    t = pane.get("started")
    if t:
        try:
            if time.time() - float(t) < STARTUP_S:
                return True
        except (TypeError, ValueError):
            return True
    if session and session.get("started_at"):
        if time.time() - status_store.epoch(session["started_at"]) < STARTUP_S:
            return True
    return pane["cmd"] == "claude" and not session


def _readiness(pane_id: str) -> str:
    """Readiness needs a current hook session, startup age, and no trust or permission dialog."""
    try:
        pane = resolve(pane_id)
        if pane["id"] != pane_id:
            return "pending"
        sessions = load_sessions()
    except Exception:
        return "pending"
    session = _session(pane, sessions)
    if not session or starting(pane, sessions):
        return "starting"
    try:
        if trusting(pane):
            return "waiting for folder trust on screen"
        if showing_prompt(pane, sessions):
            return "waiting for input"
    except Exception:
        return "pending"
    if session.get("state") == "idle":
        return "ready"
    if session.get("state") == "working":
        return "working"
    return "pending"


STARTING = "Claude is still starting in that window; put the first request in tmux new's prompt, or try again in a few seconds"


def _read(a: dict) -> dict:
    p = resolve(a["target"])
    raw = tmux("capture-pane", "-p", "-J", "-t", p["id"], "-S", f"-{a.get('lines', 40)}")
    lines = ANSI.sub("", raw).splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return {"text": "\n".join(lines), "say": next((l.strip() for l in reversed(lines) if l.strip()), "")}


def screen(pane_id: str, lines: int = 15) -> str:
    return tmux("capture-pane", "-p", "-t", pane_id, "-S", f"-{lines}")


def prompt_draft(pane_id: str) -> bool | None:
    """Read a bounded Claude input box, preserving SGR so dim/grey placeholders are empty."""
    raw = tmux('capture-pane', '-p', '-e', '-t', pane_id)
    raw = re.sub(r'\x1b\].*?(?:\x1b\\|\x07)', '', raw, flags=re.S)
    plain, typed = '', ''
    dim, grey = False, False
    for part in re.split(r'(\x1b\[[0-9;]*m)', raw):
        if re.fullmatch(r'\x1b\[[0-9;]*m', part):
            codes = [int(c or '0') for c in part[2:-1].split(';')]
            i = 0
            while i < len(codes):
                code = codes[i]
                if code == 0:
                    dim = grey = False
                elif code in (2, 22):
                    dim = code == 2
                elif code in (30, 31, 32, 33, 34, 35, 36, 37, 39, 90, 91, 92, 93, 94, 95, 96, 97):
                    grey = code == 90
                elif code == 38:
                    if codes[i + 1:i + 2] == [5] and len(codes) > i + 2:
                        grey = codes[i + 2] == 8 or 232 <= codes[i + 2] < 255
                        i += 2
                    elif codes[i + 1:i + 2] == [2] and len(codes) > i + 4:
                        grey = len(set(codes[i + 2:i + 5])) == 1 and 0 < codes[i + 2] < 255
                        i += 4
                    else:
                        return None
                elif code == 48:  # background colour's parameters are not foreground SGR codes
                    size = 2 if codes[i + 1:i + 2] == [5] else 4 if codes[i + 1:i + 2] == [2] else 0
                    if not size or len(codes) <= i + size:
                        return None
                    i += size
                i += 1
        else:
            if any(ord(c) < 32 and c not in '\n\t' for c in part):
                return None
            plain += part
            typed += ''.join(c if not (dim or grey) or c == '\n' else ' ' for c in part)
    lines, visible = plain.splitlines(), typed.splitlines()
    rules = [i for i, line in enumerate(lines) if RULE.fullmatch(line.strip())]
    if len(rules) < 2:
        return None
    start, end = rules[-2:]
    if end <= start + 1 or not re.match(r'^\s*❯(?:\s|$)', lines[start + 1]):
        return None
    offset = lines[start + 1].index('❯') + 1
    draft = visible[start + 1][offset:] + '\n' + '\n'.join(visible[start + 2:end])
    greyed = lines[start + 1][offset:] + '\n' + '\n'.join(lines[start + 2:end])
    return bool(draft.strip()) or re.fullmatch(r'\s*(?:Try ".*")?\s*', greyed) is None


def _status_ready(pane_id: str, session_id: str) -> dict:
    """Fixed status submits require the same idle session and a clearly empty prompt."""
    p = resolve(pane_id)
    sessions = load_sessions(include_ended=True)
    s = _session_for_pane(pane_id)
    line = None
    if p['id'] != pane_id or not s or s['session_id'] != session_id:
        line = f"Window {p['window_index']}'s Claude session changed; I didn't ask."
    elif pane_id == os.environ.get('TMUX_PANE'):
        line = "I won't ask for status in the pane I'm running from."
    elif s.get('state') != 'idle' and busy(s):
        line = f"Window {p['window_index']} is busy; I didn't ask."
    elif trusting(p):
        line = TRUSTING
    elif starting(p, sessions):
        line = STARTING
    elif _stale_ended_claude(p, sessions):
        line = _unobserved_claude_message(p, sessions)
    elif showing_prompt(p, sessions) or p['in_mode']:
        line = 'Pane is showing a prompt or tmux mode; I did not type into it.'
    else:
        draft = prompt_draft(pane_id)
        if draft is None:
            line = f"I can't see window {p['window_index']}'s prompt box clearly, so I didn't type into it."
        elif draft:
            line = f"Window {p['window_index']} has something typed in its prompt; I didn't ask."
    if line:
        raise JvError(4, line, say=line)
    return p


def _status_submit(pane_id: str, session_id: str, text: str) -> None:
    from heardback.status.watch import STATUS_LINE
    if text not in (STATUS_LINE, 'status'):
        raise JvError(2, 'invalid fixed status request')
    _status_ready(pane_id, session_id)
    # One burst reads as a paste to Claude Code and the Enter lands as a newline (live 10:12:02, window 3).
    tmux('send-keys', '-t', pane_id, '-l', '--', text)
    time.sleep(0.15)
    tmux('send-keys', '-t', pane_id, 'Enter')


def bottom(pane_id: str, n: int = BOTTOM) -> list[str]:
    """Last n non-blank lines of the visible screen (no history), stripped."""
    return [l.strip() for l in tmux("capture-pane", "-p", "-t", pane_id).splitlines() if l.strip()][-n:]


# A running turn always draws its spinner above the box: "✻ Wandering… (1m 18s · ↓ 4.4k tokens · …)";
# a finished one shows "✻ Worked for 33s · done 9:58 AM".
SPINNER = re.compile(r"^\S\s+[^…()]{1,40}…\s*\(")


def turn_running(pane_id: str) -> bool:
    """An interrupted turn fires no Stop hook, so a session can read 'working' forever (live 10:23, window 2).
    Two captures half a second apart; a failed capture counts as running."""
    for i in range(2):
        if i:
            time.sleep(0.5)
        try:
            if any(SPINNER.match(l) for l in bottom(pane_id, 15)):
                return True
        except JvError:
            return True
    return False


def busy(s: dict) -> bool:
    """Hook state, except that 'working' needs the pane's spinner to agree."""
    return s.get('state') == 'waiting' or s.get('state') == 'working' and turn_running(s['tmux_pane'])


def showing_prompt(pane: dict, sessions: list[dict]) -> bool:
    s = _session(pane, sessions)
    if s and s.get("state") == "waiting":
        return True
    tail = bottom(pane["id"])
    box = tail[max((i + 1 for i, l in enumerate(tail) if RULE.match(l)), default=0):]
    # ponytail: a reply quoting the survey line itself still matches; anchor it to the input box if that happens
    return any(rx.search(l) for l in box for rx in DIALOG_LINES) or any(SURVEY.search(l) for l in tail)


def trusting(pane: dict) -> bool:
    return any(TRUST.match(l) for l in bottom(pane["id"]))


OPTION = re.compile(r"^(?:❯\s*)?(\d)\.\s+(.+?)\s*$")
OPT_CUT = re.compile(r"\s(?:for|to|from|in|what|·)\s")
SUBTLE = re.compile(r"^(?:Tip:|[╌─]{5,}|⎿)")


def _short_option(text: str) -> str:
    """'Yes, and don't ask again for uv run …' → 'yes and don't ask again'."""
    t = OPT_CUT.split(text.replace("(esc)", "").replace(",", ""))[0]
    return " ".join(t.lower().split()[:6])


def dialog(pane_id: str) -> dict | None:
    """The open dialog at the bottom of the pane: {kind, question, options: [(n, text)]}, or None.
    kind: permission | trust (never answered by voice) | survey. Same box rule as showing_prompt."""
    tail = bottom(pane_id, 40)
    if any(TRUST.match(l) for l in tail[-BOTTOM:]):
        return {"kind": "trust", "question": "trust this folder?", "options": []}
    box = tail[max((i + 1 for i, l in enumerate(tail) if RULE.match(l)), default=0):]
    if any(rx.search(l) for l in box[-BOTTOM:] for rx in DIALOG_LINES):
        opts = [(int(m[1]), m[2]) for m in map(OPTION.match, box) if m]
        first = next((i for i, l in enumerate(box) if OPTION.match(l)), len(box))
        head = [l for l in box[:first] if not SUBTLE.match(l)]
        q = next((l for l in reversed(head) if l.endswith("?")), "")
        if q in ("", "Do you want to proceed?") and len(head) > 1:   # generic: say the title + its subject
            q = f"{head[0]}: {textwrap.shorten(head[1], 60, placeholder='…')}?"
        return {"kind": "permission", "question": q[:1].lower() + q[1:], "options": opts}
    if s := next((l for l in tail if SURVEY.search(l)), None):
        return {"kind": "survey", "question": "how is Claude doing?",
                "options": [(int(n), t) for n, t in re.findall(r"(\d): (\w+)", s)]}
    return None


def ask_line(d: dict) -> str:
    """'edit main.py? 1 yes, 2 yes and don't ask again, 3 no'."""
    return ", ".join([d["question"]] + [f"{n} {_short_option(t)}" for n, t in d["options"]]).replace("?,", "?", 1)


def _prompt(a: dict) -> dict:
    p = resolve(a["target"])
    w, d = p["window_index"], dialog(p["id"])
    if not d:
        say = f"Window {w} isn't asking anything."
    elif d["kind"] == "trust":
        say = f"Window {w} asks whether to trust its folder; answer that on screen."
    else:
        say = f"Window {w} asks: {ask_line(d)}"
    return {"target": p["target"], "dialog": d, "say": say}


def option_text(pane_id: str, n: int) -> str | None:
    rx = re.compile(rf"^\s*(?:❯\s*)?{n}\.\s+(.+?)\s*$")
    hits = [m.group(1) for m in map(rx.match, screen(pane_id).splitlines()) if m]
    return hits[-1] if hits else None


CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")   # C0 except \t \n, plus DEL


def _text(a: dict, destination: dict) -> str:
    """Read paste/attachment sources locally and bind preflight to dispatch without exposing their text."""
    src = [s for s in ("clipboard", "selection") if a.get(s)]
    kind = a.get("attach")
    if a.get("attach_target") is not None and kind != "pane":
        raise JvError(2, "attach_target needs --attach pane")
    if kind == "pane" and not a.get("attach_target"):
        raise JvError(2, "--attach pane needs --attach-target")
    if kind:
        if src:
            raise JvError(2, "--attach cannot be combined with --clipboard or --selection")
        instruction = a.get("text") or "Look at this."
        if instruction.lstrip().startswith(("!", "/")):
            raise JvError(4, "an attachment instruction cannot start with ! or /")
        from jv import attach
        content, revision, label = attach.read(kind, a.get("attach_target"), destination["id"])
        expected = a.get("source_revision") or a.get("_source_revision")
        if expected and expected != revision:
            message = f"the {kind} changed after read-back; ask again"
            raise JvError(4, message, say=message)
        a.update(_source_revision=revision, _attach_label=label, enter=True)
        text = attach.compose(instruction, kind, content, label)
        if CONTROL.search(text):
            raise JvError(2, "text has control characters")
        return text
    if len(src) > 1 or src and a.get("text"):
        raise JvError(2, "give text, --clipboard or --selection, not two")
    if a.get("source_revision") and not src:
        raise JvError(2, "source_revision needs --clipboard or --selection")
    if src:
        from jv import ctx
        raw, revision = ctx.read(src[0])
        expected = a.get("source_revision") or a.get("_source_revision")
        if expected and expected != revision:
            message = f"the {src[0]} changed after read-back; ask again before pasting"
            raise JvError(4, message, say=message)
        a["_source_revision"] = revision
        text = raw.replace("\r\n", "\n").rstrip("\n")
    elif (text := a.get("text")) is None:
        raise JvError(2, "missing argument: text")
    if CONTROL.search(text):
        raise JvError(2, "text has control characters")
    return text


# "type ls and press enter" / "… send it": the spoken ending means Enter, not text (friction2 #39)
ENTER_TAIL = re.compile(r"[\s,]+(?:(?:and )?(?:press|hit) enter|enter|(?:and )?send it|and submit)[.!]?$", re.I)


ENDS_SESSION = {"exit", "quit", "/exit", "/quit", "/clear"}   # Enter on these ends or wipes a Claude session
TAINT_ENV = "HEARDBACK_TAINTED"   # set only by the brain's dry-run, once its conversation has taken in outside text


def _tainted() -> bool:
    return os.environ.get(TAINT_ENV) == "1"


def _submit_check(text: str, w: str, a: dict) -> str:
    """The injection gate's read-back for Enter into a Claude pane, built here so the model never words it."""
    if a.get("attach"):
        return _readback(f"Send '{a.get('text') or 'Look at this.'}' with {a['_attach_label']} "
                         f"to the Claude session in window {w}?")
    if source := next((s for s in ("clipboard", "selection") if a.get(s)), None):
        # Never quoted: copied text can be a password, and read-backs are spoken aloud and logged.
        return f"Send the {source} text, {len(text)} characters, to the Claude session in window {w}?"
    if text.lstrip().startswith("!"):
        return _readback(f"Run shell command '{text.strip()}' in the Claude session in window {w}?")
    if not text.strip():
        return f"Press enter in the Claude session in window {w}? Whatever is in its prompt gets submitted."
    return _readback(f"Send '{text}' to the Claude session in window {w}?")


def _dictated(a: dict) -> dict:
    if (t := a.get("text")) and (m := ENTER_TAIL.search(t)):
        return {**a, "text": t[:m.start()], "enter": True}
    return a


DICTATION_SERVER_FORMAT = "#{pid}\t#{start_time}\t#{socket_path}"
DICTATION_PANE_FORMAT = "#{pane_id}\t#{pane_pid}\t#{session_id}\t#{@jv_started}\t#{alternate_on}\t#{pane_in_mode}"
DICTATION_RESOLUTION_S = 3.0
DICTATION_INSERT_S = 8.0
DICTATION_CLEANUP_S = 1.0


def _dictation_server() -> tuple[str, str, str]:
    fields = tmux("display-message", "-p", DICTATION_SERVER_FORMAT).strip().split("\t")
    if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit() or not fields[2]:
        raise JvError(4, "can't confirm tmux server identity")
    return fields[0], fields[1], fields[2]


def _dictation_pane(pane_id: str) -> dict:
    raw = tmux("display-message", "-p", "-t", pane_id, DICTATION_PANE_FORMAT).rstrip("\n")
    fields = raw.split("\t")
    if len(fields) != 6 or fields[0] != pane_id or not fields[1].isdigit() or \
            not re.fullmatch(r"\$\d+", fields[2]):
        raise JvError(4, "selected tmux pane is unavailable")
    return {"id": fields[0], "pid": fields[1], "session_id": fields[2], "started": fields[3],
            "alternate": fields[4] == "1", "in_mode": fields[5] == "1"}


def _dictation_guard(pane: dict, sessions: list[dict]) -> None:
    _guard(pane, sessions)
    if pane["in_mode"]:
        raise JvError(4, "pane is in a tmux mode; exit it on screen first")


def dictation_target(selector: str) -> dict:
    """Resolve within one small deadline and pin server identity before selector lookup."""
    previous = getattr(_DICTATION_DEADLINE, "deadline", None)
    deadline = time.monotonic() + DICTATION_RESOLUTION_S
    _DICTATION_DEADLINE.deadline = min(previous, deadline) if previous is not None else deadline
    try:
        server = _dictation_server()
        pane = resolve(selector)
        sessions = load_sessions(include_ended=True)
        _dictation_guard(pane, sessions)
        state = _dictation_pane(pane["id"])
        if state["started"] != str(pane.get("started") or ""):
            raise JvError(4, "selected tmux pane changed")
        if _dictation_server() != server:
            raise JvError(4, "tmux server changed while selecting the pane")
        return {"target": pane["target"], "pane_id": pane["id"], "pane_pid": state["pid"],
                "session_id": state["session_id"], "started": state["started"], "server": server,
                "requires_commit": bool(state["alternate"] and not is_claude(pane, sessions))}
    finally:
        if previous is None:
            del _DICTATION_DEADLINE.deadline
        else:
            _DICTATION_DEADLINE.deadline = previous


def _dictation_revalidate(pin: dict, *, confirmed: bool) -> dict:
    pane_id = pin.get("pane_id")
    if not isinstance(pane_id, str) or not re.fullmatch(r"%\d+", pane_id):
        raise JvError(4, "selected tmux pane identity is invalid")
    server = _dictation_server()
    if server != tuple(pin.get("server", ())):
        raise JvError(4, "selected tmux server changed")
    pane = next((p for p in panes() if p["id"] == pane_id), None)
    if pane is None:
        raise JvError(4, "selected tmux pane disappeared")
    state = _dictation_pane(pane_id)
    if state["pid"] != pin.get("pane_pid") or state["session_id"] != pin.get("session_id") or \
            state["started"] != pin.get("started") or pane.get("started", "") != pin.get("started"):
        raise JvError(4, "selected tmux pane was replaced")
    _dictation_guard({**pane, "alternate": state["alternate"], "in_mode": state["in_mode"]},
                     load_sessions(include_ended=True))
    if _dictation_server() != server:
        raise JvError(4, "tmux server changed while checking the pane")
    claude = is_claude(pane, load_sessions(include_ended=True))
    if state["alternate"] and not claude and not confirmed:
        return {"status": "confirmation_required", "accepted": False, "observed": False,
                "pane_id": pane_id}
    return {"status": "ready", "accepted": False, "observed": False, "pane_id": pane_id}


def dictation_insert(pin: dict, text: str, *, confirmed: bool, claim) -> dict:
    """Insert literal single-line text once at a pinned pane cursor; never send Enter or focus keys."""
    if not isinstance(text, str) or not text or len(text) > 4000:
        raise JvError(2, "dictation text is empty or over its limit")
    if any(unicodedata.category(ch) == "Cc" or ch in "\u2028\u2029" for ch in text):
        raise JvError(2, "dictation text contains a line break or control character")
    previous = getattr(_DICTATION_DEADLINE, "deadline", None)
    _DICTATION_DEADLINE.deadline = min(previous, time.monotonic() + DICTATION_INSERT_S) \
        if previous is not None else time.monotonic() + DICTATION_INSERT_S
    buffer = "jvdict-" + uuid4().hex
    staging_attempted = False
    try:
        first = _dictation_revalidate(pin, confirmed=confirmed)
        if first["status"] == "confirmation_required":
            return first
        try:
            staging_attempted = True
            tmux("load-buffer", "-b", buffer, "-", input=text)
        except JvError:
            raise JvError(4, "could not stage the dictation text") from None
        # Recheck after staging and immediately before the one authorized paste dispatch.
        last = _dictation_revalidate(pin, confirmed=confirmed)
        if last["status"] == "confirmation_required":
            return last
        if not claim():
            return {"status": "cancelled", "accepted": False, "observed": False,
                    "pane_id": pin["pane_id"]}
        try:
            tmux("paste-buffer", "-b", buffer, "-d", "-p", "-t", pin["pane_id"])
        except JvError:
            return {"status": "uncertain", "accepted": None, "observed": False,
                    "pane_id": pin["pane_id"]}
        return {"status": "accepted", "accepted": True, "observed": False,
                "pane_id": pin["pane_id"], "chars": len(text)}
    finally:
        if staging_attempted:
            cleanup_deadline = time.monotonic() + DICTATION_CLEANUP_S
            _DICTATION_DEADLINE.deadline = min(previous, cleanup_deadline) \
                if previous is not None else cleanup_deadline
            try:
                tmux("delete-buffer", "-b", buffer)
            except JvError:
                pass
        if previous is None:
            del _DICTATION_DEADLINE.deadline
        else:
            _DICTATION_DEADLINE.deadline = previous


def _type_check(a: dict) -> str | None:
    a = a if a.get("attach") else _dictated(a)
    p, sessions = resolve(a["target"]), load_sessions(include_ended=True)
    if a.get("attach") and not is_claude(p, sessions):
        raise JvError(4, f"window {p['window_index']} isn't a Claude Code session")
    text = _text(a, p)
    _guard(p, sessions)
    claude, w = is_claude(p, sessions), p["window_index"]
    if a.get("enter") and claude and prompt_draft(p["id"]) is not False:
        return _readback("The prompt has text or is unreadable; it may run in shell mode. " + _submit_check(text, w, a))
    if not a.get("enter") and claude and prompt_draft(p["id"]) is True:   # typing appends to the user's unsent draft
        return _readback(f"Window {w}'s prompt already has text. Add '{text}' after it?")
    if a.get("enter") and claude and text.strip().lower() in ENDS_SESSION:   # friction2 #9: "exit" ended sessions
        return f"Send '{text.strip()}' to the Claude session in window {w}?"
    if a.get("enter") and claude and (_tainted() or text.lstrip().startswith("!")):
        return _submit_check(text, w, a)
    if a.get("enter") and not claude:   # enter into a Claude pane just submits a prompt: no read-back on a clean turn
        return f"Run '{text}' in window {w}?" if text else f"Press enter in window {w}?"
    if "\n" in text and not claude:
        return f"Paste {text.count(chr(10)) + 1} lines into window {w}?"
    if p["alternate"] and not claude:
        return f"Type '{text}' into {p['cmd']} in window {w}?"
    return None


def _type(a: dict) -> dict:
    a = a if a.get("attach") else _dictated(a)
    p, sessions = resolve(a["target"]), load_sessions(include_ended=True)
    if a.get("attach") and not is_claude(p, sessions):
        raise JvError(4, f"window {p['window_index']} isn't a Claude Code session")
    text = _text(a, p)
    _guard(p, sessions)
    before = _session(p, sessions)
    lower, claude, w = text.strip().lower(), is_claude(p, sessions), p["window_index"]
    operation = ("clear_session" if a.get("enter") and claude and lower == "/clear" else
                 "end_session" if a.get("enter") and claude and lower in {"exit", "quit", "/exit", "/quit"} else
                 "submit" if a.get("enter") else "type")
    queued = operation == "submit" and claude and (before or {}).get("state") == "working"
    requested_at = status_store.now_iso()
    if p["in_mode"]:
        tmux("send-keys", "-t", p["id"], "-X", "cancel")
    if "\n" in text:
        tmux("load-buffer", "-b", "jv", "-", input=text)
        tmux("paste-buffer", "-b", "jv", "-d", "-p", "-t", p["id"])
    else:
        tmux("send-keys", "-t", p["id"], "-l", "--", text)
    if a.get("enter"):
        time.sleep(0.15)
        tmux("send-keys", "-t", p["id"], "Enter")
    if operation == "type":
        return {"target": p["target"], "queued": queued, "say": f"typed into window {w} without pressing Enter"}
    if operation == "submit":
        if not claude:
            say = f"typed into window {w} and pressed Enter; command completion is not observed"
        else:
            watch_note = _arm_watch(p["id"], requested_at)

            def submitted(after):
                return bool(after and after.get("last_hook") == "UserPromptSubmit"
                            and status_store.epoch(after.get("updated_at")) >= status_store.epoch(requested_at))
            after = _session_for_pane(p["id"])
            # The hook lands 30-60 ms after Enter (live 10:23:10); checking at once never saw it.
            for _ in range(SUBMIT_POLLS):
                if submitted(after):
                    break
                time.sleep(0.05)
                after = _session_for_pane(p["id"])
            same = not before or after.get("session_id") == before.get("session_id") if after else False
            if submitted(after) and (not text or after.get("last_prompt") == text[:80]) and same:
                say = f"Claude in window {w} accepted the submitted request"
            else:
                say = f"queued for window {w}" if queued else f"typed into window {w} and pressed Enter"
            say += watch_note
        return {"target": p["target"], "queued": queued, "say": say}
    history = status_store.user_sessions(status_store.list_sessions(resolve=False), include_ended=True)
    old = (before or {}).get("session_id")
    ended = next((s for s in history if s.get("session_id") == old and s.get("state") == "ended"), None)
    if operation == "end_session":
        say = (f"the Claude session in window {w} has ended" if ended
               else f"sent the end command in window {w}; exit is not yet observed")
    else:
        replacement = any(s.get("tmux_pane") == p["id"] and s.get("state") != "ended" and s.get("session_id") != old
                          and status_store.epoch(s.get("started_at") or s.get("updated_at"))
                          >= status_store.epoch(requested_at) for s in history)
        say = (f"sent /clear in window {w}; a new session identity was observed" if replacement else
               f"sent /clear in window {w}; the old session ended, but its replacement identity is not yet observed"
               if ended else f"sent /clear in window {w}; the replacement session identity is not yet observed")
    return {"target": p["target"], "queued": queued, "say": say}

def _clear(a: dict) -> dict:
    """C-u: drop what's typed on the pane's input line (a bad dictation)."""
    p = resolve(a.get("target") or "current")
    _guard(p, load_sessions(include_ended=True))
    if p["in_mode"]:
        tmux("send-keys", "-t", p["id"], "-X", "cancel")
    tmux("send-keys", "-t", p["id"], "C-u")
    return {"target": p["target"], "say": f"sent clear-line keys to window {p['window_index']}"}

def _tmux_foreground() -> bool:
    """Whether a terminal containing this tmux session is the active Hyprland window."""
    try:
        from jv import hypr
        active = hypr.hyprctl("activewindow", json_out=True).get("address")
        terminal = next((c for c in hypr.clients() if c.get("address") == active), None)
        terminal_pid = terminal.get("pid") if terminal else None
        if not isinstance(terminal_pid, int):
            return False
        clients = [int(x) for x in tmux("list-clients", "-t", session(), "-F", "#{client_pid}").split()
                   if x.isdigit()]
        for client_pid in clients:
            pid = client_pid
            while pid and pid > 1:
                if pid == terminal_pid:
                    return True
                pid = _ppid(pid) or 0
    except Exception:  # failure to establish focus must not turn "current" into a destructive guess
        return False
    return False


def _close_targets(a: dict) -> list[dict]:
    """Resolve comma/and-separated windows, preserving multiword names and stable pane targets."""
    raw = str(a.get("windows") or "").strip()
    groups = [w.strip() for w in re.split(r"\s*(?:,|&|\band\b)\s*", raw, flags=re.I) if w.strip()]
    if not groups:
        raise JvError(2, "missing argument: windows")
    ps = panes()
    indices = list(dict.fromkeys(p["window_index"] for p in ps))
    names = list(dict.fromkeys(p["window_name"] for p in ps))
    out = {}

    def add(target: str) -> None:
        normalized = normalize_window_target(target, window_indices=indices, window_names=names)
        named = any(target.casefold() == name.casefold() for name in names)
        if not named and normalized.lower() in HERE and not _tmux_foreground():
            raise JvError(2, "Which tmux window should I close? Say its number or name.")
        pane = resolve(target)
        out.setdefault(pane["window_index"], pane)

    for group in groups:
        try:
            add(group)
        except JvError as e:
            # Keep the existing compact "2 3 4" list form while allowing spaces inside a window name.
            tokens = group.split()
            if e.code != 1 or len(tokens) < 2 or not all(
                    normalize_window_target(token).isdigit() for token in tokens):
                raise
            for token in tokens:
                add(token)
    return list(out.values())


def _and(xs: list[str]) -> str:
    return xs[0] if len(xs) == 1 else f"{', '.join(xs[:-1])} and {xs[-1]}"


def _close_check(a: dict) -> str:
    ps, sessions = panes(), load_sessions()
    hit = _close_targets(a)
    claude = [p["window_index"] for p in hit
              if any(is_claude(q, sessions) for q in ps if q["window_index"] == p["window_index"])]
    names = _and([f"{p['window_index']} {p['window_name']}" for p in hit])
    q = f"Close window{'s' * (len(hit) > 1)} {names}?"
    if claude:
        q += f" {_and(claude)} {'is a Claude session' if len(claude) == 1 else 'are Claude sessions'}."
    return q


def _close(a: dict) -> dict:
    hit = _close_targets(a)
    initial = _window_pane_inventory()
    affected = {}
    for p in hit:
        ids = next((pane_ids for pane_ids in initial.values() if p["id"] in pane_ids), set()) if initial else set()
        affected[p["window_index"]] = sorted(ids)
    for i, p in enumerate(hit):
        try:
            tmux("kill-window", "-t", p["id"])   # by pane id: renumber-windows can't shift the next target
        except JvError as e:
            raise JvError(e.code, e.msg, **e.extra, close_sent=[q["window_index"] for q in hit[:i]],
                          not_attempted=[f"window {q['window_index']}" for q in hit[i + 1:]]) from None
    inventory = _pane_inventory()
    closed, lines = [], []
    for p in hit:
        pane_ids = affected.get(p["window_index"], [])
        outcome = None if inventory is None or not pane_ids else all(pid not in inventory for pid in pane_ids)
        if outcome is True:
            closed.append(p["window_index"])
            lines.append(f"closed window {p['window_index']}")
        elif outcome is False:
            lines.append(f"close accepted, but window {p['window_index']} is still present")
        else:
            lines.append(f"close accepted, but window {p['window_index']} could not be checked")
    return {"requested_close": [p["window_index"] for p in hit], "closed": closed, "say": ". ".join(lines),
            "observed": len(closed) == len(hit)}

def _close_pane_target(a: dict) -> tuple[dict, str, bool]:
    """(pane, how to say it, last pane of its window)."""
    t = str(a.get("target") or "this pane").strip()
    if a.get("window") is not None:   # fast phrase: "close the left pane of window 2" = target + window
        t = f"{t} of window {a['window']}"
    ps = panes()
    names = list(dict.fromkeys(p["window_name"] for p in ps))

    def implicit_window(target: str) -> bool:
        raw = str(target).strip()
        named = any(raw.casefold() == name.casefold() for name in names)
        return not named and _spoken(raw, ps).lower() in HERE

    match = PANE.match(t)
    window_ref = a.get("window") or (match[3] if match else None)
    implicit = (window_ref is None or implicit_window(window_ref)) if match else implicit_window(t)
    if implicit and not _tmux_foreground():
        raise JvError(2, "Which tmux pane should I close? Say its side and window, or pane ID.")
    p = resolve(t)
    w = p["window_index"]
    name = (f"the {match[2].lower()} pane of window {w}" if match and match[2] else
            f"this pane in window {w}" if match or t.lower() in HERE else f"the active pane of window {w}")
    return p, name, sum(q["window_index"] == w for q in panes()) == 1


def _close_pane_check(a: dict) -> str:
    p, name, last = _close_pane_target(a)
    if last:
        return _close_check({"windows": p["window_index"]}) + " It's the only pane, so the window closes."
    q = f"Close {name}?"
    return q + " It's a Claude session." if is_claude(p, load_sessions()) else q


def _close_pane(a: dict) -> dict:
    p, name, last = _close_pane_target(a)
    tmux("kill-pane", "-t", p["id"])
    inventory = _pane_inventory()
    pane_absent = inventory is not None and p["id"] not in inventory
    if last:
        say = f"closed window {p['window_index']}" if pane_absent else \
            f"sent close for window {p['window_index']}; closure is not confirmed"
    else:
        say = f"closed {name}" if pane_absent else f"sent close for {name}; closure is not confirmed"
    return {"closed": p["target"] if pane_absent else None, "say": say, "observed": pane_absent}

NAME_MAX = 30


def _rename(a: dict) -> dict:
    p = resolve(a["window"])
    name = str(a.get("name") or "").strip().rstrip(".").strip()[:NAME_MAX].strip()
    if not name:
        raise JvError(2, "missing argument: name")
    if CONTROL.search(name):
        raise JvError(2, "name has control characters")
    tmux("rename-window", "-t", p["id"], "--", name.replace("#", "##"))   # rename-window expands #{formats}
    try:
        observed = any(q["window_index"] == p["window_index"] and q["window_name"] == name for q in panes())
    except Exception:
        observed = False
    return {"target": p["target"], "say": f"renamed window {p['window_index']} to {name}", "observed": observed}


def _neighbour(idx: str, side: str, ps: list[dict]) -> str:
    wins = sorted({int(p["window_index"]) for p in ps})
    i = wins.index(int(idx)) + (1 if side == "right" else -1)
    if not 0 <= i < len(wins):
        raise JvError(1, f"Window {idx} is already the {'last' if side == 'right' else 'first'} window.")
    return str(wins[i])


def _move(a: dict) -> dict:
    """Swap with the window at `to` (others keep their numbers); an empty `to` = move-window there. Then select the
    pane the user was on: they keep looking at the same content (tmux 3.8: swap-window -d jumps to dst instead)."""
    ps, p = panes(), resolve(a["window"])
    src, to = p["window_index"], str(a["to"]).strip().lower()
    dst = _neighbour(src, to, ps) if to in ("left", "right") else _spoken(to, ps)
    if not dst.isdigit():
        raise JvError(2, "to must be a window number, left or right")
    if dst == src:
        return {"target": p["target"], "say": f"window {src} is already there"}
    if dst in {q["window_index"] for q in ps}:
        tmux("swap-window", "-s", f"{session()}:{src}", "-t", f"{session()}:{dst}")
    else:
        tmux("move-window", "-d", "-s", f"{session()}:{src}", "-t", f"{session()}:{dst}")
    if on := next((q["id"] for q in ps if q["window_active"] and q["active"]), None):
        tmux("select-window", "-t", on)   # no-op when it is already current
    try:
        observed = any(q["window_index"] == dst and q["id"] == p["id"] for q in panes())
    except Exception:
        observed = False
    return {"target": f"{session()}:{dst}", "say": f"moved window {src} to {dst}", "observed": observed}


SIDES = {"left": ("-L", "left", "to the left"), "right": ("-R", "right", "to the right"),
         "up": ("-U", "top", "above"), "down": ("-D", "bottom", "below")}


def _edge(p: dict, side: str, ps: list[dict]) -> bool:
    """p sits on that edge of its window: no pane beyond it (select-pane would wrap round instead)."""
    win = [q for q in ps if q["window_index"] == p["window_index"]]
    if side in ("left", "up"):
        return p["left" if side == "left" else "top"] == 0
    k = "right" if side == "right" else "bottom"
    return p[k] >= max(q[k] for q in win)


def _focus(a: dict) -> dict:
    ps, p = panes(), resolve(a.get("window") or "current")
    flag, _, where = SIDES[a["dir"]]
    if _edge(p, a["dir"], ps):
        raise JvError(1, f"There's no pane {where} in window {p['window_index']}.")
    candidates = [q for q in ps if q["window_index"] == p["window_index"] and q["id"] != p["id"] and
                  ((a["dir"] == "left" and q["right"] < p["left"]) or
                   (a["dir"] == "right" and q["left"] > p["right"]) or
                   (a["dir"] == "up" and q["bottom"] < p["top"]) or
                   (a["dir"] == "down" and q["top"] > p["bottom"]))]
    target = min(candidates, key=lambda q: abs(q["left"] - p["left"]) + abs(q["top"] - p["top"])) if candidates else None
    tmux("select-pane", flag, "-t", p["id"])
    try:
        observed = target is not None and any(q["id"] == target["id"] and q["active"] for q in panes())
    except Exception:
        observed = False
    return {"target": p["target"], "say": f"{SIDES[a['dir']][1]} pane", "observed": observed}


def _answer_check(a: dict) -> str:
    p = resolve(a["target"])
    _guard(p, load_sessions(include_ended=True), prompt=True)
    if a["choice"] == "esc":
        return f"Press escape in window {p['window_index']}?"
    d = dialog(p["id"])
    if not d or not d["question"]:
        raise JvError(4, "couldn't read the dialog question; answer on screen")
    question = textwrap.shorten(d["question"].rstrip("?"), 100, placeholder="…")
    option = textwrap.shorten(option_text(p['id'], int(a['choice'])) or a['choice'], 50, placeholder="…")
    return _readback(f"Answer '{option}' to {question} in window {p['window_index']}?")


def _answer(a: dict) -> dict:
    p = resolve(a["target"])
    _guard(p, load_sessions(include_ended=True), prompt=None)
    tmux("send-keys", "-t", p["id"], "Escape" if a["choice"] == "esc" else a["choice"])
    return {"say": f"sent the answer key to window {p['window_index']}; Claude's response is not yet observed"}

def _obj(props: dict, required: list[str] = ()) -> dict:
    return {"type": "object", "properties": props, "required": list(required)}


S = {"type": "string"}
HERE_ARG = {"target": "current"}
HERE_W = {"window": "current"}
MOVE_TO = [f"{head} to {w}{{to:int}}" for head in ("move window {window:int}", "move this window")
           for w in ("", "window ", "position ")]
MOVE_SIDE = {f"{head} (to )?{side}": {"to": side} for head in ("move window {window:int}", "move this window")
             for side in ("left", "right")}
MOVE_ARGS = {**{ph: HERE_W for ph in MOVE_TO if "this" in ph},
             **{ph: {**a, **(HERE_W if "this" in ph else {})} for ph, a in MOVE_SIDE.items()}}
# context: "paste my clipboard into window 3 (and press enter)", "type what I copied into window 3"
PASTE_FROM = {"clipboard": ["(my )?clipboard", "the copied text", "what i copied", "my copied text",
                            "the selected text from the clipboard", "selected text from the clipboard",
                            "selected text from clipboard"],
              "selection": ["the selected text", "the selection", "what i selected", "the highlighted text",
                            "my selection"]}
PASTE_PHRASES = {f"{verb} {what} in(to)? window {{target:int}}{enter}": {src: True, **({"enter": True} if enter else {})}
                 for src, whats in PASTE_FROM.items() for what in whats for verb in ("paste", "type")
                 for enter in ("", " and (then )?press enter")}
# fix2 P2: "type X and send it" (the "and" would send it to Claude); "… enter" without "and" is stripped by _dictated
TYPE_HEADS = {"type in this window": HERE_ARG, "type in this session": HERE_ARG, "type in current window": HERE_ARG,
              "type in window {target:int}": {}, "type": HERE_ARG}
SEND_PHRASES = {f"{head} {{text:text}} and {tail}": {**arg, "enter": True} for head, arg in TYPE_HEADS.items()
                for tail in ("press enter", "send it", "submit")}
ATTACH_SOURCES = {
    "tab": ("this page", "this tab", "this link", "the page"),
    "clipboard": ("my clipboard", "what i copied"),
    "selection": ("my selection", "the selected text"),
    "last_heard": ("what i just said", "what i said"),
    "pane": ("the output of window {attach_target:int}",),
    "screenshot": ("a screenshot", "the screenshot"),
}
ATTACH_VERBS = ("send", "pass", "give")
ATTACH_TO_CLAUDE = {
    f"{verb} {source} to claude{code}": {"run": "claude", "attach": kind}
    for kind, sources in ATTACH_SOURCES.items() for source in sources
    for verb in ATTACH_VERBS for code in ("", " code")
}
ATTACH_TO_WINDOW = {
    f"{verb} {source} to {claude}window {{target:int}}": {"attach": kind}
    for kind, sources in ATTACH_SOURCES.items() if kind != "screenshot" for source in sources
    for verb in ATTACH_VERBS for claude in ("", "claude in ")
}
# "split (this|the) window side by side", "split window 2 vertically and open claude"
SPLIT_DIRS = {"horizontally": "horizontal", "side by side": "horizontal", "vertically": "vertical",
              "top and bottom": "vertical"}
SPLIT_PHRASES = {f"{head} {said}{run}": {"dir": d, **(HERE_W if "{" not in head else {}),
                                         **({"run": "claude"} if run else {})}
                 for head in ("split this window", "split window", "split current window", "split window {window:int}")
                 for said, d in SPLIT_DIRS.items() for run in ("", " and open claude", " and run claude", " and start claude")}
# "go to the left pane", "focus the top pane", "right pane", "switch to the bottom pane in window 2"
PANE_SIDES = {"left": "left", "right": "right", "top": "up", "bottom": "down", "upper": "up", "lower": "down"}
FOCUS_PHRASES = {f"{verb}{said} pane{w}": {"dir": d} for verb in ("", "switch to ", "focus ", "focus on ")
                 for said, d in PANE_SIDES.items() for w in ("", " in window {window:int}")}
# "close this pane", "close the left pane", "kill the bottom pane in window 2"
CLOSE_PANE = {f"close {side} pane{w}": {"target": f"{side} pane"}
              for side in ("this", "current", *PANE_SIDES) for w in ("", " in window {window:int}", " of window {window:int}")
              if not (w and side in ("this", "current"))}
CLOSE_PANE["close pane"] = {"target": "this pane"}
CLOSE_CURRENT = {phrase: {"windows": "current"} for phrase in (
    "close window", "close the window", "close this window", "close current window", "close the current window",
    "close this tmux window", "close the current tmux window", "close the active tmux window",
)}
VERBS = [
    Verb("tmux.list", _list, "List tmux windows and panes, marking Claude sessions.", _obj({}), []),
    Verb("tmux.select", _select, "Switch to a tmux window by number or name (\"current\" = the one on screen).",
         _obj({"window": S, "no_focus": {"type": "boolean"}}, ["window"]), ["window", "--no-focus"],
         args={"window": {"type": "enum", "choices": window_choices}},
         phrases=["focus window {window:enum}", "switch to tmux window {window:enum}", "switch to window {window:enum}",
                  "tmux window {window:enum}"], observes=True),
    Verb("tmux.new", _new, "Open a new tmux window in a folder, optionally running claude or nvim. dir is a path or a project "
         "name as heard (\"web app\"), resolved under the configured project roots "
         "(case-insensitive when unambiguous); no dir: claude starts in the configured Claude home. \"Open a claude session "
         "and do X\" = one call with run claude and prompt X; don't tmux_type into a claude that just started. "
         "attach: tab link, clipboard, selection, pane output, last_heard, or the latest screenshot; needs run claude. prompt is the instruction "
         "(default Look at this.); keep it under 100 characters when attaching. attach_target is required only "
         "for pane. Do not read the source with ctx first. Instructions starting ! or / are refused. "
         "source_revision binds the named attachment after read-back; never invent it. "
         "attach works the same with run claude and prompt = the user's instruction."
         " \"Open a Claude session (in D) and do X\" is one tmux_new with run \"claude\" and prompt X; never tmux_type into a session you just opened. \"Open claude (or a shell) in <project>\" is tmux_new with dir set to the project name as heard; never build cd, zoxide or other shell commands to reach a folder. In \"open claude in heardback\", heardback is the project name, never the assistant; use tmux_new with dir heardback. If the name doesn't resolve, say the closest projects from the error instead of asking for a path. No folder named means the configured Claude home: call tmux_new without dir, never ask where. Attach rules: see tmux_type.",
         _obj({"dir": S, "name": S, "run": {"type": "string", "enum": ["shell", "claude", "nvim"]},
               "prompt": {"type": "string", "maxLength": 4000},
               "attach": {"type": "string", "enum": ["tab", "clipboard", "selection", "pane", "last_heard", "screenshot"]},
               "attach_target": S,
               "source_revision": {"type": "string", "maxLength": 75,
                                   "pattern": r"^(clipboard|selection|tab|pane|heard|screenshot):[0-9a-f]{64}$"}}),
         ["--dir", "--name", "--run", "--prompt", "--attach", "--attach-target", "--source-revision"],
         confirm="auto", check=_new_check, args={"run": {"type": "enum", "choices": ["shell", "claude", "nvim"]},
                                              "attach_target": {"type": "int"}},
         phrases=list(ATTACH_TO_CLAUDE), phrase_args=ATTACH_TO_CLAUDE, observes=True),
    Verb("tmux.split", _split, "Split a tmux window into two panes: dir horizontal = side by side, vertical = "
         "stacked (top and bottom). The new pane opens in the same folder and gets the focus; run claude (with an "
         "optional first prompt) or a shell. window: number, name, or \"current\".",
         _obj({"window": S, "dir": {"type": "string", "enum": list(SPLIT)},
               "run": {"type": "string", "enum": ["shell", "claude"]}, "prompt": {"type": "string", "maxLength": 4000}},
              ["dir"]), ["--window", "--run", "--prompt", "dir"],
         confirm="auto", check=_split_check,
         args={"window": {"type": "int"}, "dir": {"type": "enum", "choices": list(SPLIT)},
               "run": {"type": "enum", "choices": ["shell", "claude"]}},
         phrases=list(SPLIT_PHRASES), phrase_args=SPLIT_PHRASES, observes=True),
    Verb("tmux.focus", _focus, "Move to the next pane left / right / up / down in a tmux window (default the one on "
         "screen).", _obj({"window": S, "dir": {"type": "string", "enum": list(SIDES)}}, ["dir"]),
         ["--window", "dir"], args={"window": {"type": "int"}, "dir": {"type": "enum", "choices": list(SIDES)}},
         phrases=list(FOCUS_PHRASES), phrase_args=FOCUS_PHRASES, observes=True),
    Verb("tmux.read", _read, "Read the last lines of a tmux pane (target \"current\" = the window on screen).",
         _obj({"target": S, "lines": {"type": "integer", "minimum": 1, "maximum": 500, "default": 40}}, ["target"]),
         ["target", "--lines"]),
    Verb("tmux.type", _type, "Type text into a tmux pane, optionally pressing enter. target: window number or name, "
         "\"claude\", or \"current\" for the window on screen. clipboard / selection: type what the user copied / "
         "selected instead of text (jv reads it; leave text out). source_revision is opaque metadata for binding a "
         "confirmed paste to the exact clipboard or selection contents. After reading outside text, a submit into a "
         "Claude pane requires a read-back: keep text under 150 characters. Send to Claude Code with no session "
         "named: use tmux_new with run claude and prompt, in the configured Claude home; never guess an existing session. "
         "attach: send a tab link, clipboard, selection, pane output or last_heard to an existing Claude session; implies Enter. "
         "text is the instruction (default Look at this.); keep text under 100 characters when attaching. "
         "attach_target is required only for pane. Do not read the source with ctx first. "
         "Attachments cannot be combined with clipboard/selection flags or instructions starting ! or /. "
         "attach sends a reference with a short instruction: tab (the current Zen tab's link), clipboard, selection, "
         "pane (attach_target = the window whose last 200 lines to send), last_heard (the user's previous request). "
         "jv reads it; never read it first or copy it into text. With attach, text is only the user's instruction, "
         "under 100 characters, and enter is implied; the target must be a Claude Code session. "
         "A plain leading ! with Enter into Claude always requires a shell-command read-back."
         " \"Type X\" or \"write X\" (optionally \"in this window\", \"in window N\" or \"in <name>\") is an order, not chat: call tmux_type at once with only X as the text, without the command words, target \"current\" unless a window is named, and press enter only if they say \"and press enter\" or \"send it\". Don't reply about the words themselves. \"This window\" or \"here\" means the current tmux window (target \"current\"), so don't ask which. Never call tmux_type twice with the same text in one turn. Never press enter in a window you didn't type into in this turn, unless the user asked only to press enter. To type or paste the copied or selected text, call tmux_type with clipboard or selection true and no text; never type the words \"selected text\" or \"clipboard\" themselves (a misheard \"Slector Test\" is \"selected text\"). Only call tmux_type with them after an explicit paste request and a clear destination, and preserve the returned source_revision when pasting that same reading. Never invent a source_revision. To send this page, this tab or link, the clipboard, the selection, a window's output or the user's previous words to Claude Code, call tmux_type (a named session) or tmux_new with run claude (none named) once, with attach and only the user's instruction as text, under 100 characters. Never read it with ctx_ctx first and never copy it into text. \"This error\" or \"this output\" means attach pane with attach_target \"current\" when a terminal is on screen; otherwise ask which window. heardback reads out a session's answers itself; never repeat or guess what another session said.",
         _obj({"target": S, "text": {"type": "string", "maxLength": 4000}, "enter": {"type": "boolean"},
               "clipboard": {"type": "boolean"}, "selection": {"type": "boolean"},
               "attach": {"type": "string", "enum": ["tab", "clipboard", "selection", "pane", "last_heard"]},
               "attach_target": S,
               "source_revision": {"type": "string", "maxLength": 74,
                                   "pattern": r"^(clipboard|selection|tab|pane|heard):[0-9a-f]{64}$"}},
              ["target"]), ["target", "text", "--enter", "--clipboard", "--selection", "--attach", "--attach-target", "--source-revision"],
              # Source revisions contain only an opaque digest, never attachment text.
         confirm="auto", check=_type_check, args={"text": {"type": "text"}, "target": {"type": "int"},
                                                   "attach_target": {"type": "int"}},
         # frictionfix: what the user says (friction #1, #7, #13, #14); exact match only (confirm verbs never fuzzy)
         phrases=["type in this window {text:text}", "type in this session {text:text}",
                  "type in current window {text:text}", "type in window {target:int} {text:text}",
                  "type {text:text}", "press enter( in this window)?", "press enter in window {target:int}",
                  *PASTE_PHRASES, *SEND_PHRASES, *ATTACH_TO_WINDOW],
         phrase_args={"type in this window {text:text}": HERE_ARG, "type in this session {text:text}": HERE_ARG,
                      "type in current window {text:text}": HERE_ARG, "type {text:text}": HERE_ARG,
                      "press enter( in this window)?": {**HERE_ARG, "text": "", "enter": True},
                      "press enter in window {target:int}": {"text": "", "enter": True}, **PASTE_PHRASES,
                      **SEND_PHRASES, **ATTACH_TO_WINDOW}),
    Verb("tmux.clear", _clear, "Clear the typed input line (ctrl-u) in a tmux pane; target defaults to the window on "
         "screen. Use to undo a bad dictation.",
         _obj({"target": S}), ["target"], args={"target": {"type": "int"}},
         phrases=["clear the line", "clear that", "delete that", "clear the line in window {target:int}"]),
    Verb("tmux.close", _close, "Close (kill) tmux windows by number or name, e.g. windows \"2, 3 and 4\". This is how "
         "to close a window or end a Claude session in it; never type exit or quit instead. An implicit current window "
         "is available only when its tmux terminal is in the foreground; otherwise ask which window."
         " Closing a tmux window (or ending the Claude session in it) is tmux_close with the window numbers, never typing exit or quit.",
         _obj({"windows": S}, ["windows"]), ["windows"], confirm=True, check=_close_check,
         args={"windows": {"type": "text"}}, observes=True,
         phrases=["close window {windows:text}", "close windows {windows:text}", "close tmux window {windows:text}",
                  "kill window {windows:text}", *CLOSE_CURRENT], phrase_args=CLOSE_CURRENT),
    Verb("tmux.close_pane", _close_pane, "Close (kill) one pane of a split tmux window. target: \"this pane\" "
         "(default), \"left pane\" / right / top / bottom, optionally with window = the window it's in. Closing the "
         "last pane closes the window.",
         _obj({"target": S, "window": S}), ["target", "--window"], confirm=True, check=_close_pane_check,
         args={"window": {"type": "int"}}, phrases=list(CLOSE_PANE), phrase_args=CLOSE_PANE, observes=True),
    Verb("tmux.rename", _rename, "Rename a tmux window. window: number, name, or \"current\" for the one on screen.",
         _obj({"window": S, "name": {"type": "string", "maxLength": 200}}, ["window", "name"]), ["window", "name"],
         args={"window": {"type": "int"}, "name": {"type": "text"}},
         phrases=["rename window {window:int} to {name:text}", "rename this window to {name:text}",
                  "rename current window to {name:text}", "call window {window:int} {name:text}",
                  "call this window {name:text}"],
         phrase_args={"rename this window to {name:text}": HERE_W, "rename current window to {name:text}": HERE_W,
                      "call this window {name:text}": HERE_W}, observes=True),
    Verb("tmux.move", _move, "Move a tmux window: swap it with the window at position `to` (the others keep their "
         "numbers; an empty position just moves it there), or `to` left / right = swap with the previous / next "
         "window. window: number, name, or \"current\". Not Hyprland workspaces (that's hypr_move).",
         _obj({"window": S, "to": {"type": "string", "pattern": "^([0-9]+|left|right)$"}}, ["window", "to"]),
         ["window", "to"], args={"window": {"type": "int"}, "to": {"type": "int"}},
         phrases=[*MOVE_TO, *MOVE_SIDE, "swap window(s)? {window:int} and {to:int}",
                  "swap window(s)? {window:int} with {to:int}"],
         phrase_args=MOVE_ARGS, observes=True),
    Verb("tmux.prompt", _prompt, "Say the question and numbered options a Claude window's open dialog is asking.",
         _obj({"target": S}, ["target"]), ["target"], args={"target": {"type": "int"}},
         phrases=["what is window {target:int} asking", "what does window {target:int} want",
                  "what is this window asking"],
         phrase_args={"what is this window asking": HERE_ARG}),
    Verb("tmux.answer", _answer, "Answer a numbered prompt (1-9) or press escape in a tmux pane.",
         _obj({"target": S, "choice": {"type": "string", "pattern": "^[1-9]$|^esc$"}}, ["target", "choice"]),
         ["target", "choice"], confirm=True, check=_answer_check),
]
PANE_T = (" In a split window, target a pane as \"left pane of window 2\" (left / right / top / bottom) or "
          "\"this pane\"; a plain window target means its active pane.")
for v in VERBS:
    if v.name in ("tmux.read", "tmux.type", "tmux.clear", "tmux.prompt", "tmux.answer"):
        v.description += PANE_T
