"""Move the previous installation's private directories before the daemon opens them."""
import os
import re
import shlex
import sys
from pathlib import Path

LEGACY_NAME = "jarvis"


def migrate_dirs() -> None:
    home = Path.home()
    for variable, default in (
        ("XDG_CONFIG_HOME", home / ".config"),
        ("XDG_STATE_HOME", home / ".local/state"),
        ("XDG_DATA_HOME", home / ".local/share"),
        ("XDG_RUNTIME_DIR", Path(f"/run/user/{os.getuid()}")),
    ):
        base = Path(os.environ.get(variable) or default)
        old, new = base / LEGACY_NAME, base / "heardback"
        if old.is_dir() and not new.exists() and not new.is_symlink():
            old.rename(new)


def migrate_hooks(path: Path) -> None:
    """Retarget old absolute jv-hook commands without changing other settings or hooks."""
    from heardback.status.install import _load, _save, _dump

    path = path.resolve()
    settings = _load(path)
    if settings is None:
        return
    changed = False
    for groups in settings.get("hooks", {}).values():
        for group in groups:
            for hook in group.get("hooks", []):
                command = hook.get("command", "")
                if not isinstance(command, str) or LEGACY_NAME not in command:
                    continue
                words = shlex.split(command)
                if words and Path(words[0]).name == "jv-hook" and LEGACY_NAME in Path(words[0]).parts:
                    # Keep shell operators and quoting after the executable verbatim.
                    new = re.sub(r'''^(\s*)(?:"[^"]*"|'[^']*'|\S+)''',
                                 lambda m: m[1] + shlex.quote(str(Path(sys.executable).with_name("jv-hook"))),
                                 command, count=1)
                    if new != command:
                        hook["command"] = new
                        changed = True
    if changed:
        _save(path, _dump(settings), True)
