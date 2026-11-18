"""System controls: volume (wpctl), brightness (brightnessctl), Bluetooth (bluetoothctl). Fixed argv, no shell.

Not `jv/sys.py`: that name reads like the stdlib module. Bluetooth never pairs, trusts, removes or scans.
"""
import re
import subprocess
import time
from contextvars import ContextVar
from functools import wraps

from heardback.config import load as desktop_config
from jv.verb import JvError, Verb, fuzzy

SINK = "@DEFAULT_AUDIO_SINK@"
STEP, BIT = 10, 5            # percent: "volume up" / "volume up a bit"
DIM, BRIGHT_MIN = 20, 5      # "dim the screen"; brightness never goes below this (0 = black screen)
CONNECT_TIMEOUT = 10
POWER_POLL_SECONDS, POWER_POLL_INTERVAL = 4, 0.2
BT_OPERATION_BUDGET = 8
POWER_COMMAND_TIMEOUT = 2.5
BT_READBACK_RESERVE = 2
BT_DEVICE_POLL_SECONDS = 1.5
_BT_DEADLINE = ContextVar("bt_deadline", default=None)


def _run(argv: list[str], timeout: float = 3) -> subprocess.CompletedProcess:
    deadline = _BT_DEADLINE.get()
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            raise JvError(1, f"{' '.join(argv)} exceeded the Bluetooth time budget",
                          say="I couldn't confirm the Bluetooth operation.")
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise JvError(1, f"{argv[0]} is not installed", say=f"{argv[0]} isn't installed.")
    except subprocess.TimeoutExpired:
        raise JvError(1, f"{argv[0]} timed out", say=f"{argv[0]} didn't answer.")


def _ok(argv: list[str], say: str, timeout: float = 3) -> str:
    p = _run(argv, timeout)
    if p.returncode:
        raise JvError(1, f"{' '.join(argv)}: {(p.stderr or p.stdout).strip()[-200:]}", say=say)
    return p.stdout


def _clamp(n: int, lo: int, hi: int = 100) -> int:
    return max(lo, min(hi, n))


def _bounded_bt(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        token = _BT_DEADLINE.set(time.monotonic() + BT_OPERATION_BUDGET)
        try:
            return fn(*args, **kwargs)
        finally:
            _BT_DEADLINE.reset(token)
    return run


# --- volume -----------------------------------------------------------------------------------------
def volume_now() -> tuple[int, bool]:
    out = _ok(["wpctl", "get-volume", SINK], "I couldn't read the volume.")
    m = re.search(r"Volume:\s*([\d.]+)", out)
    if not m:
        raise JvError(1, f"wpctl: unexpected {out!r}", say="I couldn't read the volume.")
    return round(float(m[1]) * 100), "MUTED" in out


def _volume(a: dict) -> dict:
    action, n = a.get("action") or "get", a.get("n")
    level, muted = volume_now()
    if action in ("mute", "unmute"):
        _ok(["wpctl", "set-mute", SINK, "1" if action == "mute" else "0"], f"I couldn't {action}.")
        muted = action == "mute"
    elif action != "get":
        if action == "set" and n is None:
            raise JvError(2, "set needs a level", say="To what level?")
        level = _clamp(n if action == "set" else level + (n or STEP) * (1 if action == "up" else -1), 0)
        _ok(["wpctl", "set-volume", SINK, f"{level / 100:.2f}"], "I couldn't change the volume.")
    expected = level, muted
    try:
        level, muted = volume_now() if action != "get" else expected
    except JvError:
        return {"observed": False}
    say = "muted" if action == "mute" and muted else f"volume {level}{', muted' * muted}"
    return {"level": level, "muted": muted, "observed": (level, muted) == expected, "say": say}


# --- brightness -------------------------------------------------------------------------------------
def brightness_now() -> int:
    out = _ok(["brightnessctl", "-m"], "I couldn't read the brightness.")
    try:   # device,class,current,percent%,max
        _, _, cur, _, mx = out.splitlines()[0].split(",")[:5]
        return round(int(cur) * 100 / int(mx))
    except (IndexError, ValueError, ZeroDivisionError):
        raise JvError(1, f"brightnessctl: unexpected {out!r}", say="I couldn't read the brightness.")


def _brightness(a: dict) -> dict:
    action, n = a.get("action") or "get", a.get("n")
    level = brightness_now()
    if action != "get":
        if action == "set" and n is None:
            raise JvError(2, "set needs a level", say="To what level?")
        level = _clamp(n if action == "set" else level + (n or STEP) * (1 if action == "up" else -1), BRIGHT_MIN)
        _ok(["brightnessctl", "-q", "set", f"{level}%"], "I couldn't change the brightness.")
    expected = level
    try:
        level = brightness_now() if action != "get" else level
    except JvError:
        return {"observed": False}
    return {"level": level, "observed": level == expected, "say": f"brightness {level}"}


# --- bluetooth --------------------------------------------------------------------------------------
def _bt(*args: str, timeout: float = 3, reserve: float = 0) -> subprocess.CompletedProcess:
    deadline = _BT_DEADLINE.get()
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic() - reserve)
        if timeout <= 0:
            raise JvError(1, "Bluetooth operation time budget exhausted",
                          say="I couldn't confirm the Bluetooth operation.")
    return _run(["bluetoothctl", *args], timeout)


def _devices(which: str, timeout: float = 3) -> list[tuple[str, str]]:
    """(mac, name) from `bluetoothctl devices Paired|Connected`."""
    p = _bt("devices", which, timeout=timeout)
    if p.returncode:
        raise JvError(1, f"bluetoothctl devices {which}: {(p.stderr or p.stdout).strip()[-200:]}",
                      say="I couldn't confirm the Bluetooth device state.")
    out = p.stdout
    return [(m[1], m[2].strip()) for m in re.finditer(r"^Device (\S+) (.+)$", out, re.M)]


def _power_status() -> tuple[bool, bool]:
    p = _bt("show")
    if p.returncode:
        raise JvError(1, f"bluetoothctl show: {(p.stderr or p.stdout).strip()[-200:]}",
                      say="I couldn't confirm Bluetooth's power state.")
    match = re.search(r"^\s*Powered:\s*(yes|no)\s*$", p.stdout, re.M | re.I)
    if not match:
        raise JvError(1, f"bluetoothctl show: unexpected power state {p.stdout!r}",
                      say="I couldn't confirm Bluetooth's power state.")
    return match[1].lower() == "yes", "off-blocked" in p.stdout


def _powered() -> bool:
    return _power_status()[0]


def device(name: str) -> tuple[str, str]:
    paired = _devices("Paired")
    if not paired:
        raise JvError(1, "no paired devices", say="No Bluetooth devices are paired.")
    q = re.sub(r"^(?:to |my |the )+", "", name.strip().lower())
    q = desktop_config().aliases.get(q, q)
    names = [n for _, n in paired]
    try:
        hit = fuzzy(q, names)
    except JvError as e:
        if e.code == 2:
            c = e.extra["candidates"]
            raise JvError(2, e.msg, candidates=c, say=f"Which one: {', '.join(c[:-1])} or {c[-1]}?")
        raise JvError(1, e.msg, say=f"No paired Bluetooth device called {name}.")
    return next(p for p in paired if p[1] == hit)


def _sink() -> tuple[str, str]:
    """(bluez address or "", description) of the default audio sink. Read-only."""
    out = _run(["wpctl", "inspect", SINK]).stdout
    addr = re.search(r'api\.bluez5\.address = "([^"]+)"', out)
    desc = re.search(r'node\.description = "([^"]+)"', out)
    return (addr[1] if addr else ""), (desc[1] if desc else "the speakers")


def _audio_moved(macs: set[str]) -> str:
    """After a disconnect/off that took the default sink: where audio plays now. PipeWire switches in a moment."""
    for _ in range(10):   # ponytail: ~2 s poll; a pw-dump subscription if it's ever slower
        deadline = _BT_DEADLINE.get()
        if deadline is not None and time.monotonic() >= deadline:
            return ""
        addr, desc = _sink()
        if addr not in macs:
            return f" Audio is on {desc} now."
        time.sleep(min(0.2, max(0, deadline - time.monotonic())) if deadline is not None else 0.2)
    return ""


def _wait_device(mac: str, connected: bool) -> bool:
    deadline = min(time.monotonic() + BT_DEVICE_POLL_SECONDS, _BT_DEADLINE.get())
    first_read = True
    while first_read or time.monotonic() < deadline:
        if time.monotonic() >= _BT_DEADLINE.get():
            return False
        remaining = deadline - time.monotonic()
        if not first_read and remaining < 0.1:
            return False
        first_read = False
        try:
            rows = _devices("Connected", timeout=min(1, max(0.001, remaining)))
        except JvError:   # one slow poll read is "not observed yet", not a failure of the whole operation
            rows = None
        if rows is not None and (mac in dict(rows)) == connected:
            return True
        if time.monotonic() < deadline:
            time.sleep(min(POWER_POLL_INTERVAL, deadline - time.monotonic()))
    return False


@_bounded_bt
def _power(a: dict) -> dict:
    on = a["state"] == "on"
    try:
        powered, is_blocked = _power_status()
    except JvError as e:
        raise JvError(1, e.msg, say=f"I couldn't confirm Bluetooth is {a['state']}.")
    if powered == on:
        word = "on" if on else "off"
        return {"powered": on, "observed": True, "say": f"Bluetooth is already {word}."}
    unblocked = False
    if on and is_blocked:   # rfkill soft block: power on alone fails
        _run(["rfkill", "unblock", "bluetooth"])
        unblocked = True
    addr = _sink()[0] if not on else ""
    busy = False
    attempts = 2 if unblocked else 1
    for attempt in range(attempts):
        try:
            p = _bt("power", "on" if on else "off", timeout=POWER_COMMAND_TIMEOUT,
                    reserve=BT_READBACK_RESERVE)
            busy = "org.bluez.Error.Busy" in (p.stderr + p.stdout)
        except JvError:
            busy = False
        poll_deadline = min(time.monotonic() + POWER_POLL_SECONDS, _BT_DEADLINE.get())
        state_seen, latest_blocked, latest_read_valid = False, False, False
        first_read = True
        while True:
            if time.monotonic() >= _BT_DEADLINE.get():
                break
            if not first_read and time.monotonic() >= poll_deadline:
                break
            first_read = False
            try:
                actual, latest_blocked = _power_status()
                state_seen, latest_read_valid = True, True
                if actual == on:
                    try:
                        moved = _audio_moved({addr}) if addr else ""
                    except JvError:
                        moved = ""
                    return {"powered": on, "observed": True, "say": f"Bluetooth {a['state']}.{moved}"}
            except JvError:
                latest_read_valid = False
            if time.monotonic() >= poll_deadline:
                break
            time.sleep(min(POWER_POLL_INTERVAL, max(0, poll_deadline - time.monotonic())))
        if not (busy and unblocked and attempt == 0):
            detail = ("blocked" if state_seen and latest_read_valid and latest_blocked else
                      "busy" if state_seen and latest_read_valid and busy else "could not confirm")
            raise JvError(1, f"bluetoothctl power {a['state']}: {detail}",
                          say=f"Bluetooth is still blocked." if detail == "blocked" else
                          f"Bluetooth is busy; I couldn't turn it {a['state']}." if detail == "busy" else
                          f"I couldn't confirm Bluetooth is {a['state']}.")
    moved = _audio_moved({addr}) if addr else ""
    return {"powered": on, "say": f"Bluetooth {a['state']}.{moved}"}


@_bounded_bt
def _connect(a: dict) -> dict:
    mac, name = device(a["name"])
    if not _powered():
        raise JvError(1, "bluetooth is off", say="Bluetooth is off. Say bluetooth on first.")
    if mac in dict(_devices("Connected")):
        return {"device": name, "observed": True, "say": f"{name} is already connected."}
    command_error = None
    try:
        _bt("connect", mac, timeout=CONNECT_TIMEOUT, reserve=BT_READBACK_RESERVE)
    except JvError as e:
        command_error = e
    connected = _wait_device(mac, True)
    if not connected:
        say = (f"I couldn't reach {name}. Is it on?" if command_error and command_error.msg.endswith("timed out")
               else f"I couldn't connect {name}. Is it on and nearby?")
        raise JvError(1, f"connect {name}: requested state not observed", say=say)
    return {"device": name, "observed": True, "say": f"{name} connected."}


@_bounded_bt
def _disconnect(a: dict) -> dict:
    mac, name = device(a["name"])
    if mac not in dict(_devices("Connected")):
        return {"device": name, "observed": True, "say": f"{name} isn't connected."}
    was_sink = _sink()[0] == mac
    try:
        _bt("disconnect", mac, timeout=5, reserve=BT_READBACK_RESERVE)
    except JvError:
        pass
    if not _wait_device(mac, False):
        raise JvError(1, f"disconnect {name} did not reach the requested state",
                      say=f"I couldn't disconnect {name}.")
    try:
        moved = _audio_moved({mac}) if was_sink else ""
    except JvError:
        moved = ""
    return {"device": name, "observed": True, "say": f"{name} disconnected.{moved}"}


@_bounded_bt
def _status(a: dict) -> dict:
    conn = _devices("Connected")
    if a.get("name"):
        mac, name = device(a["name"])
        yes = mac in dict(conn)
        return {"device": name, "connected": yes, "say": f"{name} is {'' if yes else 'not '}connected."}
    if not _powered():
        return {"connected": [], "say": "Bluetooth is off."}
    names = [n for _, n in conn]
    say = ("Nothing is connected." if not names else
           f"{' and '.join(names)} {'is' if len(names) == 1 else 'are'} connected.")
    return {"connected": names, "say": say}


# --- verbs ------------------------------------------------------------------------------------------
def _obj(props: dict, required: list[str] = ()) -> dict:
    return {"type": "object", "properties": props, "required": list(required)}


N = {"type": "integer", "minimum": 0, "maximum": 1000}   # clamped in the verb (150 -> 100), not refused
PCT = "( percent)?"
_VOL = {
    **{p: {"action": "up"} for p in ["volume up", "turn volume up", "turn it up", "turn up volume", "louder",
                                     "increase volume", "raise volume"]},
    **{p: {"action": "down"} for p in ["volume down", "turn volume down", "turn it down", "turn down volume",
                                       "quieter", "lower volume", "decrease volume"]},
    **{p: {"action": "up", "n": BIT} for p in ["volume up a bit", "turn it up a bit", "a bit louder", "louder a bit",
                                               "turn volume up a bit"]},
    **{p: {"action": "down", "n": BIT} for p in ["volume down a bit", "turn it down a bit", "a bit quieter",
                                                 "quieter a bit", "turn volume down a bit"]},
    **{p: {"action": "set"} for p in [f"volume {{n:int}}{PCT}", f"set volume (to )?{{n:int}}{PCT}",
                                      f"volume to {{n:int}}{PCT}", f"turn volume to {{n:int}}{PCT}"]},
    "mute": {"action": "mute"}, "mute (audio|sound|volume|speakers)": {"action": "mute"},
    "unmute": {"action": "unmute"}, "unmute (audio|sound|volume|speakers)": {"action": "unmute"},
    "whats volume": {"action": "get"}, "what is volume": {"action": "get"}, "hows volume": {"action": "get"},
}
_BRI = {
    **{p: {"action": "up"} for p in ["brightness up", "brighter", "turn brightness up", "increase brightness",
                                     "brighter screen", "make screen brighter", "raise brightness"]},
    **{p: {"action": "down"} for p in ["brightness down", "dimmer", "turn brightness down", "decrease brightness",
                                       "lower brightness", "make screen dimmer"]},
    **{p: {"action": "set"} for p in [f"brightness {{n:int}}{PCT}", f"set brightness (to )?{{n:int}}{PCT}",
                                      f"brightness to {{n:int}}{PCT}"]},
    **{p: {"action": "set", "n": DIM} for p in ["dim screen", "dim display", "dim my screen"]},
    **{p: {"action": "set", "n": 100} for p in ["full brightness", "max brightness", "brightness full",
                                                "brightness max"]},
    "whats brightness": {"action": "get"}, "what is brightness": {"action": "get"},
}
_POW = {**{p: {"state": "on"} for p in ["bluetooth on", "turn on bluetooth", "turn bluetooth on", "enable bluetooth"]},
        **{p: {"state": "off"} for p in ["bluetooth off", "turn off bluetooth", "turn bluetooth off",
                                         "disable bluetooth"]}}


def _alts(table: dict) -> dict:
    """Keep the "(a|b)" groups literal for phrase_args: expand them into one phrase each."""
    out = {}
    for p, v in table.items():
        m = re.search(r"\((\w+(?:\|\w+)+)\)", p)
        out |= {p[:m.start()] + w + p[m.end():]: v for w in m[1].split("|")} if m else {p: v}
    return out


_VOL, _BRI = _alts(_VOL), _alts(_BRI)
NAME = {"name": {"type": "string", "maxLength": 80}}
VERBS = [
    Verb("volume", _volume,
         "Speaker volume of the default audio output (wpctl): up/down (n = step, default 10), set (n = level 0-100), "
         "mute, unmute, get. Says the new level.",
         _obj({"action": {"type": "string", "enum": ["up", "down", "set", "mute", "unmute", "get"]}, "n": N},
              ["action"]), ["action", "n"], observes=True,
         args={"action": {"type": "enum", "choices": ["up", "down", "set", "mute", "unmute", "get"]},
               "n": {"type": "int"}}, phrases=list(_VOL), phrase_args=_VOL),
    Verb("brightness", _brightness,
         "Screen brightness (brightnessctl): up/down (n = step, default 10), set (n = percent; never under 5), get. "
         "'Dim the screen' is set 20.",
         _obj({"action": {"type": "string", "enum": ["up", "down", "set", "get"]}, "n": N}, ["action"]),
         ["action", "n"], observes=True, args={"action": {"type": "enum", "choices": ["up", "down", "set", "get"]},
                                "n": {"type": "int"}}, phrases=list(_BRI), phrase_args=_BRI),
    Verb("bt.power", _power, "Turn Bluetooth on or off. Off drops connected devices; says where audio goes.",
         _obj({"state": {"type": "string", "enum": ["on", "off"]}}, ["state"]), ["state"],
         args={"state": {"type": "enum", "choices": ["on", "off"]}}, phrases=list(_POW), phrase_args=_POW, observes=True),
    Verb("bt.connect", _connect, "Connect an already-paired Bluetooth device by name ('buds', 'speaker', 'JBL').",
         _obj(NAME, ["name"]), ["name"], args={"name": {"type": "str"}},
         phrases=["connect (to )?(my )?{name:str}", "connect (my )?{name:str} to bluetooth",
                  "connect (my )?{name:str} over bluetooth"], observes=True),
    Verb("bt.disconnect", _disconnect, "Disconnect a Bluetooth device by name; says where audio goes if it was the "
         "output.", _obj(NAME, ["name"]), ["name"], args={"name": {"type": "str"}},
         phrases=["disconnect (my )?{name:str}"], observes=True),
    Verb("bt.status", _status, "Is a Bluetooth device connected (name), or what is connected (no name).",
         _obj(NAME), ["name"], args={"name": {"type": "str"}},
         phrases=["is (my )?{name:str} connected", "are (my )?{name:str} connected", "whats connected", "what is connected",
                  "whats connected to bluetooth", "whats connected on bluetooth",
                  "what bluetooth devices are connected"]),
]
