"""Exercise the cutover with fake commands; never contact systemd or the installed tool."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from heardback.migrate import LEGACY_NAME

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "scripts/migrate-service.sh").is_file():
    pytest.skip("developer migration script isn't included in the public export", allow_module_level=True)


@pytest.mark.parametrize("install_ok", [True, False])
def test_cutover_order_and_install_failure(tmp_path, monkeypatch, install_ok):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copytree(ROOT / "systemd", repo / "systemd")
    script = repo / "scripts/migrate-service.sh"
    shutil.copy2(ROOT / "scripts/migrate-service.sh", script)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "commands"
    monkeypatch.setenv("TEST_REPO", str(repo))
    monkeypatch.setenv("TEST_LOG", str(log))
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("PYTHONPATH", str(ROOT / "src"))
    tool_dir = tmp_path / "tools/heardback/bin"
    tool_dir.mkdir(parents=True)
    (tool_dir / "python").symlink_to(sys.executable)
    programs = {
        bin_dir / "git": 'case "$3" in rev-parse) echo "$TEST_REPO";; show) cat "$TEST_REPO/${4#*:}";; *) exit 1;; esac',
        bin_dir / "systemctl": 'echo "systemctl $*" >> "$TEST_LOG"',
        bin_dir / "uv": f'case "$*" in "tool dir") echo "{tmp_path}/tools";; '
                        '"tool uninstall "*) echo "uv $*" >> "$TEST_LOG";; *) exit 1;; esac',
        repo / "scripts/install-snapshot.sh": 'echo "install $*" >> "$TEST_LOG"\n' +
                                               f'exit {0 if install_ok else 1}',
    }
    for path, body in programs.items():
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
    settings = Path.home() / ".claude/settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": f"/tools/{LEGACY_NAME}/bin/jv-hook"}]}]}}))
    result = subprocess.run(["bash", str(script), "cutover"], capture_output=True, text=True, timeout=10)
    expected = [f"systemctl --user disable --now {LEGACY_NAME}.service",
                f"systemctl --user stop {LEGACY_NAME}-down.service", f"uv tool uninstall {LEGACY_NAME}",
                "install cutover"]
    if install_ok:
        expected += ["systemctl --user daemon-reload", "systemctl --user enable --now heardback.service"]
    assert result.returncode == (0 if install_ok else 1), result.stderr
    assert log.read_text().splitlines() == expected
    units = Path(os.environ["XDG_CONFIG_HOME"]) / "systemd/user"
    if install_ok:
        assert (units / "heardback.service").read_bytes() == (ROOT / "systemd/heardback.service").read_bytes()
        assert (units / "heardback-down.service").exists()
        assert json.loads(settings.read_text())["hooks"]["Stop"][0]["hooks"][0]["command"] == str(tool_dir / "jv-hook")
    else:
        assert not units.exists()
        assert LEGACY_NAME in settings.read_text()
