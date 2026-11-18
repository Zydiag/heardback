"""Fixed, reversible desktop settings with read-back and no shell commands."""
import re
import subprocess

from jv.verb import JvError, Verb
from jv import hypr


def _run(argv):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        raise JvError(1, f"{argv[0]} is unavailable", say=f"I couldn't confirm that setting.")
    if p.returncode:
        raise JvError(1, f"{argv[0]} failed: {(p.stderr or p.stdout).strip()[:160]}",
                      say="I couldn't confirm that setting.")
    return p.stdout.strip()


def _dnd(a):
    flag = {"on": "-dn", "off": "-df", "state": "-D"}[a["state"]]
    out = _run(["swaync-client", flag]).lower()
    if out not in {"true", "false"}:
        raise JvError(1, "swaync-client returned unexpected DND state", say="I couldn't confirm do not disturb.")
    on = out == "true"
    return {"observed": on == (a["state"] == "on") if a["state"] != "state" else True,
            "say": f"Do not disturb is {'on' if on else 'off'}."}


def _hypr(*args, json_out=False):
    try:
        return hypr.hyprctl(*args, json_out=json_out)
    except (JvError, ValueError, TypeError, KeyError):
        raise JvError(1, "unexpected Hyprland output", say="I couldn't confirm that setting.") from None


def _night_temperature():
    out = _hypr("hyprsunset", "temperature").strip()
    if not re.fullmatch(r"\d+", out) or not 1000 <= int(out) <= 20000:
        raise JvError(1, "hyprsunset returned an unexpected temperature", say="I couldn't confirm night light.")
    return int(out)


def _night(a):
    state = a["state"]
    if state == "off":
        _hypr("hyprsunset", "identity")
        # hyprsunset exposes no identity-state query, so the IPC acknowledgement is the strongest read-back.
        return {"say": "Night light off; I couldn't confirm its state."}
    wanted = 4500 if state == "on" else max(1000, _night_temperature() - 500)
    _hypr("hyprsunset", "temperature", str(wanted))
    actual = _night_temperature()
    if actual != wanted:
        raise JvError(1, "hyprsunset temperature read-back did not match", say="I couldn't confirm night light.")
    return {"say": f"Night light is {'on' if state == 'on' else 'at its warmest' if actual == 1000 else 'warmer'} at {actual} kelvin."}


def _power(a):
    if a.get("profile"):
        _run(["powerprofilesctl", "set", a["profile"]])
    profile = _run(["powerprofilesctl", "get"])
    if profile not in {"power-saver", "balanced", "performance"}:
        raise JvError(1, "powerprofilesctl returned an unexpected profile", say="I couldn't confirm the power profile.")
    if a.get("profile") and profile != a["profile"]:
        raise JvError(1, "power profile read-back did not match", say="I couldn't confirm the power profile.")
    return {"observed": not a.get("profile") or profile == a["profile"], "say": f"Power profile is {profile}."}


def _sinks():
    out = _run(["wpctl", "status"])
    section = re.search(r"^[ \t│├└─]*Sinks:[ \t]*\n(.*?)(?=^[ \t│├└─]*[A-Za-z][^\n:]*:[ \t]*$|^\w|\Z)", out, re.M | re.S)
    if not section:
        return []
    return [(m[2], m[3].strip(), m[1] == "*") for m in re.finditer(r"(?m)^[ \t│├└─]*(\*)?\s*(\d+)\.\s+(.+?)(?:\s+\[vol:[^\]]*\])?[ \t]*$", section[1])]


def _outputs(_):
    rows = _sinks()
    if not rows:
        raise JvError(1, "no audio outputs found", say="I couldn't find any audio outputs.")
    return {"say": "Audio outputs are " + ", ".join(name for _, name, _ in rows) + "."}


def _output(a):
    wanted = a["name"].strip().casefold()
    if not wanted:
        raise JvError(1, "empty audio output name", say="I couldn't find one matching audio output.")
    hits = [row for row in _sinks() if wanted in row[1].casefold()]
    if len(hits) != 1:
        raise JvError(1, "audio output name is missing or ambiguous", say="I couldn't find one matching audio output.")
    _run(["wpctl", "set-default", hits[0][0]])
    current = [row for row in _sinks() if row[2]]
    if len(current) != 1 or current[0][0] != hits[0][0]:
        raise JvError(1, "audio output read-back did not match", say="I couldn't confirm the audio output.")
    return {"observed": True, "say": f"Audio output is {current[0][1]}."}


def _mic(a):
    node = "@DEFAULT_AUDIO_SOURCE@"
    def read():
        out = _run(["wpctl", "get-volume", node])
        if not re.fullmatch(r"Volume:\s*[\d.]+(?:\s+\[MUTED\])?", out, re.I):
            raise JvError(1, "wpctl returned an unexpected microphone state", say="I couldn't read the microphone state.")
        return "[MUTED]" in out.upper()
    muted = read()
    wanted = a["state"] == "mute"
    if muted != wanted:
        _run(["wpctl", "set-mute", node, "1" if wanted else "0"])
    actual = read()
    if actual != wanted:
        raise JvError(1, "microphone mute read-back did not match", say="I couldn't confirm the microphone state.")
    return {"observed": actual == wanted, "say": f"Microphone is {'muted' if actual else 'unmuted'}."}


def _window(a):
    prop = {"fullscreen": "fullscreen", "float": "floating"}[a["action"]]
    def read():
        window = _hypr("activewindow", json_out=True)
        if not isinstance(window, dict) or not isinstance(window.get("address"), str) or not re.fullmatch(hypr.ADDR_RE, window["address"]):
            raise JvError(1, "invalid focused window", say="I couldn't find a focused window.")
        state = window.get(prop)
        if (prop == "fullscreen" and (type(state) is not int or state not in (0, 1, 2, 3))
                or prop == "floating" and type(state) is not bool):
            raise JvError(1, "invalid window state", say="I couldn't confirm the window setting.")
        return window
    before = read()
    wanted = 2 if prop == "fullscreen" else True
    if before[prop] == wanted:
        return {"observed": True, "say": f"Window is already {'fullscreen' if prop == 'fullscreen' else 'floating'}."}
    target = f'window = "address:{before["address"]}"'
    operation = (f'hl.dsp.window.fullscreen({{mode = "fullscreen", action = "set", {target}}})'
                 if prop == "fullscreen" else f'hl.dsp.window.float({{action = "set", {target}}})')
    try:
        hypr.dispatch(operation)
    except JvError:
        raise JvError(1, "Hyprland window dispatch failed", say="I couldn't confirm the window setting.") from None
    after = read()
    if after["address"] != before["address"] or after[prop] != wanted:
        raise JvError(1, "focused window changed before read-back", say="I couldn't confirm the window setting.")
    return {"observed": True, "say": f"Window is {'fullscreen' if prop == 'fullscreen' else 'floating'}."}


def _theme(a):
    key = ["org.gnome.desktop.interface", "color-scheme"]
    wanted = "prefer-dark" if a["scheme"] == "dark" else "prefer-light"
    _run(["gsettings", "set", *key, wanted])
    actual = _run(["gsettings", "get", *key]).strip("'")
    if actual not in {"prefer-dark", "prefer-light", "default"}:
        raise JvError(1, "gsettings returned an unexpected color scheme", say="I couldn't confirm the color scheme.")
    return {"observed": actual == wanted, "say": f"Color scheme is {'dark' if actual == 'prefer-dark' else 'light' if actual == 'prefer-light' else 'default'}."}


EMPTY = {"type": "object", "properties": {}}
TEXT = {"type": "string", "maxLength": 80}
VERBS = [
    Verb("settings.dnd", _dnd, "Read or set do not disturb.", {"type": "object", "properties": {"state": {"type": "string", "enum": ["on", "off", "state"]}}, "required": ["state"]}, ["state"], args={"state": {"type": "enum", "choices": ["on", "off", "state"]}}, phrases=["do not disturb status", "turn on do not disturb", "notifications back on"], phrase_args={"do not disturb status": {"state": "state"}, "turn on do not disturb": {"state": "on"}, "notifications back on": {"state": "off"}}, observes=True),
    Verb("settings.night", _night, "Set night light on, off or warmer using hyprsunset.", {"type": "object", "properties": {"state": {"type": "string", "enum": ["on", "off", "warmer"]}}, "required": ["state"]}, ["state"], args={"state": {"type": "enum", "choices": ["on", "off", "warmer"]}}, phrases=["night light on", "night light off", "warmer screen"], phrase_args={"night light on": {"state": "on"}, "night light off": {"state": "off"}, "warmer screen": {"state": "warmer"}}),
    Verb("settings.power", _power, "Set the power profile to power-saver, balanced or performance.", {"type": "object", "properties": {"profile": {"type": "string", "enum": ["power-saver", "balanced", "performance"]}}, "required": ["profile"]}, ["profile"], args={"profile": {"type": "enum", "choices": ["power-saver", "balanced", "performance"]}}, phrases=["power saver", "performance mode", "balanced power profile"], phrase_args={"power saver": {"profile": "power-saver"}, "performance mode": {"profile": "performance"}, "balanced power profile": {"profile": "balanced"}}, observes=True),
    Verb("settings.power_get", _power, "Read the current power profile.", EMPTY, [], phrases=["current power profile", "read my power profile", "power profile status"]),
    Verb("settings.outputs", _outputs, "List available audio output sinks.", EMPTY, [], phrases=["list audio outputs", "available audio outputs", "list audio sinks"]),
    Verb("settings.output", _output, "Switch the default audio output to one uniquely named sink.", {"type": "object", "properties": {"name": TEXT}, "required": ["name"]}, ["name"], args={"name": {"type": "str"}}, phrases=["switch audio to {name:str}", "use {name:str} for audio", "switch to {name:str} speakers"], observes=True),
    Verb("settings.mic", _mic, "Mute or unmute the default microphone.", {"type": "object", "properties": {"state": {"type": "string", "enum": ["mute", "unmute"]}}, "required": ["state"]}, ["state"], args={"state": {"type": "enum", "choices": ["mute", "unmute"]}}, phrases=["mute my mic", "mute the microphone", "unmute my mic"], phrase_args={"mute my mic": {"state": "mute"}, "mute the microphone": {"state": "mute"}, "unmute my mic": {"state": "unmute"}}, observes=True),
    Verb("settings.window", _window, "Make the focused window fullscreen or floating.", {"type": "object", "properties": {"action": {"type": "string", "enum": ["fullscreen", "float"]}}, "required": ["action"]}, ["action"], args={"action": {"type": "enum", "choices": ["fullscreen", "float"]}}, phrases=["make this fullscreen", "fullscreen this window", "float this window"], phrase_args={"make this fullscreen": {"action": "fullscreen"}, "fullscreen this window": {"action": "fullscreen"}, "float this window": {"action": "float"}}, observes=True),
    Verb("settings.theme", _theme, "Set the desktop color scheme to dark or light and read it back.", {"type": "object", "properties": {"scheme": {"type": "string", "enum": ["dark", "light"]}}, "required": ["scheme"]}, ["scheme"], args={"scheme": {"type": "enum", "choices": ["dark", "light"]}}, phrases=["dark mode", "switch to dark mode", "light mode"], phrase_args={"dark mode": {"scheme": "dark"}, "switch to dark mode": {"scheme": "dark"}, "light mode": {"scheme": "light"}}, observes=True),
]
