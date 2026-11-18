"""The offline run-file adapter satisfies the current Voice call contract."""
import asyncio
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

sys.path.insert(0, str(Path(__file__).parent.parent))
from fake_brain import FakeBrain  # noqa: E402
from heardback.voice import endpoint, runfile, stt, text  # noqa: E402
from heardback.voice.config import Config  # noqa: E402


class FakeStt:
    def __init__(self, cfg, words):
        self.calls = 0

    def transcribe(self, audio):
        self.calls += 1
        return "stop"


def _fake_vad(chunk):
    return 1.0 if np.sqrt(np.mean((chunk / 32768.0) ** 2)) > 0.01 else 0.0


async def test_run_file_uses_silent_current_adapters_without_background_errors(monkeypatch, tmp_path, capsys):
    from heardback.voice.audio_in import FRAME

    path = tmp_path / "brain.sock"
    brain = FakeBrain(path)
    sent = []
    send = brain.send

    async def record_sent(message):
        sent.append(message)
        await send(message)

    monkeypatch.setattr(brain, "send", record_sent)

    async def reply(peer, _message):
        await peer.send({"type": "say", "text": "Done.", "final": True})
        await peer.send({"type": "done"})

    brain.on_utterance = reply
    await brain.start()
    monkeypatch.setattr(runfile, "load", lambda: Config())
    monkeypatch.setattr(stt, "Stt", FakeStt)
    monkeypatch.setattr(stt, "load_catalogue", lambda: None)
    monkeypatch.setattr(stt, "local_words", lambda: [])
    monkeypatch.setattr(text, "hotwords", lambda *args: "")
    monkeypatch.setattr(endpoint, "silero", lambda: _fake_vad)
    monkeypatch.setattr(runfile, "wav_frames", lambda _path: iter([np.full(FRAME, 5000, np.int16)] * 8))
    try:
        rc = await runfile.run_file(tmp_path / "synthetic.wav", path)
        await brain.wait_for("utterance")
        async with asyncio.timeout(5):
            while not any(message.get("type") == "done" for message in sent):
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.6)  # let the delayed no-op resume call run
    finally:
        await brain.close()

    output = capsys.readouterr().out
    assert rc == 0 and "say: Done." in output
    assert "voice_error" not in output


def test_quiet_accepts_sticky_and_turn_scoped_calls_without_side_effects(capsys):
    quiet = runfile.Quiet()
    quiet.notify("question", sticky=True)
    quiet.pause("turn-1")
    quiet.keep()
    quiet.resume("turn-1")
    assert capsys.readouterr() == ("", "")
