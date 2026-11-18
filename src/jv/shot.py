"""Private screenshots, captured and copied by grimblast."""
import contextlib
import os
import signal
import stat
import subprocess
import time
from pathlib import Path

from heardback.status.store import state_dir
from jv.verb import JvError, Verb
from jv.say import say as speak

CAP = 20


def _directory(create: bool = True) -> Path:
    path = state_dir(create=create) / "shots"
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    elif not path.exists() and not path.is_symlink():
        return path
    if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
        raise JvError(1, "unsafe screenshot directory", say="I couldn't take the screenshot.")
    return path


def _files() -> list[Path]:
    try:
        d = _directory(False)
        return [p for p in d.glob("*.png") if not p.is_symlink() and p.is_file()]
    except FileNotFoundError:
        return []


def latest() -> Path:
    files = _files()
    if not files:
        raise JvError(4, "no screenshot is available", say="Take a screenshot first.")
    return max(files, key=lambda p: (p.stat().st_mtime_ns, p.name))


def _prune() -> None:
    files = sorted(_files(), key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
    for path in files[CAP:]:
        path.unlink()


def _capture(a: dict) -> dict:
    try:
        locked = subprocess.run(["pgrep", "-x", "hyprlock"], capture_output=True, timeout=2)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        raise JvError(1, "could not check screen lock", say="I couldn't take the screenshot.") from None
    if locked.returncode == 0:
        raise JvError(4, "screen is locked", say="I can't take a screenshot while the screen is locked.")
    if locked.returncode != 1:
        raise JvError(1, "could not check screen lock", say="I couldn't take the screenshot.")
    path = None
    try:
        directory = _directory()
        path = directory / f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns()}.png"
        target = {"screen": "screen", "window": "active", "area": "area"}[a["target"]]
        if target == "area":
            try:
                speak({"text": "Select the area."})
            except JvError:
                pass
        # Not PIPE: wl-copy's clipboard server inherits the fds and holds them open, so communicate() waited 30 s (live 17:38).
        proc = subprocess.Popen(["grimblast", "copysave", target, str(path)], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            stdout, stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise
        result = subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
        if result.returncode or not path.is_file():
            if path.exists():
                path.unlink()
            if target == "area" and result.returncode:
                raise JvError(1, "area selection cancelled", say="Area selection cancelled.")
            raise JvError(1, "grimblast failed", say="I couldn't take the screenshot.")
        if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
            raise JvError(1, "grimblast did not create a regular file", say="I couldn't take the screenshot.")
        os.chmod(path, 0o600, follow_symlinks=False)
        _prune()
        return {"say": "Screenshot copied."}
    except JvError:
        raise
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        if path is not None:
            path.unlink(missing_ok=True)
        raise JvError(1, "screenshot capture failed", say="I couldn't take the screenshot.") from None


VERBS = [Verb("shot", _capture, "Take a screenshot and copy it to the clipboard.",
              {"type": "object", "properties": {"target": {"type": "string", "enum": ["screen", "window", "area"]}},
               "required": ["target"]}, ["target"], args={"target": {"type": "enum", "choices": ["screen", "window", "area"]}},
              phrases=["take a screenshot of the screen", "screenshot the screen", "capture the screen",
                       "take a screenshot of this window", "screenshot this window", "capture this window",
                       "take a screenshot of an area", "screenshot an area", "capture an area"],
              phrase_args={"take a screenshot of the screen": {"target": "screen"},
                           "screenshot the screen": {"target": "screen"}, "capture the screen": {"target": "screen"},
                           "take a screenshot of this window": {"target": "window"},
                           "screenshot this window": {"target": "window"}, "capture this window": {"target": "window"},
                           "take a screenshot of an area": {"target": "area"}, "screenshot an area": {"target": "area"},
                           "capture an area": {"target": "area"}})]
