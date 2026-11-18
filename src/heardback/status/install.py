"""Add / remove our hook entries in Claude Code's settings.json and our hypridle listener, leaving everything else alone."""
import copy
import json
import os
import re
import shutil
import time
from pathlib import Path

from heardback.migrate import LEGACY_NAME
from heardback.status.away import AWAY_AFTER_S
from jv.verb import JvError

CMD = "jv-hook"
_ASYNC = [{"hooks": [{"type": "command", "command": CMD, "async": True}]}]
# Stop is sync (unlike the spec): an async Stop is lost when claude exits right after the reply (Task 5 probe).
HOOKS = {
    "SessionStart": _ASYNC,
    "UserPromptSubmit": _ASYNC,
    "Notification": [{"matcher": "permission_prompt|elicitation_dialog|agent_needs_input|idle_prompt", **_ASYNC[0]}],
    "PostToolUse": [{"matcher": "*", **_ASYNC[0]}],
    "Stop": [{"hooks": [{"type": "command", "command": CMD, "timeout": 2}]}],
    "StopFailure": _ASYNC,
    "SessionEnd": [{"hooks": [{"type": "command", "command": CMD, "timeout": 2}]}],
}


def _ours(h: object) -> bool:
    return isinstance(h, dict) and h.get("command") == CMD


def _load(path: Path) -> dict | None:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    try:
        d = json.loads(text)
    except ValueError:
        d = None
    if not isinstance(d, dict):
        raise JvError(1, f"{path.name} is not valid JSON; not touched")
    if not _shape_ok(d.get("hooks", {})):
        raise JvError(1, f"{path.name} has an unexpected hooks layout; not touched")
    return d


def _shape_ok(hooks: object) -> bool:
    """{event: [{"hooks": [{...}, ...], ...}, ...]}: anything else we refuse to edit."""
    if not isinstance(hooks, dict) or not all(isinstance(gs, list) for gs in hooks.values()):
        return False
    groups = [g for gs in hooks.values() for g in gs]
    return all(isinstance(g, dict) and isinstance(g.get("hooks", []), list)
               and all(isinstance(h, dict) for h in g.get("hooks", [])) for g in groups)


def _save(path: Path, text: str, existed: bool) -> Path | None:
    backup = None
    if existed:
        backup = path.with_name(f"{path.name}.bak-heardback-{time.strftime('%Y%m%d-%H%M%S')}")
        if not backup.exists():  # same second: keep the older, more original copy
            shutil.copy2(path, backup)
    mode = os.stat(path).st_mode & 0o777 if existed else 0o600
    tmp = path.with_name(f".{path.name}.heardback.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return backup


def _strip(hooks: dict, keep=()) -> None:
    """Remove our handlers from every group; drop groups left empty, and events left empty unless in `keep`."""
    for ev in list(hooks):
        groups = []
        for g in hooks[ev]:
            left = [h for h in g.get("hooks", []) if not _ours(h)]
            if left != g.get("hooks", []):
                if not left:
                    continue
                g = {**g, "hooks": left}
            groups.append(g)
        if groups != hooks[ev]:
            if groups or ev in keep:
                hooks[ev] = groups
            else:
                del hooks[ev]


def install_hooks(path: Path) -> Path | None:
    path = path.resolve()  # a dotfiles symlink stays a symlink
    d = _load(path)
    existed, d = d is not None, d or {}
    before = copy.deepcopy(d)
    hooks = d.setdefault("hooks", {})
    _strip(hooks, HOOKS)
    for ev, groups in HOOKS.items():
        hooks[ev] = hooks.get(ev, []) + copy.deepcopy(groups)
    if existed and d == before:
        return None
    return _save(path, _dump(d), existed)


def uninstall_hooks(path: Path) -> Path | None:
    path = path.resolve()
    d = _load(path)
    if d is None or "hooks" not in d:
        return None
    hooks = d["hooks"]
    before = copy.deepcopy(hooks)
    _strip(hooks)
    if hooks == before:
        return None
    if not hooks:
        del d["hooks"]
    return _save(path, _dump(d), True)


def check_hooks(path: Path) -> None:
    """Raise the JvError install/uninstall_hooks would, without writing."""
    _load(path.resolve())


def _dump(d: dict) -> str:
    return json.dumps(d, indent=2) + "\n"


IDLE_BLOCK = f"""# heardback-away begin (managed by jv status install-hooks --idle)
listener {{
    timeout = {AWAY_AFTER_S}
    on-timeout = touch $XDG_RUNTIME_DIR/heardback-away
    on-resume = rm -f $XDG_RUNTIME_DIR/heardback-away
}}
# heardback-away end
"""
_IDLE_RE = re.compile(rf"\n?# (?P<name>{LEGACY_NAME}|heardback)-away begin.*?# (?P=name)-away end\n?", re.S)


def _idle_text(conf: Path) -> str | None:
    """The conf without our block; None if the file is missing."""
    try:
        text = conf.read_text()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        raise JvError(1, f"{conf.name} is unreadable; not touched")
    base = _IDLE_RE.sub("", text, count=1)
    if any(f"# {name}-away" in base for name in (LEGACY_NAME, "heardback")):
        raise JvError(1, f"{conf.name} has a broken heardback-away block; not touched")
    return base


def install_idle(conf: Path) -> Path | None:
    """Append our away listener (once). Returns the backup, or None if it was already there."""
    conf = conf.resolve()
    base = _idle_text(conf)
    if base is None:
        raise JvError(1, "hypridle.conf not found")
    new = base + "\n" + IDLE_BLOCK
    return None if new == conf.read_text() else _save(conf, new, True)


def uninstall_idle(conf: Path) -> Path | None:
    conf = conf.resolve()
    base = _idle_text(conf)
    return None if base is None or base == conf.read_text() else _save(conf, base, True)


def check_idle(conf: Path) -> None:
    _idle_text(conf.resolve())
