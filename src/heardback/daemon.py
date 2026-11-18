"""heardbackd: the brain's socket server plus the status announcer, and the voice client when the extra is in."""
import signal

# SUPER+C / mute send SIGUSR1/2 and the real handlers only land after the models load; until then the default action
# kills us. Ignore both before any other import; add_signal_handler replaces this once voice is up.
# ponytail: the interpreter's own start-up before this line still leaves a short window; it can't be closed from Python.
for _s in (signal.SIGUSR1, signal.SIGUSR2):
    signal.signal(_s, signal.SIG_IGN)

import asyncio  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import secrets  # noqa: E402
import sys  # noqa: E402

from heardback.brain import brain
from heardback.brain.claude_proc import with_bin
from heardback.migrate import migrate_dirs
from heardback.sdnotify import sd_notify
from jv import trace

ROTATE_EVERY_S = 3600
log = logging.getLogger("heardbackd")


async def _voice(voice_token: str) -> None:
    """Voice is a socket client; a crash restarts the whole service."""
    from heardback.voice.extra import INSTALL, missing
    if missing():
        log.info("%s; brain only", INSTALL)
        sd_notify("READY=1")
        return
    from heardback.voice import config, daemon
    try:
        await daemon.run(config.load(), brain.sock_path(), voice_token)
        raise RuntimeError("voice loop exited unexpectedly")
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("voice stopped; restarting service")
        # Executor threads cannot be killed or joined safely after a model hangs.
        os._exit(1)


async def _rotate() -> None:
    """Only heardbackd rotates trace.jsonl (at startup, then hourly); short-lived writers just append."""
    while True:
        await asyncio.to_thread(trace.rotate)
        await asyncio.sleep(ROTATE_EVERY_S)


def _dash():
    """The read-only dashboard on 127.0.0.1:dash_port (its own thread). Any failure leaves the brain and voice up."""
    from heardback.voice import config
    try:
        port = config.load().dash_port
        if not port:
            return None
        from heardback.dash import server
        return server.start(port)
    except Exception as e:
        log.warning("dash off: %r", e)
        return None


async def _main() -> None:
    voice_token = secrets.token_hex(32)
    server = asyncio.create_task(brain.serve(voice_token=voice_token))
    done, _ = await asyncio.wait({server}, timeout=0.5)   # e.g. "already running": don't load models
    extra = [] if done else [asyncio.create_task(_voice(voice_token)), asyncio.create_task(_rotate())]
    dash = None if done else _dash()
    try:
        await server
    finally:
        if dash:
            dash.shutdown()
        for t in extra:
            t.cancel()


def main() -> None:
    migrate_dirs()
    os.environ["PATH"] = with_bin(sys.executable)   # brain/STT `jv` calls use this install's jv
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
