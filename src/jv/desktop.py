"""Optional desktop integrations: presence checks, without contacting the desktop."""
import shutil
from pathlib import Path

from jv.verb import JvError


def unavailable(name: str) -> str | None:
    domain = name.partition(".")[0]
    if domain == "hypr" or name in {"settings.night", "settings.window"}:
        if not shutil.which("hyprctl"):
            return "hyprctl is absent; install Hyprland to use this module"
    elif domain == "wall":
        from jv.wall import SCRIPT
        if not shutil.which(str(Path(SCRIPT).expanduser())):
            return "ml4w-wallpaper is absent; install the ML4W wallpaper module"
    elif domain == "zen" and name != "zen.install":
        if not (shutil.which("zen-browser") or shutil.which("zen")):
            return "Zen browser is absent; install zen-browser or zen on PATH"
    return None


def require(name: str) -> None:
    if reason := unavailable(name):
        message = f"not available on this desktop: {reason}"
        raise JvError(1, message, status="not available on this desktop", reason=reason, say=message)
