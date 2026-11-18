"""Wallpaper verbs: drive the user's ml4w wallpaper script (it re-themes too)."""
import os
import subprocess
import tempfile
from pathlib import Path

from jv.verb import JvError, Verb, fuzzy

# Expanded at call time (_p), never at import, so a test HOME is always honoured.
SCRIPT = "~/.config/ml4w/scripts/ml4w-wallpaper"
APP = "~/.config/ml4w/scripts/ml4w-wallpaper-app"
APP_SETTING = "~/.config/ml4w/settings/wallpaper-app"
FOLDER_FILE = "~/.config/ml4w/settings/wallpaper-folder"   # holds "$HOME/Pictures/..."
CURRENT_FILE = "~/.cache/ml4w/hyprland-dotfiles/current_wallpaper"
EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
PICKER_TIMEOUT = 2


def _p(s: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(s.strip())))


def folder() -> Path:
    try:
        return _p(_p(FOLDER_FILE).read_text())
    except OSError:
        raise JvError(1, "no wallpaper folder set")


def images() -> list[Path]:
    f = folder()
    imgs = sorted(p for p in f.iterdir() if p.is_file() and p.suffix.lower() in EXTS) if f.is_dir() else []
    if not imgs:
        raise JvError(1, "wallpaper folder empty")
    return imgs


def stems() -> list[str]:
    try:
        return [p.stem for p in images()]
    except JvError:
        return []


def current() -> Path | None:
    try:
        s = _p(CURRENT_FILE).read_text().strip()
    except OSError:
        return None
    return Path(s) if s else None


def apply(img: Path | None) -> None:
    argv = [str(_p(SCRIPT)), str(img) if img else "--random"]
    # No pipes: a backgrounded child of the script holding one would block us until timeout.
    with tempfile.TemporaryFile() as err:
        try:   # waits: the script re-themes (~1-2 s)
            r = subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=err, timeout=60)
        except subprocess.TimeoutExpired:   # its str() holds the argv
            raise JvError(1, "wallpaper script timed out")
        except OSError as e:
            raise JvError(1, f"wallpaper script: {e}")
        if r.returncode:
            err.seek(0)
            raise JvError(1, f"wallpaper script failed: {err.read().decode(errors='replace').strip()[-200:]}")


def _current(a: dict) -> dict:
    c = current()
    if not c:
        raise JvError(1, "no current wallpaper")
    return {"path": str(c), "name": c.stem, "say": f"wallpaper is {c.stem}"}


def _next(a: dict) -> dict:
    imgs, c = images(), current()
    img = imgs[(imgs.index(c) + 1) % len(imgs)] if c in imgs else imgs[0]
    apply(img)
    return {"path": str(img), "observed": current() == img, "say": f"wallpaper {img.stem}"}


def _random(a: dict) -> dict:
    apply(None)
    return {"say": "random wallpaper"}


def _set(a: dict) -> dict:
    imgs = {p.stem: p for p in images()}
    img = imgs[fuzzy(a["name"], list(imgs))]
    apply(img)
    return {"path": str(img), "observed": current() == img, "say": f"wallpaper {img.stem}"}


def _picker_app() -> str:
    try:
        return _p(APP_SETTING).read_text().rstrip("\n")
    except FileNotFoundError:
        return "quickshell"
    except OSError as e:
        raise JvError(1, f"wallpaper app setting: {e}")


def _qs_call(method: str) -> str:
    env = os.environ.copy()
    if env.get("WAYLAND_DISPLAY") and "QT_QPA_PLATFORM" not in env:
        env["QT_QPA_PLATFORM"] = "wayland;xcb"
    try:
        r = subprocess.run(["qs", "ipc", "call", "wallpaper", method],
                           capture_output=True, text=True, timeout=PICKER_TIMEOUT, env=env)
    except FileNotFoundError:
        raise JvError(1, "quickshell IPC command not found")
    except subprocess.TimeoutExpired:
        raise JvError(1, f"quickshell wallpaper {method} timed out")
    except OSError as e:
        raise JvError(1, f"quickshell IPC: {e}")
    if r.returncode:
        detail = (r.stderr or r.stdout).strip()
        raise JvError(1, f"quickshell wallpaper {method} failed: {detail or r.returncode}")
    return r.stdout.strip()


def _show_waypaper() -> dict:
    # shortcut: Waypaper visibility is unobservable; this confirms launch only, until it exposes a readiness signal.
    with tempfile.TemporaryFile() as err:
        try:
            p = subprocess.Popen([str(_p(APP))], start_new_session=True, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=err)
        except OSError as e:
            raise JvError(1, f"wallpaper picker launch: {e}")
        try:
            code = p.wait(timeout=1)
        except subprocess.TimeoutExpired:
            return {"say": "wallpaper picker launching"}
        if code:
            err.seek(0)
            detail = err.read().decode(errors="replace").strip()[-200:]
            raise JvError(1, f"wallpaper picker launch failed: {detail or code}")
    return {"say": "wallpaper picker launch requested"}


def _show(a: dict) -> dict:
    app = _picker_app()
    if app == "quickshell":
        _qs_call("open")
        if _qs_call("isOpen") != "true":
            raise JvError(1, "quickshell wallpaper picker did not confirm open")
        return {"say": "wallpaper picker open"}
    if app == "waypaper":
        return _show_waypaper()
    raise JvError(1, f"unsupported wallpaper app: {app}")


def _obj(props: dict, required: list[str] = ()) -> dict:
    return {"type": "object", "properties": props, "required": list(required)}


VERBS = [
    Verb("wall.current", _current, "Say which wallpaper is set.", _obj({}), [], phrases=["what's my wall"]),
    Verb("wall.next", _next, "Switch to the next wallpaper in the folder (re-themes).", _obj({}), [],
         phrases=["next wall", "change (my )?wall", "switch to next wall", "change to next wall"], observes=True),
    Verb("wall.random", _random, "Switch to a random wallpaper (re-themes).", _obj({}), [],
         phrases=["random wall"]),
    Verb("wall.set", _set, "Set a wallpaper by name (re-themes).",
         _obj({"name": {"type": "string"}}, ["name"]), ["name"],
         args={"name": {"type": "enum", "choices": stems}}, phrases=["set wall {name:enum}"], observes=True),
    Verb("wall.show", _show, "Open the wallpaper picker.", _obj({}), [], phrases=["show walls", "open wall picker"]),
]
