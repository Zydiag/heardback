"""Read-only prerequisite checks; never inspect Claude credential files."""
import contextlib
import io
import json
import re
import shutil
import subprocess
import sys

from heardback import config
from heardback.voice import config as voice_config
from heardback.voice.extra import INSTALL, default_device, missing
from jv import desktop

# Runtime integration was tested with this version; newer versions remain usable with a warning.
TESTED_CLAUDE_RANGE = ((2, 1, 312), (2, 1, 327))


def _command(*argv: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None


def run() -> int:
    failed = False

    def check(name, status, detail, fix="", *, required=False):
        nonlocal failed
        failed |= required and status == "missing"
        line = f"{status} {name}: {detail}" + (f"; fix: {fix}" if status != "ok" else "")
        print(" ".join(line.split()))

    check("python", "ok" if sys.version_info >= (3, 12) else "missing",
          sys.version.split()[0], "install Python 3.12+", required=True)
    tmux = _command("tmux", "-V")
    check("tmux", "ok" if tmux is not None and tmux.returncode == 0 else "missing",
          "required; installed" if tmux is not None and tmux.returncode == 0 else "required; not found or unusable",
          "install tmux and put it on PATH", required=True)
    pactl = _command("pactl", "info")
    pipewire = pactl is not None and pactl.returncode == 0 and "pipewire" in pactl.stdout.lower()
    check("pipewire", "ok" if pipewire else "missing", "required; pactl connected to PipeWire" if pipewire
          else "required; pactl cannot confirm PipeWire", "install PipeWire and pactl (pulseaudio-utils on Debian); start your PipeWire user session",
          required=True)

    claude = _command("claude", "--version")
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", claude.stdout) if claude and claude.returncode == 0 else None
    version = tuple(map(int, match.groups())) if match else None
    tested = TESTED_CLAUDE_RANGE[0] <= version <= TESTED_CLAUDE_RANGE[1] if version else False
    tested_text = "–".join(dict.fromkeys(".".join(map(str, v)) for v in TESTED_CLAUDE_RANGE))
    check("claude", "ok" if tested else "warn" if version else "missing",
          f"{match[0]}" + (f" (untested; tested {tested_text})" if not tested else "") if match
          else "not found or version unreadable",
          f"install Claude Code {tested_text} on PATH" if not version else "other versions usually work; report an issue if one breaks",
          required=True)
    auth = _command("claude", "auth", "status") if version else None
    try:
        info = json.loads(auth.stdout) if auth and auth.returncode == 0 else {}
        logged_in = isinstance(info, dict) and info.get("loggedIn") is True
    except ValueError:
        logged_in = False
    check("claude-login", "ok" if logged_in else "missing", "Claude reports logged in" if logged_in
          else "Claude did not report logged in", "run claude auth login with your Claude subscription", required=True)

    try:
        config.load()
        with contextlib.redirect_stdout(io.StringIO()) as warnings:
            cfg = voice_config.load()
    except (OSError, ValueError) as e:
        check("config", "missing", f"cannot parse settings ({type(e).__name__})",
              f"correct {config.config_path()} and {voice_config.config_path()}", required=True)
        cfg = None
    else:
        check("config", "warn" if warnings.getvalue() else "ok",
              "voice.toml has ignored settings" if warnings.getvalue() else "config.toml and voice.toml valid (absent files use defaults)",
              f"remove unknown keys from {voice_config.config_path()}")
    wake = voice_config.data_dir() / "models" / "hey_claude.onnx"
    check("wake", "ok", "wake model found" if wake.is_file() else "push-to-talk; send SIGUSR1 to heardbackd to speak")
    absent = missing()
    check("voice", "warn" if absent else "ok", "optional; missing " + ", ".join(absent) if absent
          else "voice extra installed", INSTALL)
    device = default_device() if not absent else "cpu"
    wanted = cfg.stt_device if cfg else device
    status = "warn" if absent else "missing" if wanted == "cuda" and device != "cuda" else "ok"
    check("voice-device", status, f"{wanted.upper()} configured; {device.upper()} available" if not absent
          else "voice extra absent; default CPU", INSTALL if absent else 'set stt_device = "cpu" in voice.toml',
          required=not absent)
    for label, verb in (("hyprland", "hypr.list"), ("wallpaper", "wall.current"), ("zen", "zen.list")):
        reason = desktop.unavailable(verb)
        check(f"optional-{label}", "warn" if reason else "ok", "not available on this desktop" if reason else "found", reason or "")
    kitty = bool(shutil.which("kitty"))
    check("optional-kitty", "ok" if kitty else "warn", "found" if kitty else "absent; tmux still works",
          "install kitty for automatic terminal focus")
    return int(failed)
