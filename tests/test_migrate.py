import json
import sys
from pathlib import Path

import pytest

from heardback.migrate import LEGACY_NAME, migrate_dirs, migrate_hooks
from heardback.status import store


@pytest.mark.parametrize("explicit", [False, True])
def test_migrate_dirs_preserves_data_and_modes(tmp_path, monkeypatch, explicit):
    monkeypatch.setenv("HOME", str(tmp_path))
    bases = [tmp_path / p for p in (".config", ".local/state", ".local/share", "runtime")]
    variables = ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR")
    for i, (variable, base) in enumerate(zip(variables, bases)):
        if explicit or i == 3:
            monkeypatch.setenv(variable, str(base))
        else:
            monkeypatch.delenv(variable, raising=False)
        old = base / LEGACY_NAME
        old.mkdir(mode=0o700, parents=True)
        (old / "saved").write_bytes(b"notes, reminders, sessions and models")
    migrate_dirs()
    migrate_dirs()
    for base in bases:
        assert not (base / LEGACY_NAME).exists()
        new = base / "heardback"
        assert (new / "saved").read_bytes() == b"notes, reminders, sessions and models"
        assert new.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("destination", ["absent", "directory", "file", "symlink"])
def test_migrate_dirs_never_overwrites(tmp_path, monkeypatch, destination):
    # All roots point at scratch storage, including data (the root fixture leaves it inherited).
    for variable in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
        monkeypatch.setenv(variable, str(tmp_path))
    old, new = tmp_path / LEGACY_NAME, tmp_path / "heardback"
    if destination == "absent":
        migrate_dirs()
        assert not new.exists()
        return
    old.mkdir()
    (old / "saved").write_text("keep")
    if destination == "directory":
        new.mkdir()
    elif destination == "file":
        new.write_text("keep new")
    else:
        new.symlink_to(tmp_path / "missing")
    before = new.lstat()
    migrate_dirs()
    assert (old / "saved").read_text() == "keep"
    assert new.lstat() == before


def test_old_brain_records_stay_internal():
    assert store.is_internal({"cwd": f"/old/state/{LEGACY_NAME}/brain-cwd"})


def test_daemon_migrates_before_starting(monkeypatch):
    from heardback import daemon

    calls = []
    monkeypatch.setattr(daemon, "migrate_dirs", lambda: calls.append("migrate"))
    async def main():
        calls.append("start")
    monkeypatch.setattr(daemon, "_main", main)
    daemon.main()
    assert calls == ["migrate", "start"]


def test_hook_migration_only_changes_old_hook_paths(tmp_path):
    path = tmp_path / "settings.json"
    old = f"/tools/{LEGACY_NAME}/bin/jv-hook"
    commands = [old, f'"{old}" && echo "keep quoting"', "jv-hook", "/tools/heardback/bin/jv-hook",
                f"echo {LEGACY_NAME}", f"/tools/{LEGACY_NAME}/bin/other"]
    settings = {"model": "opus", "hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": command, "timeout": 2} for command in commands]}]}}
    path.write_text(json.dumps(settings))
    original = path.read_bytes()
    path.chmod(0o640)
    link = tmp_path / "linked.json"
    link.symlink_to(path)
    migrate_hooks(link)
    result = json.loads(path.read_text())
    hooks = result["hooks"]["Stop"][0]["hooks"]
    new = str(Path(sys.executable).with_name("jv-hook"))
    assert [h["command"] for h in hooks] == [new, f'{new} && echo "keep quoting"', *commands[2:]]
    assert result["model"] == "opus" and all(h["timeout"] == 2 for h in hooks)
    assert path.stat().st_mode & 0o777 == 0o640 and link.is_symlink()
    assert next(tmp_path.glob("settings.json.bak-heardback-*")).read_bytes() == original
    before = path.stat()
    migrate_hooks(path)
    assert path.stat() == before


def test_hook_migration_missing_or_invalid_settings(tmp_path):
    from jv.verb import JvError

    path = tmp_path / "settings.json"
    migrate_hooks(path)
    assert not path.exists()
    path.write_text("broken json")
    with pytest.raises(JvError, match="not touched"):
        migrate_hooks(path)
    assert path.read_text() == "broken json"
