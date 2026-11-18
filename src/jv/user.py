"""`jv do …`: user actions from actions.toml (spec 05). The loader never raises."""
import contextlib
import importlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path

from jv import cli, say, selftest
from jv.selftest import SLOT
from jv.verb import JvError, Verb, runtime_path

NAME = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
SLOT_NAME = re.compile(r"^[a-z][a-z_]{0,20}$")
PLACEHOLDER = re.compile(r"\{(\w+)\}")
RESERVED = {"check", "draft", "accept"}
KEYS = {"name", "description", "phrases", "confirm", "background", "timeout_s", "say", "done_say", "fail_say",
        "confirm_say", "run", "args"}
ARG_KEYS = {"type", "max", "min", "choices", "optional", "dash"}
TYPES = {"int", "str", "enum", "text"}
SAYS = ("say", "done_say", "fail_say", "confirm_say")
TIMEOUTS = {False: (10, 60), True: (600, 900)}   # background -> (default, cap)
SHELLS = {"sh", "bash", "zsh", "fish", "dash", "env", "sudo", "doas", "pkexec", "su", "xargs", "setsid", "nohup",
          "timeout", "nice", "ionice", "stdbuf", "flock", "chrt", "taskset", "systemd-run", "uwsm"}
INTERP = re.compile(r"^(python[\d.]*|perl|ruby|node)$")
INTERP_FLAGS = {"-c", "-e"}
DRAFT_BANNED = SHELLS | {"rm", "dd", "shred", "chmod", "chown", "chattr", "systemctl"}


def actions_path() -> Path:
    if p := os.environ.get("HEARDBACK_ACTIONS"):
        return Path(p)
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "heardback" / "actions.toml"


def drafts_dir() -> Path:
    return actions_path().parent / "drafts"


def builtins() -> dict[str, Verb]:
    return {v.name: v for m in cli.MODULES if m != "jv.user" for v in importlib.import_module(m).VERBS}


def _strs(x) -> bool:
    return isinstance(x, list) and all(isinstance(s, str) and s for s in x)


def _check_step(step: list[str], e: dict, slots: set, draft: bool, verbs: dict) -> None:
    if step[0] == "jv":
        if len(step) < 3:
            raise ValueError("jv step needs a domain and a verb")
        if step[1] == "do":
            raise ValueError("do steps are not allowed")
        name = step[1] if step[1] in verbs and "." not in step[1] else f"{step[1]}.{step[2]}"
        if name not in verbs:
            raise ValueError(f"unknown jv verb {name}")
        if verbs[name].confirm is not False and e["confirm"] is not True:
            raise ValueError(f"step {name} needs a yes, so the action must be confirm = true")
        return
    base = os.path.basename(step[0])
    if draft and (base in DRAFT_BANNED or base.startswith("mkfs") or INTERP.match(base)):
        raise ValueError(f"{step[0]} is not allowed in a draft")
    if base in SHELLS or (INTERP.match(base) and INTERP_FLAGS.intersection(step)):
        if any(k in slots for s in step for k in PLACEHOLDER.findall(s)):
            raise ValueError(f"{base} step can't take a slot")
    if shutil.which(step[0]) is None:
        raise ValueError(f"{step[0]} not found")


def parse_entry(raw: dict, *, draft: bool = False, verbs: dict | None = None) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("not a table")
    for k in raw:
        if k not in KEYS:
            raise ValueError(f"unknown key {k}")
    name = raw.get("name")
    if not isinstance(name, str) or not NAME.match(name) or name in RESERVED:
        raise ValueError("bad name")
    bg = raw.get("background", False)
    e = {"phrases": [], "confirm": True, "background": bg, "timeout_s": TIMEOUTS[bg is True][0], "args": {},
         **{k: "" for k in ("description", *SAYS)}, **raw}
    for k in ("confirm", "background"):
        if not isinstance(e[k], bool):
            raise ValueError(f"{k} must be true or false")
    for k in ("description", *SAYS):
        if not isinstance(e[k], str):
            raise ValueError(f"{k} must be a string")
    if not e["description"].strip():
        raise ValueError("description is empty")
    run = e.get("run")
    if not isinstance(run, list) or not run or not all(_strs(s) and s for s in run):
        raise ValueError("run must be a list of non-empty argv lists of strings")
    if not _strs(e["phrases"]):
        raise ValueError("phrases must be a list of strings")
    cap = TIMEOUTS[bg][1]
    if type(e["timeout_s"]) is not int or not 1 <= e["timeout_s"] <= cap:
        raise ValueError(f"timeout_s must be a whole number from 1 to {cap}")
    if not isinstance(e["args"], dict):
        raise ValueError("args must be tables")
    seen_optional = False
    for i, (k, a) in enumerate(e["args"].items()):
        if not SLOT_NAME.match(k):
            raise ValueError(f"bad slot name {k}")
        if not isinstance(a, dict):
            raise ValueError(f"args.{k} must be a table")
        for ak in a:
            if ak not in ARG_KEYS:
                raise ValueError(f"unknown key {ak}")
        t = a.get("type")
        if t not in TYPES:
            raise ValueError(f"args.{k} type must be int, str, enum or text")
        if t == "enum" and not (_strs(a.get("choices")) and a["choices"]):
            raise ValueError(f"args.{k} enum needs a choices list")
        if t == "text" and i != len(e["args"]) - 1:
            raise ValueError(f"args.{k} text must be the last slot")
        for n in ("min", "max"):
            if n in a and type(a[n]) is not int:
                raise ValueError(f"args.{k} {n} must be a whole number")
        for n in ("optional", "dash"):
            if not isinstance(a.get(n, False), bool):
                raise ValueError(f"args.{k} {n} must be true or false")
        if a.get("optional"):
            seen_optional = True
        elif seen_optional:
            raise ValueError(f"args.{k} is required but follows an optional slot")
    slots = set(e["args"])
    for p in e["phrases"]:
        for k, t in SLOT.findall(p):
            if e["args"].get(k, {}).get("type") != t:
                raise ValueError(f"phrase slot {{{k}:{t}}} does not match args")
    for s in (x for step in e["run"] for x in step):
        for k in PLACEHOLDER.findall(s):
            if k not in slots and k != "home":
                raise ValueError(f"unknown placeholder {{{k}}} in run")
    for f in SAYS:
        for k in PLACEHOLDER.findall(e[f]):
            if k not in slots | {"home", "out", "err"}:
                raise ValueError(f"unknown placeholder {{{k}}} in {f}")
    verbs = builtins() if verbs is None else verbs
    for step in e["run"]:
        _check_step(step, e, slots, draft, verbs)
    return e


def load(text: str, *, draft: bool = False) -> tuple[list[dict], list[str]]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return [], [f"actions.toml: {e}"]
    raws = data.get("action", [])
    if not isinstance(raws, list):
        return [], ["actions.toml: action must be [[action]] entries"]
    entries, probs, names, verbs = [], [], set(), builtins()
    for i, raw in enumerate(raws, 1):
        n = raw.get("name") if isinstance(raw, dict) else None
        label = n if isinstance(n, str) and NAME.match(n) else f"action #{i}"
        try:
            e = parse_entry(raw, draft=draft, verbs=verbs)
        except ValueError as err:
            probs.append(f"{label}: {err}")
            continue
        if e["name"] in names:
            probs.append(f"{e['name']}: duplicate name")
            continue
        names.add(e["name"])
        entries.append(e)
    return entries, probs


def fill(template: str, values: dict) -> str:
    return PLACEHOLDER.sub(lambda m: str(values.get(m[1], m[0])), template)


def step_argv(step: list[str], values: dict) -> list[str]:
    # A value fills one element and never splits; an absent optional slot drops its bare element.
    return [fill(s, values) for s in step if not ((m := PLACEHOLDER.fullmatch(s)) and m[1] not in values)]


def last_line(text: str) -> str:
    return next((ln.strip() for ln in reversed(text.splitlines()) if ln.strip()), "")


def _jv_verb_len(step: list[str]) -> int:
    """How many argv words name the verb: `jv say …` → 1, `jv tmux select …` → 2."""
    return 1 if "." not in step[1] and step[1] in builtins() else 2


def run_step(argv: list[str], timeout: float, yes: bool) -> tuple[int, str, str]:
    if argv[0] == "jv":
        n = 1 + _jv_verb_len(argv)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.run(argv[1:n] + ["--json"] + (["--yes"] if yes else []) + argv[n:],
                         verbs=list(builtins().values()))
        try:
            res = json.loads(last_line(out.getvalue()) or "{}")
        except ValueError:
            res = {}
        if rc == 0:
            return 0, res.get("say", ""), ""
        return rc, "", res.get("error") or res.get("confirm") or last_line(err.getvalue())
    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, cwd=Path.home(), process_group=0)
    except FileNotFoundError:
        return 1, "", f"{argv[0]} not found"
    except OSError as e:
        return 1, "", f"{argv[0]}: {e.strerror}"
    try:
        out, err = p.communicate(timeout=max(timeout, 0.01))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, _ = p.communicate()
        return 1, out, "timed out"
    return p.returncode, out, err


def _values(args: dict) -> dict:
    return {**{k: str(v) for k, v in args.items()}, "home": str(Path.home())}


def run_action(e: dict, args: dict) -> tuple[int, str, str]:
    values, deadline = _values(args), time.monotonic() + e["timeout_s"]
    rc, out, err = 0, "", ""
    for step in e["run"]:
        rc, out, err = run_step(step_argv(step, values), deadline - time.monotonic(), e["confirm"] is True)
        if rc != 0:
            break
    return rc, out, err


def log_run(name: str, rc: int, secs: float, out: str = "", err: str = "", bg: bool = False) -> None:
    line = {"name": name, "rc": rc, "s": round(secs, 1), "bg": bg}
    if bg:
        line |= {"out": out.splitlines()[-5:], "err": err.splitlines()[-5:]}
    log_run_line(line)


def log_run_line(line: dict) -> None:
    line = {"t": datetime.now().astimezone().isoformat(timespec="seconds"), **line}
    try:
        p = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "heardback" / "actions.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps(line) + "\n")
    except OSError:
        pass


def make_check(e: dict):
    def check(a: dict) -> str | None:
        for k, v in a.items():
            if not isinstance(v, str):
                continue
            if any(c < " " or c == "\x7f" for c in v):
                raise JvError(2, f"{k} has control characters")
            if v.startswith("-") and not e["args"].get(k, {}).get("dash"):
                raise JvError(2, f"{k} starts with a dash")
        if e["confirm"] is not True:
            return None
        if s := fill(e["confirm_say"], _values(a)):
            return s
        vals = ", ".join(str(a[k]) for k in e["args"] if k in a)
        return f"Run {e['name']} with {vals}?" if vals else f"Run {e['name']}?"
    return check


def pidfile(name: str) -> Path:
    return Path(runtime_path("heardback")) / f"do-{name}.pid"


def alive(name: str) -> bool:
    try:
        os.kill(int(pidfile(name).read_text().strip()), 0)
    except (OSError, ValueError):
        return False
    return True


def announce(text: str) -> None:
    try:
        say.say({"text": text})
    except JvError:
        log_run_line({"say_failed": text})


def _start_bg(e: dict, a: dict) -> dict:
    name = e["name"]
    if alive(name):
        raise JvError(4, "Still on the last one.")
    p = subprocess.Popen([sys.executable, "-m", "jv.user", "bg", name, json.dumps(a)], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         cwd=Path.home())
    pf = pidfile(name)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(str(p.pid))
    return {"say": fill(e["say"], _values(a)) or f"Starting {name}.", "background": True, "pid": p.pid}


def bg_main(name: str, args_json: str) -> int:
    try:
        try:
            e = next((x for x in load(actions_path().read_text())[0] if x["name"] == name), None)
        except OSError:
            e = None
        if e is None:
            text, rc = f"{name} is gone from your actions file.", 1
        else:
            a, t = json.loads(args_json), time.monotonic()
            rc, out, err = run_action(e, a)
            log_run(name, rc, time.monotonic() - t, out, err, bg=True)
            values = {**_values(a), "name": name, "out": last_line(out), "err": last_line(err)}
            if rc == 0:
                text = fill(e["done_say"] or "{out}", values) or f"{name} done."
            else:
                text = fill(e["fail_say"] or "{name} failed: {err}", values)
    finally:
        pf = pidfile(name)
        try:
            if pf.read_text().strip() == str(os.getpid()):
                pf.unlink()
        except (OSError, ValueError):
            pass
    announce(text)
    return 1 if rc else 0


def make_fn(e: dict):
    name = e["name"]

    def fn(a: dict) -> dict:
        if e["background"]:
            return _start_bg(e, a)
        t = time.monotonic()
        rc, out, err = run_action(e, a)
        log_run(name, rc, time.monotonic() - t)
        values = _values(a)
        if rc == 0:
            o = last_line(out)
            return {"say": fill(e["say"], {**values, "out": o}) or o or "Done.", "out": o}
        er = last_line(err)
        raise JvError(1, fill(e["fail_say"], {**values, "err": er}) or er or f"{name} failed")
    return fn


def to_verb(e: dict) -> Verb:
    props, req, args = {}, [], {}
    for k, a in e["args"].items():
        t = a["type"]
        if t == "int":
            props[k] = {"type": "integer", **({"minimum": a["min"]} if "min" in a else {}),
                        **({"maximum": a["max"]} if "max" in a else {})}
        elif t == "enum":
            props[k] = {"type": "string", "enum": list(a["choices"])}
        else:
            props[k] = {"type": "string", "maxLength": a.get("max", 200)}
        args[k] = {"type": t, **({"choices": list(a["choices"])} if t == "enum" else {})}
        if not a.get("optional"):
            req.append(k)
    return Verb(f"do.{e['name']}", make_fn(e), e["description"],
                {"type": "object", "properties": props, "required": req}, list(e["args"]),
                confirm=e["confirm"], check=make_check(e), args=args, phrases=list(e["phrases"]))


def _key(phrase: str) -> str:
    return " ".join(phrase.lower().split())


def build() -> tuple[list[Verb], list[str]]:
    path = actions_path()
    if not path.exists():
        return list(FIXED), []
    entries, probs = load(path.read_text())
    out, owner = [], {_key(p): v.name for v in builtins().values() for p in v.phrases}
    for e in entries:
        v = to_verb(e)
        if ps := selftest.problems(cli.catalogue([v])):
            probs += ps
            continue
        for p in v.phrases:
            if (o := owner.setdefault(_key(p), v.name)) != v.name:
                probs.append(f"{e['name']}: phrase '{p}' also used by {o}")
        out.append(v)
    return out + FIXED, probs


def _live_names() -> set[str]:
    try:
        return {e["name"] for e in load(actions_path().read_text())[0]}
    except OSError:
        return set()


def _one_entry(text: str, name: str) -> dict:
    entries, probs = load(text, draft=True)
    if probs:
        raise JvError(2, probs[0])
    if len(entries) != 1:
        raise JvError(2, "a draft is exactly one [[action]] entry")
    if entries[0]["name"] != name:
        raise JvError(2, f"the draft is named {entries[0]['name']}, not {name}")
    if name in _live_names():
        raise JvError(2, f"{name} already exists")
    return entries[0]


def draft(a: dict) -> dict:
    name, text = a["name"], a["toml"]
    if len(text) > 4000:
        raise JvError(2, "toml too long")
    _one_entry(text, name)
    d = drafts_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.toml").write_text(text)
    return {"say": f"Drafted {name}. Say 'add {name}' to make it live."}


def draft_names() -> list[str]:
    d = drafts_dir()
    return sorted(p.stem for p in d.glob("*.toml")) if d.is_dir() else []


def readback(e: dict) -> str:
    when = f"when you say {' or '.join(f"'{p}'" for p in e['phrases'])}, " if e["phrases"] else ""
    steps = ", then ".join(" ".join(step) for step in e["run"])
    ask = "" if e["confirm"] is True else ", without asking first"
    return f"New command {e['name']}: {when}it runs {steps}{ask}. Add it?"


def _draft_entry(name: str) -> tuple[dict, str]:
    f = drafts_dir() / f"{name}.toml"
    try:
        text = f.read_text()
    except OSError:
        raise JvError(1, f"no draft called {name}")
    return _one_entry(text, name), text


def accept_check(a: dict) -> str:
    return readback(_draft_entry(a["name"])[0])


def accept(a: dict) -> dict:
    name = a["name"]
    _, text = _draft_entry(name)
    path = actions_path()
    bak = path.with_name(path.name + ".bak")
    old = path.read_text() if path.exists() else None
    if old is not None:
        shutil.copy2(path, bak)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = (old or "") + ("\n" if old and not old.endswith("\n") else "") + text
    path.write_text(new)
    entries, probs = load(new)
    if any(p.startswith(f"{name}:") for p in probs) or name not in {e["name"] for e in entries}:
        if old is None:
            path.unlink()
        else:
            shutil.copy2(bak, path)
        raise JvError(1, "The new actions file didn't load, so I put the old one back.")
    (drafts_dir() / f"{name}.toml").unlink()
    return {"say": f"Added {name}."}


DRAFT_DESC = ("Draft a new voice command for the user. toml = one [[action]] entry: name, description, phrases "
              "(e.g. \"lofi\" or \"play {song:text}\"), run = list of argv lists (no shells, no rm/sudo), optional "
              "confirm (default true), [action.args.<slot>] type = int|str|enum|text. Inert until the user says "
              "'add <name>'.")


def check_cmd(a: dict) -> dict:
    if not PROBLEMS:
        n = sum(v not in FIXED for v in VERBS)
        return {"say": f"{n} actions, no problems."}
    k = len(PROBLEMS)
    raise JvError(1, f"Your actions file has {k} problem{'s' * (k > 1)}: " + "; ".join(PROBLEMS), problems=PROBLEMS)


FIXED = [
    Verb("do.check", check_cmd, "Check the user's actions file and say what is wrong with it.",
         {"type": "object", "properties": {}}, [], phrases=["check my actions"]),
    Verb("do.draft", draft, DRAFT_DESC,
         {"type": "object", "properties": {"name": {"type": "string", "pattern": NAME.pattern},
                                           "toml": {"type": "string", "maxLength": 4000}},
          "required": ["name", "toml"]},
         ["name", "toml"], args={"toml": {"type": "text"}}),
    Verb("do.accept", accept, "Make a drafted command live (asks the user first).",
         {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}, ["name"],
         confirm=True, check=accept_check, args={"name": {"type": "enum", "choices": draft_names}},
         phrases=["add {name:enum}"]),
]

try:
    VERBS, PROBLEMS = build()
except Exception as e:   # the loader never breaks jv
    VERBS, PROBLEMS = list(FIXED), [f"actions loader crashed: {e}"]

if __name__ == "__main__" and sys.argv[1:2] == ["bg"]:
    sys.exit(bg_main(sys.argv[2], sys.argv[3]))
