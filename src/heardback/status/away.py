"""Away (idle 3 min per hypridle, or locked) and do-not-disturb for announcements."""
import json
import os
import subprocess
from pathlib import Path

from heardback.status import store
from jv.verb import runtime_path

AWAY_AFTER_S = 180


def flag_path() -> Path:
    return Path(runtime_path("heardback-away"))  # touched / removed by our hypridle listener


def is_away() -> bool:
    if flag_path().exists():
        return True
    try:
        return subprocess.run(["pgrep", "-x", "hyprlock"], capture_output=True, timeout=1).returncode == 0
    except (OSError, subprocess.SubprocessError):  # no pgrep, or it hung: not locked
        return False


def _mute_file() -> Path:
    return store.state_dir() / "mute.json"


def mute(minutes: int | None, now: float) -> str:
    until = "forever" if minutes is None else store.now_iso(now + minutes * 60)
    p = _mute_file()
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps({"muted_until": until}))
    os.replace(tmp, p)
    return until


def unmute() -> None:
    _mute_file().unlink(missing_ok=True)


def is_muted(now: float) -> bool:
    try:
        until = json.loads(_mute_file().read_text())["muted_until"]
        return until == "forever" or store.epoch(until) > now
    except (OSError, ValueError, TypeError, KeyError):
        return False
