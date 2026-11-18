"""SIGUSR1/SIGUSR2 during startup (before voice installs its handlers) must not kill heardbackd."""
import os
import signal
import subprocess
import sys
import time

import pytest


def test_usr_signals_ignored_after_importing_daemon():
    code = "import heardback.daemon, os, signal, time\nprint('up', flush=True)\ntime.sleep(5)"
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "up"
        for s in (signal.SIGUSR1, signal.SIGUSR2):
            os.kill(p.pid, s)
        time.sleep(0.3)
        assert p.poll() is None, f"died with {p.returncode}"
    finally:
        p.kill()
        p.wait()


@pytest.mark.parametrize("crash", [True, False], ids=["voice-crashes", "voice-returns"])
def test_dead_voice_exits_nonzero_for_systemd_restart(monkeypatch, crash):
    pytest.importorskip("numpy")
    import asyncio

    from heardback import daemon
    from heardback.voice import daemon as vd

    async def boom(*a):
        if crash:
            raise RuntimeError("no mic")

    def exit_process(code):
        raise SystemExit(code)

    monkeypatch.setattr(vd, "run", boom)
    monkeypatch.setattr(daemon.os, "_exit", exit_process)
    with pytest.raises(SystemExit) as exc:
        asyncio.run(daemon._voice("test-voice-token"))
    assert exc.value.code == 1
