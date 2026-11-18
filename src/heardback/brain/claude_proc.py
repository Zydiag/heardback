"""The brain's one persistent `claude -p` stream-json process (subscription login, never --bare)."""
import asyncio
import json
import logging
import os
import signal
import subprocess
from pathlib import Path

from heardback.config import config_path, load as desktop_config

PROMPT = Path(__file__).with_name("prompt.md")
KILL_GRACE_S = 2
LINE_LIMIT = 16 * 1024 * 1024   # init/result lines can be long

log = logging.getLogger(__name__)


def prompt_text() -> str:
    return PROMPT.read_text().replace("{{user}}", desktop_config().name or "the user")


def with_bin(python: str) -> str:
    """PATH with `python`'s bin dir first, so `jv` resolves to the install that python belongs to
    (the uv tool snapshot under the service), never whatever else is on PATH."""
    d = str(Path(python).parent)   # not resolved: a venv's bin/python symlinks out of the venv
    return os.pathsep.join([d, *(x for x in os.environ.get("PATH", "").split(os.pathsep) if x and x != d)])


def build_cmd(claude: list[str], prompt_file: Path, allowed: list[str] | None = None,
              mcp_python: str | None = None, execution_owner: str = "") -> list[str]:
    cmd = [*claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
           "--include-partial-messages", "--model", "haiku", "--restricted", "--tools", "",
           "--disable-slash-commands", "--strict-mcp-config", "--permission-mode", "manual",
           "--setting-sources", "",   # R9: --restricted alone still loads ~/.claude/settings.json
           "--max-turns", "8", "--max-budget-usd", "0.50", "--system-prompt-file", str(prompt_file),
           "--no-session-persistence"]
    if prompt_file == PROMPT:
        i = cmd.index("--system-prompt-file")
        cmd[i:i + 2] = ["--system-prompt", prompt_text()]
    if mcp_python:
        mcp = {"mcpServers": {"jv": {"type": "stdio", "command": mcp_python, "args": ["-m", "heardback.brain.mcp_jv"],
                                     "env": {"PATH": with_bin(mcp_python),   # jv from the same install as the brain
                                             "HEARDBACK_REQUIRE_GRANT": "1",
                                             "HEARDBACK_EXECUTION_GATE": "1",
                                             "HEARDBACK_EXECUTION_OWNER": execution_owner,
                                             "HEARDBACK_CONFIG": str(config_path().absolute()),
                                             **{k: os.environ[k] for k in ("LANG", "LC_ALL") if k in os.environ}}}}}
        cmd += ["--mcp-config", json.dumps(mcp), "--allowedTools", ",".join(allowed or []),
                "--permission-prompt-tool", "stdio"]
    return cmd


def child_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["HEARDBACK_BRAIN"] = "1"   # 02's jv-hook exits on it
    return env


class ClaudeProc:
    def __init__(self, cmd: list[str], cwd: Path, execution_owner: str = ""):
        self.cmd, self.cwd = cmd, Path(cwd)
        self.execution_owner = execution_owner
        self.proc: asyncio.subprocess.Process | None = None
        self.events: asyncio.Queue[dict] = asyncio.Queue()
        self._rid = 0
        self._reader: asyncio.Task | None = None

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    async def start(self) -> None:
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.proc = await asyncio.create_subprocess_exec(
            *self.cmd, cwd=self.cwd, env=child_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            limit=LINE_LIMIT)
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            while line := await self.proc.stdout.readline():
                try:
                    ev = json.loads(line)
                except ValueError:
                    ev = None
                if isinstance(ev, dict):
                    self.events.put_nowait(ev)
                else:
                    log.warning("claude: non-JSON line %r", line[:200])
        except (ValueError, ConnectionError) as e:   # ValueError: line over LINE_LIMIT
            log.warning("claude: stdout read failed (%s)", e)
        finally:
            self.proc.stdin.close()   # also when cancelled at shutdown: EOF ends the child
        self.events.put_nowait({"type": "_exit", "code": await self.proc.wait()})

    async def _write(self, msg: dict) -> bool:
        if not self.alive:
            return False
        try:
            self.proc.stdin.write((json.dumps(msg) + "\n").encode())
            await self.proc.stdin.drain()
            return self.alive
        except (ConnectionError, RuntimeError) as e:
            log.warning("claude: stdin write failed (%s)", e)
            return False

    async def send(self, text: str) -> bool:
        return await self._write({"type": "user", "message": {"role": "user", "content": text}})

    async def control(self, subtype: str, **fields) -> str:
        self._rid += 1
        rid = f"jv-{self._rid}"
        await self._write({"type": "control_request", "request_id": rid, "request": {"subtype": subtype, **fields}})
        return rid

    async def respond(self, request_id: str, response: dict) -> None:
        await self._write({"type": "control_response",
                           "response": {"subtype": "success", "request_id": request_id, "response": response}})

    async def kill(self) -> None:
        """SIGTERM, then SIGKILL after KILL_GRACE_S. Never SIGINT: it ends the process too (spec §2.5)."""
        if not self.alive:
            return
        self.proc.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(self.proc.wait(), KILL_GRACE_S)
        except asyncio.TimeoutError:
            self.proc.kill()
            await self.proc.wait()
        if self._reader:   # drains stdout, queues _exit; bounded: a grandchild may hold stdout open
            try:
                await asyncio.wait_for(self._reader, KILL_GRACE_S)
            except asyncio.TimeoutError:
                self.events.put_nowait({"type": "_exit", "code": self.proc.returncode})
