"""Private, bounded local control socket for typed dictation commands."""
import asyncio
import errno
import json
import os
import socket
import stat
import struct
import time
from pathlib import Path

from .dictation import DictationError

MAX_REQUEST = 2048
MAX_RESPONSE = 4096
CLIENT_TIMEOUT_S = 5.0
ACTION_TIMEOUT_S = 4.0
MUTATIONS = {"pause", "resume", "finish", "cancel", "commit"}
ACTIONS = MUTATIONS | {"start", "status"}
_RESULT_FIELDS = frozenset({"session_id", "phase", "reason", "target", "pane_id", "chars",
                            "revision", "final", "commit_started", "operation", "mode", "class",
                            "delivered_chars", "held_chars"})


def socket_path() -> Path:
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    return runtime / "heardback-voice-control.sock"


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def validate_request(value: object) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("action"), str):
        raise ValueError("invalid_request")
    action = value["action"]
    if action not in ACTIONS:
        raise ValueError("invalid_action")
    allowed = {
        "start": {"action", "target"},
        "status": {"action", "session_id"},
        "pause": {"action", "session_id"},
        "resume": {"action", "session_id"},
        "finish": {"action", "session_id"},
        "cancel": {"action", "session_id"},
        "commit": {"action", "session_id", "revision"},
    }[action]
    if set(value) - allowed or not set(value) >= {"action"}:
        raise ValueError("invalid_fields")
    if action == "start":
        target = value.get("target")
        if not isinstance(target, str) or not 1 <= len(target) <= 200 or \
                any(ord(c) < 32 or ord(c) == 127 for c in target) or not target.strip():
            raise ValueError("invalid_target")
    elif action in MUTATIONS or "session_id" in value:
        sid = value.get("session_id")
        if not isinstance(sid, str) or len(sid) != 32 or any(c not in "0123456789abcdef" for c in sid):
            raise ValueError("invalid_session")
    if action == "commit":
        revision = value.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise ValueError("invalid_revision")
    return value


def _peer_uid(conn: socket.socket) -> int | None:
    if not hasattr(socket, "SO_PEERCRED"):
        return None
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


def _apply_deadline(sock: socket.socket, deadline: float) -> None:
    """Set the next blocking socket operation to the remaining total I/O budget."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("control_timeout")
    sock.settimeout(remaining)


def _safe_parent(path: Path) -> None:
    try:
        st = path.parent.lstat()
    except OSError as e:
        raise RuntimeError("runtime_directory_unavailable") from e
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise RuntimeError("unsafe_runtime_directory")
    if len(os.fsencode(path)) >= 104:
        raise RuntimeError("socket_path_too_long")


def _remove_stale(path: Path) -> None:
    try:
        first = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(first.st_mode) or first.st_uid != os.getuid():
        raise RuntimeError("unsafe_existing_socket_path")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.2)
    try:
        probe.connect(str(path))
    except OSError as e:
        if e.errno not in (errno.ECONNREFUSED, errno.ENOENT):
            raise RuntimeError("existing_socket_unavailable") from e
    else:
        raise RuntimeError("control_socket_already_active")
    finally:
        probe.close()
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino, current.st_uid) != (first.st_dev, first.st_ino, os.getuid()) or \
            not stat.S_ISSOCK(current.st_mode):
        raise RuntimeError("socket_path_changed")
    path.unlink()


class ControlServer:
    def __init__(self, voice, path: Path | None = None):
        self.voice, self.path = voice, path or socket_path()
        self.server: asyncio.AbstractServer | None = None
        self._inode: tuple[int, int] | None = None

    async def start(self) -> None:
        _safe_parent(self.path)
        _remove_stale(self.path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.setblocking(False)
        old_mask = os.umask(0o177)
        try:
            sock.bind(str(self.path))
        except Exception:
            sock.close()
            raise
        finally:
            os.umask(old_mask)
        try:
            os.chmod(self.path, 0o600)
            st = self.path.lstat()
            if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600:
                raise RuntimeError("control_socket_permissions")
            self._inode = (st.st_dev, st.st_ino)
            sock.listen(socket.SOMAXCONN)
            self.server = await asyncio.start_unix_server(self._handle, sock=sock, limit=MAX_REQUEST + 1)
        except Exception:
            sock.close()
            self._cleanup_path()
            raise

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        self._cleanup_path()

    def _cleanup_path(self) -> None:
        if self._inode is None:
            return
        try:
            st = self.path.lstat()
            if stat.S_ISSOCK(st.st_mode) and st.st_uid == os.getuid() and \
                    (st.st_dev, st.st_ino) == self._inode:
                self.path.unlink()
        except FileNotFoundError:
            pass
        finally:
            self._inode = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        response = {"ok": False, "error": "invalid_request"}
        try:
            peer = writer.get_extra_info("socket")
            if peer is not None:
                uid = _peer_uid(peer)
                if uid is not None and uid != os.getuid():
                    response = {"ok": False, "error": "wrong_user"}
                    raise PermissionError("wrong_user")
            raw = await asyncio.wait_for(reader.readline(), CLIENT_TIMEOUT_S)
            if not raw or len(raw) > MAX_REQUEST or not raw.endswith(b"\n"):
                raise ValueError("invalid_request")
            value = json.loads(raw[:-1].decode("utf-8"), object_pairs_hook=_pairs)
            request = validate_request(value)
            result = await asyncio.wait_for(self.voice.dictation_control(request), ACTION_TIMEOUT_S)
            # This protocol intentionally returns metadata only. In particular, the final transcript
            # never crosses the socket even if a future handler accidentally returns it.
            response = {"ok": True, **{k: v for k, v in result.items() if k in _RESULT_FIELDS}}
        except PermissionError:
            pass
        except (asyncio.TimeoutError, TimeoutError):
            response = {"ok": False, "error": "timeout"}
        except DictationError as e:
            response = {"ok": False, "error": e.code[:64]}
        except (ValueError, UnicodeError, json.JSONDecodeError):
            response = {"ok": False, "error": "invalid_request"}
        except Exception:
            response = {"ok": False, "error": "control_error"}
        try:
            body = json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n"
            if len(body) > MAX_RESPONSE:
                body = b'{"ok":false,"error":"response_too_large"}\n'
            writer.write(body)
            await asyncio.wait_for(writer.drain(), CLIENT_TIMEOUT_S)
        except (OSError, asyncio.TimeoutError, TimeoutError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


def request(payload: dict, path: Path | None = None) -> dict:
    request = validate_request(payload)
    raw = json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(raw) > MAX_REQUEST:
        raise ValueError("request_too_large")
    deadline = time.monotonic() + CLIENT_TIMEOUT_S
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        _apply_deadline(sock, deadline)
        sock.connect(str(path or socket_path()))
        _apply_deadline(sock, deadline)
        sock.sendall(raw)
        chunks = bytearray()
        while len(chunks) <= MAX_RESPONSE:
            _apply_deadline(sock, deadline)
            part = sock.recv(min(1024, MAX_RESPONSE + 1 - len(chunks)))
            if not part:
                break
            chunks.extend(part)
            if b"\n" in part:
                break
        if len(chunks) > MAX_RESPONSE or b"\n" not in chunks:
            raise ValueError("invalid_response")
        result = json.loads(bytes(chunks).split(b"\n", 1)[0].decode("utf-8"), object_pairs_hook=_pairs)
        if not isinstance(result, dict):
            raise ValueError("invalid_response")
        return result
    except TimeoutError:
        raise TimeoutError("control_timeout") from None
    finally:
        sock.close()
