"""Only the daemon's in-memory voice token can carry speaker trust."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from heardback.brain import catalogue
from heardback.brain.brain import Brain
from heardback.voice.link import Link
from jv import trace, wall
from jv.cli import catalogue as verb_catalogue
from jv.verb import runtime_path

TOKEN = "a" * 64


@pytest.mark.parametrize("configured", [None, TOKEN])
@pytest.mark.parametrize("token", [None, "wrong", "", True, 1, [], {}, "é", "\ud800"])
def test_invalid_hello_is_not_voice(monkeypatch, configured, token):
    b = Brain(voice_token=configured)
    original, stranger = object(), object()
    b.voice = original
    emit = Mock()
    monkeypatch.setattr(trace, "emit", emit)
    hello = {"type": "hello", "role": "voice"}
    if token is not None:
        hello["token"] = token
    asyncio.run(b.handle(hello, stranger))
    assert b.voice is original
    emit.assert_called_once_with("brain", "hello_refused", None, reason="invalid voice token")
    assert TOKEN not in str(emit.call_args)


def test_no_configured_token_never_authenticates():
    b = Brain()
    asyncio.run(b.handle({"type": "hello", "role": "voice", "token": TOKEN}, object()))
    assert b.voice is None


@pytest.mark.parametrize("wire", ["confirm_reply", "utterance"])
@pytest.mark.parametrize("voice", [False, True])
@pytest.mark.parametrize("speaker", [None, False, True])
def test_confirmation_trust(wire, voice, speaker):
    async def go():
        b = Brain(voice_token=TOKEN)
        conn = Mock()
        conn.is_closing.return_value = False
        b._send = AsyncMock()
        if voice:
            await b.handle({"type": "hello", "role": "voice", "token": TOKEN}, conn)
        task = asyncio.create_task(b.ask(conn, "Close tabs?", True))
        await asyncio.sleep(0)
        question = b.awaiting[conn]
        msg = {"type": wire, "ok": True, "text": "yes", "confirmation_id": question.id}
        if speaker is not None:
            msg["speaker_ok"] = speaker
        await b.handle(msg, conn)
        trusted = voice and (speaker is True or wire == "confirm_reply" and speaker is None)
        assert await asyncio.wait_for(task, 0.2) == (trusted, "yes" if trusted else "")
        assert conn not in b.awaiting
    asyncio.run(go())


def test_stranger_cannot_answer_voice_question():
    async def go():
        b = Brain(voice_token=TOKEN)
        voice = Mock()
        voice.is_closing.return_value = False
        b._send = AsyncMock()
        await b.handle({"type": "hello", "role": "voice", "token": TOKEN}, voice)
        task = asyncio.create_task(b.ask(voice, "Close tabs?", True))
        await asyncio.sleep(0)
        msg = {"type": "confirm_reply", "ok": True, "speaker_ok": True, "text": "yes",
               "confirmation_id": b.awaiting[voice].id}
        await b.handle(msg, object())
        assert not task.done()
        await b.handle(msg, voice)
        assert await asyncio.wait_for(task, 0.2) == (True, "yes")
    asyncio.run(go())


@pytest.mark.parametrize("voice", [False, True])
@pytest.mark.parametrize("text", ["next wallpaper", "what's my wallpaper", "unmatched request"])
def test_socket_utterance_trust(tmp_path, monkeypatch, voice, text):
    cat = catalogue._index(verb_catalogue(wall.VERBS))
    monkeypatch.setattr(catalogue, "get", lambda: cat)
    run = AsyncMock(return_value=(0, '{"say":"Wallpaper."}', ""))
    monkeypatch.setattr(catalogue, "run_jv", run)

    async def go():
        b = Brain(voice_token=TOKEN)
        path = runtime_path("auth-test.sock")
        server = await asyncio.start_unix_server(b._client, path)
        link = Link(path, TOKEN if voice else "wrong")
        try:
            assert await link.connect()
            await link.send({"type": "utterance", "text": text, "speaker_ok": True})
            messages = []
            async with asyncio.timeout(2):
                while True:
                    msg = await link.recv()
                    messages.append(msg)
                    if msg["type"] == "done":
                        break
            allowed = text != "unmatched request" and (voice or text == "what's my wallpaper")
            assert run.await_count == int(allowed)
            if not voice:
                assert not Path(runtime_path("heardback-heard.json")).exists()
                assert messages[0]["text"] == ("Wallpaper." if allowed else
                                                "Actions need an authenticated voice request.")
            assert (b.voice is not None) == voice
        finally:
            link.close()
            server.close()
            await server.wait_closed()
            if b._idle:
                b._idle.cancel()
    asyncio.run(go())


def test_daemon_shares_one_fresh_token(monkeypatch):
    from heardback import daemon

    calls = []
    async def go():
        ready = asyncio.Event()
        async def serve(*, voice_token):
            calls.append(("brain", voice_token))
            await ready.wait()
        async def voice(voice_token):
            calls.append(("voice", voice_token))
            ready.set()
        monkeypatch.setattr(daemon.brain, "serve", serve)
        monkeypatch.setattr(daemon, "_voice", voice)
        monkeypatch.setattr(daemon, "_dash", lambda: None)
        monkeypatch.setattr(daemon, "_rotate", AsyncMock())
        await daemon._main()
    asyncio.run(go())
    asyncio.run(go())
    assert [name for name, _ in calls] == ["brain", "voice", "brain", "voice"]
    assert calls[0][1] == calls[1][1] and calls[2][1] == calls[3][1]
    assert calls[0][1] != calls[2][1]
    assert all(len(bytes.fromhex(token)) == 32 for _, token in calls)
