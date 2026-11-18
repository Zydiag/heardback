"""jv verb catalogue (cached `jv list --json`), argv builder and async jv runner."""
import asyncio
import contextlib
import fcntl
import json
import logging
import os
import subprocess
import tempfile
import time
import uuid

from heardback.status.store import state_dir
from jv import trace
from jv.verb import runtime_path

CATALOGUE_MAX_AGE_S = 30
JV_TIMEOUT_S = 10
DO_TIMEOUT_S = 65   # user actions (`jv do`): foreground cap 60 s, jv kills the step itself
ZEN_PAGE_TIMEOUT_S = 26   # zen pick: 14 s for the results page (jv.zen.PAGE_TIMEOUT) + the autoplay check (5 s + 3.5 s)
ZEN_PLAY_TIMEOUT_S = 45   # yt_first + fallback + autoplay, plus inventory/create/activate bridge calls
SHOT_TIMEOUT_S = 65   # area selection can take longer than grimblast's 30 s capture cap

# The injection gate fails closed: a tool taints the conversation unless it is here, so a new verb taints by default.
# Listed tools return only our own words, numbers and the names the request itself gave: never page text, tab or
# window titles, pane contents, session replies, context or status text (wallpaper file names from the user's own
# folder are fine; device and audio-sink names are not). A failed call taints whatever its name.
CLEAN_TOOLS = frozenset("mcp__jv__" + t for t in (
    "ask_user", "jv_find", "zen_play", "media_media",   # MCP hides external media data; speech uses a private receipt.
    "tmux_type", "tmux_clear", "tmux_select", "tmux_focus", "tmux_new", "tmux_split", "hypr_workspace",
    "volume_volume", "brightness_brightness", "local_time", "local_date", "local_lock", "calculate_calculate",
    "reminder_add", "reminder_cancel", "inbox_add", "fact_remember", "fact_forget", "shot_shot",
    "wall_next", "wall_random", "wall_set", "wall_show", "wall_current",
    "settings_dnd", "settings_night", "settings_power", "settings_power_get", "settings_mic", "settings_window",
    "settings_theme", "system_battery", "system_disk", "system_memory", "system_temperature", "system_gpu",
    "system_updates", "system_tailscale", "system_online"))
TAINT_ENV = "HEARDBACK_TAINTED"   # = jv.tmux.TAINT_ENV; environment, so no tool input can set or clear it

# E2: Claude sees a few verbs as their own tools; every other verb is reached through jv_find + jv_run.
FIND, RUN = "jv_find", "jv_run"


def resolve(cat: dict, tool: str, inp) -> tuple[dict | None, dict]:
    """(catalogue entry, the args jv receives) for a direct tool or jv_run's inner verb; (None, {}) when unknown,
    not Claude's, or malformed. jv_run takes exactly {verb, args}: nothing outside args reaches jv or the grant."""
    tools = {tool_name(e): e for e in cat.values() if e.get("claude", True)}
    if tool != RUN:
        return tools.get(tool), inp if isinstance(inp, dict) else {}
    if not isinstance(inp, dict) or inp.keys() - {"verb", "args"} or not isinstance(verb := inp.get("verb"), str):
        return None, {}
    entry = tools.get(verb) or next((e for e in tools.values() if e["name"] == verb), None)
    args = inp.get("args", {})
    if entry is None or not isinstance(args, dict) or args.keys() - entry["params"].get("properties", {}).keys():
        return None, {}
    return entry, args


log = logging.getLogger(__name__)
_cache: dict = {"at": None, "cat": {}}


def _fetch() -> dict:
    p = subprocess.run(["jv", "list", "--json"], capture_output=True, text=True, timeout=JV_TIMEOUT_S)
    if p.returncode != 0:
        raise RuntimeError(f"jv list exit {p.returncode}")
    return json.loads(p.stdout)


def _index(data: dict) -> dict[str, dict]:
    # Older on-disk catalogues may still mark this CLI-only command voice-enabled.
    return {v["name"]: v for v in data["verbs"] if v.get("voice", True) and v["name"] != "say"}


def get(force: bool = False) -> dict[str, dict]:
    """Blocking (runs `jv list`): async callers use `asyncio.to_thread(get)`."""
    now = time.monotonic()
    if not force and _cache["at"] is not None and now - _cache["at"] < CATALOGUE_MAX_AGE_S:
        return _cache["cat"]
    path = state_dir() / "catalogue.json"
    try:
        data = _fetch()
        cat = _index(data)
        _write_cache(path, data)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as e:
        log.warning("jv list failed (%s); using cache", e)
        try:
            cat = _index(json.loads(path.read_text()))
        except (OSError, ValueError, KeyError, TypeError):
            cat = {}
    if cat != _cache["cat"]:
        _cache["cat"] = cat   # keep identity when unchanged so callers can cheaply detect changes
    _cache["at"] = now
    return _cache["cat"]


def _write_cache(path, data: dict) -> None:
    """Atomic (unique tmp + os.replace: brain and MCP server may write at once); a failed write keeps the catalogue."""
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".catalogue-", suffix=".tmp",
                                         delete=False) as f:
            tmp = f.name
            f.write(json.dumps(data))
        os.replace(tmp, path)
    except OSError as e:
        log.warning("cannot write %s (%s)", path, e)
        if tmp:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def tool_name(entry: dict) -> str:
    return f"{entry['domain']}_{entry['verb']}"


def choices(entry: dict, param: str) -> list[str]:
    return entry.get("args", {}).get(param, {}).get("choices", [])


def slot_type(entry: dict, param: str) -> str | None:
    return entry.get("args", {}).get(param, {}).get("type")


def build_argv(entry: dict, args: dict, *extra: str) -> list[str]:
    """`["jv", domain, verb, *flags, *extra, "--", *positionals]`; no `--` without positionals."""
    flags, pos = [], []
    for a in entry["argv"]:
        if a.startswith("--"):
            v = args.get(a[2:].replace("-", "_"))
            if v is True:
                flags.append(a)
            elif v is not None and v is not False:
                flags += [a, str(v)]
        elif (v := args.get(a.replace("-", "_"))) is not None:
            pos.append(str(v))
    # a dotless verb (media, volume, brightness, say, ctx) is a top-level command: `jv media`, not `jv media media`
    head = [entry["domain"]] if "." not in entry.get("name", ".") else [entry["domain"], entry["verb"]]
    return ["jv", *head, *flags, *extra, *(["--", *pos] if pos else [])]


def jv_timeout(argv: list[str], timeout: float) -> float:
    if argv[1:2] == ["do"]:
        return max(timeout, DO_TIMEOUT_S)
    if argv[1:3] == ["zen", "play"]:
        return max(timeout, ZEN_PLAY_TIMEOUT_S)
    if argv[1:2] == ["shot"]:
        return max(timeout, SHOT_TIMEOUT_S)
    return max(timeout, ZEN_PAGE_TIMEOUT_S) if argv[1:3] == ["zen", "pick"] else timeout


TURN_FILE = "heardback-turn"   # in $XDG_RUNTIME_DIR: the Claude turn in flight, read by the MCP server


@contextlib.contextmanager
def execution_lock():
    """Serialize revocation with process launch only; never hold while a command runs."""
    with open(runtime_path("heardback-execution.lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def set_current_turn(tid: str | None, owner: str = "") -> str:
    attempt = uuid.uuid4().hex
    with execution_lock():
        with contextlib.suppress(FileNotFoundError):
            os.unlink(runtime_path("heardback-execution.json"))
        with open(runtime_path(TURN_FILE), "w") as f:
            f.write(tid or "")
        with open(runtime_path("heardback-execution.json"), "w") as f:
            json.dump({"turn": tid, "attempt": attempt, "owner": owner, "allowed": bool(tid)}, f)
    return attempt


def block_turn(tid: str | None, attempt: str | None = None) -> None:
    with execution_lock():
        state = _execution_state(strict_io=True)
        current, current_attempt = state.get("turn"), state.get("attempt")
        if current and current == tid and (attempt is None or current_attempt == attempt):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(runtime_path("heardback-execution.json"))


def _execution_state(*, strict_io: bool = False) -> dict:
    try:
        with open(runtime_path("heardback-execution.json")) as f:
            state = json.load(f)
        if isinstance(state, dict) and state.get("allowed") is True and all(
                isinstance(state.get(key), str) and state[key] for key in ("turn", "attempt")):
            return state
    except FileNotFoundError:
        pass
    except OSError:
        if strict_io:
            raise
    except ValueError:
        pass
    return {}


def execution_snapshot() -> tuple[str, str]:
    """Receipt-time provenance, separate from the caller-supplied trace turn ID."""
    state = _execution_state()
    return state.get("turn", ""), state.get("attempt", "")


def execution_allowed(tid: str, attempt: str) -> bool:
    """Caller holds execution_lock. Missing/malformed managed state fails closed."""
    state = _execution_state()
    owner = os.environ.get("HEARDBACK_EXECUTION_OWNER")
    return bool(isinstance(tid, str) and tid and isinstance(attempt, str) and attempt and owner and
                state.get("owner") == owner and (state.get("turn"), state.get("attempt")) == (tid, attempt))


def current_turn() -> str:
    try:
        with open(runtime_path(TURN_FILE)) as f:
            return f.read().strip()
    except OSError:
        return ""


async def run_jv(argv: list[str], timeout: float = JV_TIMEOUT_S, turn: str | None = None,
                 tainted: bool = False) -> tuple[int, str, str]:
    """Run jv with HEARDBACK_TURN set (its own `run` event carries it); trace a `jv` event for the brain's side."""
    t0 = time.monotonic()
    rc, out, err = await _run_jv(argv, jv_timeout(argv, timeout), turn, tainted)
    trace.emit("brain", "jv", turn, argv=argv, rc=rc, ms=round((time.monotonic() - t0) * 1000),
               stdout=out, stderr=err)
    return rc, out, err


async def _run_jv(argv: list[str], timeout: float, turn: str | None, tainted: bool = False) -> tuple[int, str, str]:
    env = {**os.environ, **({"HEARDBACK_TURN": turn} if turn else {}), **({TAINT_ENV: "1"} if tainted else {})}
    try:
        p = await asyncio.create_subprocess_exec(*argv, stdin=subprocess.DEVNULL, env=env,
                                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        return 1, "", "jv not found"
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        await p.wait()
        return 1, "", "timed out"
    except asyncio.CancelledError:   # daemon shutting down mid-call: no orphan jv
        p.kill()
        await p.wait()
        raise
    return p.returncode, out.decode(errors="replace"), err.decode(errors="replace")
