import asyncio
import itertools
import json
import os
import shutil
import subprocess
import tempfile

os.environ["JV_TMUX_SOCKET"] = f"jvtest-none-{os.getpid()}"

import pytest


@pytest.fixture(autouse=True)
def _safe_env(monkeypatch, tmp_path_factory):
    create_subprocess_exec = asyncio.create_subprocess_exec
    async def safe_exec(program, *args, **kwargs):
        if os.path.basename(os.fsdecode(program)) == "systemd-inhibit":
            raise OSError("systemd-inhibit is forbidden in tests")
        return await create_subprocess_exec(program, *args, **kwargs)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", safe_exec)
    # Never touch the live tmux server, Hyprland, the real sockets, session store or ml4w scripts.
    monkeypatch.setenv("JV_DRY", "1")
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("state")))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path_factory.mktemp("claude")))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("config")))
    rt = tempfile.mkdtemp(prefix="jvrt-", dir="/tmp")  # short: unix socket paths cap at 108 bytes
    monkeypatch.setenv("XDG_RUNTIME_DIR", rt)
    monkeypatch.setenv("JV_TMUX_SOCKET", f"jvtest-none-{os.getpid()}")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/nonexistent")   # playerctl never sees real players
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    yield safe_exec
    shutil.rmtree(rt, ignore_errors=True)


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    d = tmp_path / "heardback" / "sessions"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def runtime_dir(monkeypatch):
    d = tempfile.mkdtemp(prefix="jvrt-", dir="/tmp")  # short: unix socket paths cap at 108 bytes
    monkeypatch.setenv("XDG_RUNTIME_DIR", d)
    yield d
    shutil.rmtree(d, ignore_errors=True)


_servers = itertools.count()


@pytest.fixture
def tmux_server(monkeypatch):
    # Unique per test: kill-server returns before the old server is gone.
    name = f"jvtest-{os.getpid()}-{next(_servers)}"
    subprocess.run(["tmux", "-L", name, "-f", "/dev/null", "new-session", "-d", "-s", "0",
                    "-n", "first", "-x", "200", "-y", "50"], check=True)
    monkeypatch.setenv("JV_TMUX_SOCKET", name)
    yield lambda *a: subprocess.run(["tmux", "-L", name, *a], capture_output=True, text=True, check=True).stdout
    subprocess.run(["tmux", "-L", name, "kill-server"], capture_output=True)


@pytest.fixture
def run_jv(capsys, monkeypatch):
    from jv.cli import run as cli_run
    from jv import desktop
    # These verb tests mock desktop calls; they do not require installed optional integrations.
    monkeypatch.setattr(desktop, "unavailable", lambda name: None)

    def run(*argv):
        code = cli_run(list(argv))
        out, err = capsys.readouterr()
        return code, json.loads(out) if "--json" in argv and out else out, err
    return run


@pytest.fixture
def run_jv_with(capsys):
    from jv.cli import run as cli_run

    def run(verbs, *argv):
        code = cli_run(list(argv), verbs=verbs)
        out, err = capsys.readouterr()
        return code, json.loads(out) if "--json" in argv and out else out, err
    return run


# Files where per-file cost is >= 2 s (measured with --durations); keep out of scripts/test-fast.sh.
SLOW_FILES = {"test_zen.py", "test_dash.py", "test_loop.py", "test_personal_claude.py", "test_interrupt.py",
              "test_jv_media.py", "test_runfile.py", "test_coordinator.py", "test_manual_memory.py",
              "test_ask_user.py", "test_sysctl.py", "test_router.py", "test_confirm.py", "test_personal_fixes.py",
              "test_status_ask.py", "test_brain_claude.py", "test_injectgate.py", "test_memory.py", "test_tts.py"}


def pytest_collection_modifyitems(items):
    for item in items:
        if item.path.name in SLOW_FILES:
            item.add_marker(pytest.mark.slow)
        if "tmux_server" in item.fixturenames:
            item.add_marker(pytest.mark.tmux)
