import asyncio
import json
import os
import socket
import stat
import time

import pytest

pytest.importorskip("numpy")

from heardback.voice import control
from heardback.voice.dictation import DictationError


SID = "a" * 32


class FakeVoice:
    def __init__(self):
        self.requests = []

    async def dictation_control(self, request):
        self.requests.append(request)
        return {"session_id": SID, "phase": "review", "chars": 7,
                "text": "secret words", "operation": request["action"]}


async def raw(path, data):
    reader, writer = await asyncio.open_unix_connection(path)
    writer.write(data)
    await writer.drain()
    writer.write_eof()
    response = await asyncio.wait_for(reader.readline(), 1)
    writer.close()
    await writer.wait_closed()
    return json.loads(response)


async def serve(tmp_path, voice=None):
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    path = runtime / "dictation.sock"
    server = control.ControlServer(voice or FakeVoice(), path)
    await server.start()
    return server, path


def test_validate_request_bounds_actions_fields_and_types():
    assert control.validate_request({"action": "start", "target": "%3"})["target"] == "%3"
    assert control.validate_request({"action": "status"})["action"] == "status"
    with pytest.raises(ValueError, match="invalid_action"):
        control.validate_request({"action": "shell", "command": "whoami"})
    with pytest.raises(ValueError, match="invalid_fields"):
        control.validate_request({"action": "status", "text": "leak"})
    with pytest.raises(ValueError, match="invalid_target"):
        control.validate_request({"action": "start", "target": " "})
    with pytest.raises(ValueError, match="invalid_target"):
        control.validate_request({"action": "start", "target": "%1\n"})
    with pytest.raises(ValueError, match="invalid_session"):
        control.validate_request({"action": "cancel", "session_id": "bad"})
    for revision in (0, True, "1"):
        with pytest.raises(ValueError, match="invalid_revision"):
            control.validate_request({"action": "commit", "session_id": SID, "revision": revision})


async def test_socket_permissions_metadata_filter_and_malformed_requests(tmp_path):
    voice = FakeVoice()
    server, path = await serve(tmp_path, voice)
    try:
        st = path.stat()
        assert stat.S_ISSOCK(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o600
        result = await asyncio.to_thread(control.request, {"action": "status"}, path)
        assert result == {"ok": True, "session_id": SID, "phase": "review", "chars": 7, "operation": "status"}
        assert "secret words" not in json.dumps(result)
        assert voice.requests == [{"action": "status"}]

        for wire in (
            b"not-json\n",
            b'{"action":"status","action":"cancel"}\n',
            b'{"action":"status","unknown":1}\n',
            b'{"action":"status"}',
            b"x" * (control.MAX_REQUEST + 1) + b"\n",
        ):
            response = await raw(path, wire)
            assert response == {"ok": False, "error": "invalid_request"}
        assert voice.requests == [{"action": "status"}]
    finally:
        await server.stop()
    assert not path.exists()


async def test_control_server_rejects_wrong_uid_before_dispatch(tmp_path, monkeypatch):
    voice = FakeVoice()
    server, path = await serve(tmp_path, voice)
    monkeypatch.setattr(control, "_peer_uid", lambda _sock: os.getuid() + 1)
    try:
        response = await raw(path, b'{"action":"status"}\n')
        assert response == {"ok": False, "error": "wrong_user"}
        assert voice.requests == []
    finally:
        await server.stop()


async def test_socket_path_stale_active_and_replacement_protection(tmp_path):
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    path = runtime / "dictation.sock"

    path.write_text("keep")
    with pytest.raises(RuntimeError, match="unsafe_existing_socket_path"):
        await control.ControlServer(FakeVoice(), path).start()
    assert path.read_text() == "keep"
    path.unlink()

    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()
    server = control.ControlServer(FakeVoice(), path)
    await server.start()
    assert stat.S_ISSOCK(path.stat().st_mode)
    second = control.ControlServer(FakeVoice(), path)
    with pytest.raises(RuntimeError, match="control_socket_already_active"):
        await second.start()

    path.unlink()
    path.write_text("replacement")
    await server.stop()
    assert path.read_text() == "replacement"


async def test_control_client_uses_one_absolute_deadline(tmp_path, monkeypatch):
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    path = runtime / "slow.sock"

    async def drip(reader, writer):
        await reader.readline()
        for byte in b'{"ok":true}\n':
            writer.write(bytes([byte]))
            try:
                await writer.drain()
            except OSError:
                break
            await asyncio.sleep(0.06)
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(drip, path)
    monkeypatch.setattr(control, "CLIENT_TIMEOUT_S", 0.18)
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="control_timeout"):
            await asyncio.to_thread(control.request, {"action": "status"}, path)
        assert time.monotonic() - started < 0.35
    finally:
        server.close()
        await server.wait_closed()


async def test_desktop_control_status_filters_text_without_socket_bind(tmp_path):
    class Voice:
        async def dictation_control(self, request):
            assert request == {"action": "status"}
            return {"session_id": SID, "mode": "desktop", "class": "kitty", "phase": "paused",
                    "reason": "focus_changed", "delivered_chars": 4, "held_chars": 7,
                    "delivered": "secret delivered", "held": "secret held", "text": "secret"}

    class Writer:
        def get_extra_info(self, _key):
            return None

        def write(self, value):
            self.value = value

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    reader, writer = asyncio.StreamReader(), Writer()
    reader.feed_data(b'{"action":"status"}\n')
    await control.ControlServer(Voice(), tmp_path / "unused.sock")._handle(reader, writer)
    result = json.loads(writer.value)
    assert result == {"ok": True, "session_id": SID, "mode": "desktop", "class": "kitty",
                      "phase": "paused", "reason": "focus_changed", "delivered_chars": 4, "held_chars": 7}
    assert "secret" not in writer.value.decode()


def test_desktop_resume_requires_session():
    with pytest.raises(ValueError, match="invalid_session"):
        control.validate_request({"action": "resume"})
    assert control.validate_request({"action": "resume", "session_id": SID})["session_id"] == SID
