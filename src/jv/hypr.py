"""Hyprland verbs: list, workspace, focus, launch, open, close, move. Every call goes through _run()."""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from jv.verb import JvError, Verb, fuzzy, _readback

# Lua dispatch shapes, live-probed 2026-10-30 (follow = false keeps focus on move). None → cycle/focus fallback below.
FOCUS_WINDOW: str | None = 'hl.dsp.focus({{window = "address:{addr}"}})'
MOVE_WINDOW: str | None = 'hl.dsp.window.move({{workspace = {ws}, window = "address:{addr}", follow = false}})'
ADDR_RE = r"^0x[0-9a-f]+$"
ID_RE = r"^[A-Za-z0-9_][A-Za-z0-9._-]*$"
HOME_WS = {"zen": 2}
CYCLE_MAX = 20
_inst: str | None = None


def _run(argv: list[str]) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        raise JvError(1, "hyprctl not installed")
    except subprocess.TimeoutExpired:   # its str() holds the argv
        raise JvError(1, "hyprctl timed out")
    if r.returncode:
        raise JvError(1, f"hyprctl: {(r.stderr or r.stdout).strip()}")
    return r.stdout


def instance() -> str:
    # Newest live instance; HYPRLAND_INSTANCE_SIGNATURE goes stale across Hyprland restarts.
    global _inst
    if _inst is None:
        insts = json.loads(_run(["hyprctl", "-j", "instances"]) or "[]")
        if not insts:
            raise JvError(1, "hyprland not running")
        _inst = max(insts, key=lambda i: i["time"])["instance"]
    return _inst


def hyprctl(*args: str, json_out: bool = False):
    out = _run(["hyprctl", "-i", instance(), *(["-j"] if json_out else []), *args])
    return json.loads(out) if json_out else out


def dispatch(lua: str) -> None:
    if os.environ.get("JV_DRY") == "1":
        print(lua, file=sys.stderr)
        return
    out = hyprctl("dispatch", lua).strip()
    if out != "ok":
        raise JvError(1, out)


def clients() -> list[dict]:
    return hyprctl("clients", json_out=True)


def _active() -> str | None:
    return hyprctl("activewindow", json_out=True).get("address")


def _addr(addr: str) -> str:
    if not re.fullmatch(ADDR_RE, addr):
        raise JvError(1, f"bad window address {addr!r}")
    return addr


def focus_window(addr: str) -> None:
    addr = _addr(addr)
    if FOCUS_WINDOW:
        return dispatch(FOCUS_WINDOW.format(addr=addr))
    c = next((c for c in clients() if c["address"] == addr), None)
    if not c:
        raise JvError(1, "window is gone")
    dispatch(f"hl.dsp.focus({{workspace = {int(c['workspace']['id'])}}})")
    for _ in range(CYCLE_MAX):
        if _active() == addr:
            return
        dispatch("hl.dsp.window.cycle_next()")
    if _active() != addr:
        raise JvError(1, "couldn't focus window")


def move_window(addr: str, ws: int) -> None:
    addr, ws = _addr(addr), int(ws)
    if MOVE_WINDOW:
        return dispatch(MOVE_WINDOW.format(addr=addr, ws=ws))
    prev = _active()
    focus_window(addr)
    dispatch(f"hl.dsp.window.move({{workspace = {ws}}})")
    if prev and prev != addr:
        focus_window(prev)


def desktop_apps() -> dict[str, str]:
    home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    dirs = [home, *(os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")]
    apps, seen = {}, set()
    for d in dirs:
        for f in sorted(Path(d, "applications").glob("*.desktop")):
            if f.stem in seen:   # earlier dirs override later ones
                continue
            seen.add(f.stem)
            entry, section = {}, None
            try:
                lines = f.read_text(errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                if line.startswith("["):
                    section = line.strip()
                elif section == "[Desktop Entry]" and "=" in line:
                    k, v = line.split("=", 1)
                    entry.setdefault(k.strip(), v.strip())
            if entry.get("Name") and "true" not in (entry.get("NoDisplay"), entry.get("Hidden")):
                apps.setdefault(entry["Name"], f.stem)
    return apps


APP_MIN_SCORE = 0.75   # launching starts programs: "whatsapp" must not fuzz to "Htop" (0.5)


def _app(query: str) -> tuple[str, str]:
    apps = desktop_apps()
    try:
        name = fuzzy(query, list(apps), min_score=APP_MIN_SCORE)
    except JvError as e:
        if e.code == 1:
            raise JvError(1, f"no installed app named {query!r}; for a website use zen_open", **e.extra) from None
        raise
    if not re.fullmatch(ID_RE, apps[name]):
        raise JvError(1, f"bad desktop id {apps[name]!r}")
    return name, apps[name]


def _launch_id(did: str) -> None:
    dispatch(f'hl.dsp.exec_cmd("gtk-launch {did}")')   # Hyprland's env, not jv's possibly stale one


def _client(query: str) -> dict:
    cs = clients()
    if query == "active":
        a = _active()
        c = next((c for c in cs if c["address"] == a), None)
        if not c:
            raise JvError(1, "no active window")
        return c
    labels = {f"{c['class']} {c['title']}": c for c in cs}
    q = query.strip().casefold()
    for hits in ([c for c in cs if c["class"].casefold() == q],
                 [c for c in cs if c["class"].casefold().startswith(q)]):
        if hits:
            if len(hits) == 1:
                return hits[0]
            labels = {f"{c['class']} {c['title']}": c for c in hits}
            return labels[fuzzy(query, list(labels))]
    return labels[fuzzy(query, list(labels))]


def _list(a: dict) -> dict:
    ws = [{"id": w["id"], "windows": w["windows"]} for w in hyprctl("workspaces", json_out=True)]
    cs = [{"ws": c["workspace"]["id"], "class": c["class"], "title": c["title"], "address": c["address"]}
          for c in clients()]
    return {"workspaces": ws, "clients": cs, "say": f"{len(cs)} window{'s' * (len(cs) != 1)}"}


def _workspace(a: dict) -> dict:
    dispatch(f"hl.dsp.focus({{workspace = {int(a['n'])}}})")
    try:
        observed = hyprctl("activeworkspace", json_out=True).get("id") == int(a["n"])
    except Exception:
        observed = False
    return {"observed": observed, "say": f"workspace {a['n']}"}


def _focus(a: dict) -> dict:
    c = _client(a["query"])
    focus_window(c["address"])
    observed = False
    try:
        for i in range(4):
            if _active() == c["address"]:
                observed = True
                break
            if i < 3:
                time.sleep(0.05)
    except (JvError, ValueError, TypeError, AttributeError):
        pass   # dispatch succeeded; an unreadable receipt cannot confirm focus
    return {"address": c["address"], "observed": observed, "say": f"focused {c['class']}"}


def _close_check(a: dict) -> str:
    revision = a.get("source_revision")
    if revision:
        addr = revision.split(":")[1]
        c = next((c for c in clients() if c["address"] == addr), None)
        if c is None:
            raise JvError(1, "heardback refused: that window is gone.")
    else:
        if re.search(r"\btabs?\b", a.get("query", ""), re.I):   # a tab is never a whole window (2026-11-17: closed Zen)
            raise JvError(2, "hypr.close closes whole windows, not tabs",
                          say="I only close whole windows that way. Say close unused tabs, or close the name tabs.")
        c = _client(a.get("query", "active"))
    addr = _addr(c["address"])
    identity = json.dumps([addr, c.get("pid"), c["class"], c["title"]], ensure_ascii=False)
    current = f"hypr:{addr}:" + hashlib.sha256(identity.encode()).hexdigest()
    if revision and revision != current:
        raise JvError(1, "heardback refused: that window changed after the read-back; ask again.")
    a["_source_revision"], a["_client"] = current, c
    return _readback(f"Close {c['class']}, titled '{c['title']}'?")


def _close(a: dict) -> dict:
    c = a["_client"]
    dispatch(f'hl.dsp.window.close({{window = "address:{_addr(c["address"])}"}})')
    try:
        observed = all(row.get("address") != c["address"] for row in clients())
    except Exception:
        observed = False
    return {"address": c["address"], "observed": observed, "say": f"asked {c['class']} to close"}


def _launch(a: dict) -> dict:
    name, did = _app(a["app"])
    _launch_id(did)
    return {"say": f"launching {name}"}


def _open(a: dict) -> dict:
    name, did = _app(a["app"])
    keys = (did.casefold(), name.casefold())
    hits = [c for c in clients() if (cls := c.get("class", "").casefold())
            and any(cls in k or k in cls for k in keys)]
    if hits:
        focus_window(min(hits, key=lambda c: c.get("focusHistoryID", 99))["address"])
        return {"say": f"focused {name}"}
    ws = next((HOME_WS[k] for k in keys if k in HOME_WS), None)
    if ws:
        dispatch(f"hl.dsp.focus({{workspace = {int(ws)}}})")
    _launch_id(did)
    return {"say": f"launching {name}"}


def _move(a: dict) -> dict:
    c = _client(a["query"])
    move_window(c["address"], a["workspace"])
    try:
        observed = next(row for row in clients() if row.get("address") == c["address"])["workspace"]["id"] == a["workspace"]
    except Exception:
        observed = False
    return {"observed": observed, "say": f"moved {c['class']} to {a['workspace']}"}


def app_choices() -> list[str]:
    return list(desktop_apps())


def _obj(props: dict, required: list[str] = ()) -> dict:
    return {"type": "object", "properties": props, "required": list(required)}


WS = {"type": "integer", "minimum": 1, "maximum": 10}
S = {"type": "string"}
APP = {"app": {"type": "enum", "choices": app_choices}}
MOVE_ACTIVE = {f"move {w} to workspace {{workspace:int}}": {"query": "active"}
               for w in ("this window", "current window", "active window", "window", "this", "it")}
VERBS = [
    Verb("hypr.list", _list, "List Hyprland workspaces and windows.", _obj({}), []),
    Verb("hypr.workspace", _workspace, "Go to a Hyprland workspace (1-10).", _obj({"n": WS}, ["n"]), ["n"],
         args={"n": {"type": "int"}}, phrases=["go to workspace {n:int}", "workspace {n:int}", "focus workspace {n:int}", "switch to workspace {n:int}"], observes=True),
    Verb("hypr.focus", _focus, "Focus a window by class or title.", _obj({"query": S}, ["query"]), ["query"],
         observes=True,
         args={"query": {"type": "str"}}, phrases=["focus {query:str}"]),
    Verb("hypr.close", _close, "Politely close the focused app or a named client, after reading back its class and title. Preserve source_revision from the read-back.",
         _obj({"query": S, "source_revision": {"type": "string", "pattern": r"^hypr:0x[0-9a-f]+:[0-9a-f]{64}$"}}),
         ["query", "--source-revision"], confirm=True, check=_close_check,
         args={"query": {"type": "str"}},
         phrases=["close {query:str}", "close the app", "close this app", "close the focused window"],
         phrase_args={p: {"query": "active"} for p in ("close the app", "close this app", "close the focused window")}, observes=True),
    Verb("hypr.launch", _launch, "Launch a desktop app by name.", _obj({"app": S}, ["app"]), ["app"],
         args=APP, phrases=["launch {app:enum}"]),
    Verb("hypr.open", _open, "Focus an app if it is running, else launch it (on its home workspace)."
         " If hypr_open or hypr_launch says \"no installed app\", use zen_open instead unless the user explicitly required the Spotify desktop app; never launch an app whose name differs from what the user said.",
         _obj({"app": S}, ["app"]), ["app"], args=APP,
         phrases=["open {app:enum}", "open spotify", "open spotify app", "open desktop spotify"],
         phrase_args={p: {"app": "Spotify"} for p in ("open spotify", "open spotify app", "open desktop spotify")}),
    Verb("hypr.move", _move, "Move a window (or 'active') to a workspace.",
         _obj({"query": S, "workspace": WS}, ["query", "workspace"]), ["query", "workspace"],
         args={"query": {"type": "str"}, "workspace": {"type": "int"}},
         phrases=["move {query:str} to workspace {workspace:int}", *MOVE_ACTIVE], phrase_args=MOVE_ACTIVE, observes=True),
]
