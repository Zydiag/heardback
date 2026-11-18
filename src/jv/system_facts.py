"""Read-only system facts from fixed local commands."""
import json
import re
import subprocess

from jv.verb import JvError, Verb


def _run(argv, *, allow=()):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=8)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        raise JvError(1, f"{argv[0]} is unavailable", say=f"I couldn't read {argv[0]}.")
    if p.returncode not in (0, *allow):
        raise JvError(1, f"{argv[0]} failed: {(p.stderr or p.stdout).strip()[:160]}", say=f"I couldn't read {argv[0]}.")
    return p


def _fail(label):
    raise JvError(1, f"unexpected {label} output", say=f"I couldn't read {label}.")


def _battery(_):
    out = _run(["upower", "-i", "/org/freedesktop/UPower/devices/DisplayDevice"]).stdout
    if re.search(r"present:\s*no|no battery", out, re.I):
        return {"say": "This computer has no battery."}
    percent = re.search(r"percentage:\s*(\d+)%", out, re.I)
    state = re.search(r"state:\s*(charging|discharging|fully-charged|pending-charge|pending-discharge)", out, re.I)
    if not percent or not state:
        _fail("battery")
    return {"say": f"Battery is {percent[1]} percent, {state[1].replace('-', ' ')}."}


def _disk(_):
    rows = _run(["df", "-B1", "/"]).stdout.splitlines()
    if len(rows) < 2:
        _fail("disk")
    fields = rows[-1].split()
    if len(fields) < 6 or not fields[4].endswith("%"):
        _fail("disk")
    try:
        total, used = int(fields[1]), int(fields[2])
    except ValueError:
        _fail("disk")
    if total <= 0 or used < 0:
        _fail("disk")
    return {"say": f"Root disk is {used * 100 // total} percent used."}


def _memory(_):
    rows = _run(["free", "-b"]).stdout.splitlines()
    row = next((line.split() for line in rows if line.startswith("Mem:")), None)
    if not row or len(row) < 3:
        _fail("memory")
    try:
        total, used = int(row[1]), int(row[2])
    except ValueError:
        _fail("memory")
    if total <= 0 or used < 0:
        _fail("memory")
    return {"say": f"Memory is {used * 100 // total} percent used."}


def _temperature(_):
    out = _run(["sensors"]).stdout
    values = [float(x) for x in re.findall(r"(?m)^[^\n:]+:[ \t]*([+-]?\d+(?:\.\d+)?)[ \t]*°C\b", out)]
    if not values:
        _fail("temperature")
    return {"say": f"The hottest reported temperature is {round(max(values))} degrees Celsius."}


def _gpu(_):
    out = _run(["nvidia-smi", "--query-gpu=name,temperature.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"]).stdout
    rows = [line.strip().split(",") for line in out.splitlines() if line.strip()]
    if not rows or len(rows[0]) != 4:
        _fail("GPU")
    name, temp, used, total = (x.strip() for x in rows[0])
    try:
        temp, used, total = int(temp), int(used), int(total)
    except ValueError:
        _fail("GPU")
    if not name or total <= 0 or used < 0:
        _fail("GPU")
    return {"say": f"{name} is {temp} degrees Celsius, using {used} of {total} megabytes."}


def _updates(_):
    p = _run(["checkupdates"], allow=(2,))
    rows = [line for line in p.stdout.splitlines() if line.strip()]
    if p.returncode == 2 and not rows:
        return {"say": "No updates are available."}
    if p.returncode == 0 and not rows:
        _fail("updates")
    if any(not re.match(r"^[^\s]+\s+\S+\s+->\s+\S+", line) for line in rows):
        _fail("updates")
    return {"say": f"{len(rows)} updates are available."}


def _tailscale(_):
    out = _run(["tailscale", "status", "--json"]).stdout
    try:
        state = json.loads(out)["BackendState"]
    except (ValueError, KeyError, TypeError):
        _fail("Tailscale")
    if not isinstance(state, str) or state not in {"Running", "Stopped", "NeedsLogin", "NeedsMachineAuth", "Starting", "NoState"}:
        _fail("Tailscale")
    return {"say": f"Tailscale is {state.replace('Needs', 'needs ').lower()}."}


def _online(_):
    state = _run(["nmcli", "-t", "-f", "CONNECTIVITY", "general"]).stdout.strip()
    if state not in {"full", "limited", "portal", "none", "unknown"}:
        _fail("network")
    return {"say": {"full": "You are online.", "limited": "You are connected, but internet access is limited.",
                     "portal": "You are behind a sign-in page.", "none": "You are offline.",
                     "unknown": "I couldn't determine whether you are online."}[state]}


def _verb(name, fn, phrase, description):
    return Verb(name, fn, description, {"type": "object", "properties": {}}, [], phrases=phrase)


VERBS = [
    _verb("system.battery", _battery, ["how much battery is left", "what's my battery", "battery status"], "Read battery charge and state."),
    _verb("system.disk", _disk, ["how full is my disk", "disk space", "root disk usage"], "Read root filesystem use."),
    _verb("system.memory", _memory, ["how much memory is used", "memory usage", "how is my memory"], "Read memory use."),
    _verb("system.temperature", _temperature, ["how hot is my computer", "computer temperature", "system temperature"], "Read the hottest reported sensor."),
    _verb("system.gpu", _gpu, ["what's my GPU temperature", "GPU status", "how is my GPU"], "Read NVIDIA GPU temperature and memory."),
    _verb("system.updates", _updates, ["are there updates", "any updates available", "check for updates"], "Read available package updates."),
    _verb("system.tailscale", _tailscale, ["is tailscale up", "tailscale status", "is my tailnet connected"], "Read Tailscale status."),
    _verb("system.online", _online, ["am I online", "is the internet working", "check my connection"], "Read NetworkManager connectivity state."),
]
