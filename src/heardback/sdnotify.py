"""Stdlib sd_notify plus the watchdog health gate. No-op without $NOTIFY_SOCKET (i.e. outside systemd)."""
import asyncio
import os
import socket

PING_S = 10      # WatchdogSec=30 in systemd/heardback.service
POOL_WAIT_S = 20


def sd_notify(msg: str) -> None:
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return
    if path[0] == "@":
        path = "\0" + path[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(msg.encode(), path)
    except OSError:
        pass


async def healthy(gpu, cpu, voice, mic, wait: float = POOL_WAIT_S) -> bool:
    """A no-op on each pool returns in time, and the mic reader runs whenever it should."""
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(asyncio.gather(loop.run_in_executor(gpu, int), loop.run_in_executor(cpu, int)), wait)
    except asyncio.TimeoutError:
        return False
    return mic.running or not (voice.mic_on and mic.node)   # a missing device: failed() already notifies


async def watchdog(gpu, cpu, voice, mic) -> None:
    while True:
        if await healthy(gpu, cpu, voice, mic):
            sd_notify("WATCHDOG=1")
        await asyncio.sleep(PING_S)
