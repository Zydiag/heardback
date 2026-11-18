import asyncio
import shutil
import socket
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from heardback.sdnotify import healthy, sd_notify

UNITS = Path(__file__).parent.parent / "systemd"


def test_sd_notify_real_socket(tmp_path, monkeypatch):
    sock = tmp_path / "n.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as srv:
        srv.bind(str(sock))
        monkeypatch.setenv("NOTIFY_SOCKET", str(sock))
        sd_notify("READY=1")
        assert srv.recv(64) == b"READY=1"


def test_sd_notify_abstract_and_unset(monkeypatch):
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as srv:
        srv.bind("\0jv-test-notify")
        monkeypatch.setenv("NOTIFY_SOCKET", "@jv-test-notify")
        sd_notify("WATCHDOG=1")
        assert srv.recv(64) == b"WATCHDOG=1"
    monkeypatch.delenv("NOTIFY_SOCKET")
    sd_notify("READY=1")   # no-op, no error


def test_healthy_blocked_pool_then_released():
    async def go():
        gpu, cpu = ThreadPoolExecutor(1), ThreadPoolExecutor(1)
        voice, mic = SimpleNamespace(mic_on=True), SimpleNamespace(running=True, node="n")
        gate = threading.Event()
        gpu.submit(gate.wait)
        assert not await healthy(gpu, cpu, voice, mic, wait=0.2)
        gate.set()
        assert await healthy(gpu, cpu, voice, mic, wait=2)
        gpu.shutdown(); cpu.shutdown()
    asyncio.run(go())


def test_healthy_mic_rules():
    async def go(running, node, on):
        p = ThreadPoolExecutor(1)
        r = await healthy(p, p, SimpleNamespace(mic_on=on), SimpleNamespace(running=running, node=node), wait=2)
        p.shutdown()
        return r
    assert not asyncio.run(go(False, "n", True))   # should run but doesn't
    assert asyncio.run(go(False, "", True))        # no device: not unhealthy
    assert asyncio.run(go(False, "n", False))      # mic off by the user


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="no systemd-analyze")
def test_units_verify():
    r = subprocess.run(["systemd-analyze", "--user", "verify", str(UNITS / "heardback.service"),
                        str(UNITS / "heardback-down.service")], capture_output=True, text=True)
    # conftest points HOME at a tmp dir, so %h/.local/bin/heardbackd can't exist there
    bad = [l for l in r.stderr.splitlines() if l.strip() and "not executable" not in l]
    assert not bad, r.stderr
