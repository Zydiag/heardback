"""Model-backed Kokoro → Silero → Whisper replay; audio remains in memory."""
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")

pytestmark = [pytest.mark.models, pytest.mark.skipif(os.getenv("HEARDBACK_MODELS") != "1",
                                                       reason="set HEARDBACK_MODELS=1 to load local voice models")]


def _models():
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    try:
        import torch
        import torchaudio
        from faster_whisper import WhisperModel
        from heardback.voice.config import Config
        from heardback.voice.endpoint import silero
        from heardback.voice.models import REPOS, snapshot
        from heardback.voice.stt import Stt
        from heardback.voice.tts import load_kokoro
    except ImportError as exc:
        pytest.skip(f"voice model dependencies unavailable: {exc}")

    try:
        stt_path = snapshot(REPOS["stt"])
    except Exception as exc:
        pytest.skip(f"pinned STT/TTS models unavailable locally: {type(exc).__name__}: {exc}")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    try:
        if device == "cuda":
            stt = Stt(Config(stt_device="cuda"))
        else:
            stt = Stt.__new__(Stt)
            stt.model = WhisperModel(str(stt_path), device="cpu", compute_type="int8")
            stt.hotwords, stt.device, stt.fell_back, stt.repo = None, "cpu", False, REPOS["stt"]
        return stt, load_kokoro("bm_george", device), silero(), torchaudio
    except Exception as exc:
        if "out of memory" in str(exc).lower():   # only this skips: any other load error is a real failure
            pytest.skip(f"CUDA out of memory loading the models: {exc}")
        raise


def _pcm(audio, torchaudio, torch):
    return (torchaudio.functional.resample(torch.from_numpy(np.asarray(audio, np.float32)),
                                           24000, 16000).clamp(-1, 1).numpy() * 32767).astype(np.int16)


def _chunks(audio, prob):
    from heardback.voice.chunker import Chunker
    chunker = Chunker(prob)
    output = []
    # Deliberately irregular input lengths exercise the public feed remainder.
    sizes = (1379, 4096, 2511, 8192)
    offset = 0
    while offset < len(audio):
        size = sizes[(offset // 512) % len(sizes)]
        output.extend(chunker.feed(audio[offset:offset + size]))
        offset += size
    tail = chunker.flush()
    if tail:
        output.append(tail)
    return output


def _wer(reference, hypothesis):
    a, b = reference.lower().split(), hypothesis.lower().split()
    row = list(range(len(b) + 1))
    for i, word in enumerate(a, 1):
        nxt = [i]
        for j, got in enumerate(b, 1):
            nxt.append(min(nxt[-1] + 1, row[j] + 1, row[j - 1] + (word != got)))
        row = nxt
    return row[-1] / max(len(a), 1)


def _squeezed(pcm, max_quiet=2400):
    """Trim edge silence and cut any quiet run inside the speech down to 0.15 s, whatever Kokoro rendered."""
    quiet = np.abs(pcm) <= 0.02 * np.abs(pcm).max()
    loud = np.flatnonzero(~quiet)
    pcm, quiet = pcm[loud[0]:loud[-1] + 1], quiet[loud[0]:loud[-1] + 1]
    edges = np.flatnonzero(np.diff(np.concatenate([[0], quiet, [0]]).astype(int)))
    keep = np.ones(len(pcm), bool)
    for lo, hi in zip(edges[::2], edges[1::2]):
        keep[lo + max_quiet:hi] = False
    return pcm[keep]


def _long_clip(synth, torchaudio, torch):
    """About 30 s of the fixture sentences, each trimmed of Kokoro's own edge silence. The first three are joined
    by 0.4 s gaps (a soft-cap cut after 8 s), the rest by 0.2 s gaps, so no pause reaches the 0.6 s end silence
    and the 20 s hard cap has to cut what remains. Returns the audio and the text spoken, in order."""
    path = Path(__file__).parents[2] / "scripts" / "dictation_fixtures.py"
    spec = importlib.util.spec_from_file_location("dictation_fixtures", path)
    sentences = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sentences)
    parts, spoken, total, i = [], [], 0, 0
    while total <= 30 * 16000:   # cycle the six sentences
        spoken.append(sentences.SENTENCES[i % len(sentences.SENTENCES)])
        pcm = _pcm(synth(spoken[-1]), torchaudio, torch)
        parts += [_squeezed(pcm), np.zeros(6400 if i < 3 else 3200, np.int16)]
        total += len(parts[-2]) + len(parts[-1])
        i += 1
    return np.concatenate(parts), " ".join(spoken)


def test_chunked_vs_one_pass_within_2_percent():
    stt, synth, prob, torchaudio = _models()
    import torch

    phrases = ["The quick brown fox jumps over the lazy dog.",
               "Please keep this sentence exactly as spoken.",
               "A calm voice makes a clear recording."]
    errors = []
    for text in phrases:
        audio = _pcm(synth(text), torchaudio, torch)
        one_pass = stt.transcribe_dictation(audio)
        prob.reset()
        cuts = _chunks(np.concatenate([audio, np.zeros(16000, np.int16)]), prob)
        pieces = [stt.transcribe_dictation(c.audio) for c in cuts]
        errors.append(_wer(one_pass, " ".join(pieces)))
    assert max(errors, default=0.0) <= 0.02, f"synthetic WER: {errors}"


def _words(text):
    return "".join(c for c in text.lower() if c.isalnum() or c.isspace())


def test_long_clip_chunked_vs_source_within_2_percent():
    stt, synth, prob, torchaudio = _models()
    import torch
    clip, source = _long_clip(synth, torchaudio, torch)
    prob.reset()
    cuts = _chunks(np.concatenate([clip, np.zeros(16000, np.int16)]), prob)
    reasons = [c.reason for c in cuts]
    assert len(cuts) > 1 and {"soft_cap", "hard_cap"} <= set(reasons), f"clip not cut by both caps: {reasons}"
    pieces = [stt.transcribe_dictation(c.audio) for c in cuts]
    wer = _wer(_words(source), _words(" ".join(pieces)))
    print(f"long-clip WER vs source {wer:.3f}, cuts {reasons}")
    # 2% of ~79 words allows one error: a single word lost at a soft cut still passes (clean control is 0.000 on 4 clips).
    assert wer <= 0.02, f"long-clip WER {wer:.3f}, cuts {reasons}\nsource: {source}\nchunked: {' '.join(pieces)}"


def test_no_duplicate_boundary_words():
    stt, synth, prob, torchaudio = _models()
    import torch
    text = "The final words should not repeat at the boundary."
    audio = _pcm(synth(text), torchaudio, torch)
    joined = np.concatenate([audio, np.zeros(9600, np.int16), audio])
    prob.reset()
    chunks = _chunks(joined, prob)
    words = [stt.transcribe_dictation(c.audio).lower().split() for c in chunks]
    for left, right in zip(words, words[1:]):
        assert not (left and right and left[-1] == right[0]), f"duplicate boundary: {left[-1]!r}"


def test_no_duplicate_boundary_words_long_clip():
    stt, synth, prob, torchaudio = _models()
    import torch
    prob.reset()
    chunks = _chunks(np.concatenate([_long_clip(synth, torchaudio, torch)[0], np.zeros(16000, np.int16)]), prob)
    reasons = [c.reason for c in chunks]
    assert len(chunks) > 1 and {"soft_cap", "hard_cap"} <= set(reasons), f"clip not cut by both caps: {reasons}"
    words = [stt.transcribe_dictation(c.audio).lower().split() for c in chunks]
    for left, right in zip(words, words[1:]):
        assert not (left and right and left[-1] == right[0]), \
            f"duplicate boundary: {left[-1]!r} in cuts {reasons}"


async def test_latency_report_from_trace(tmp_path, monkeypatch):
    """Real DesktopDictation, STT and VAD, a fake injector, and the real trace file in a temp state dir."""
    stt, synth, prob, torchaudio = _models()
    if stt.device != "cuda":   # also true when Stt fell back to CPU
        pytest.skip(f"latency test needs CUDA (stt.device={stt.device!r}): CPU takes ~4 s a chunk, over the 60 s test limit")
    import torch
    from concurrent.futures import ThreadPoolExecutor
    from heardback.voice import desktop_dictation as core
    from jv import trace
    sys.path.insert(0, str(Path(__file__).parent))
    from test_desktop_dictation import PIN, Injector   # the suite's fake: no wtype, wl-copy or hyprctl

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert trace.path() == tmp_path / "heardback" / "trace.jsonl"
    audio = _pcm(synth("This is a short synthetic latency sample."), torchaudio, torch)
    utterance = np.concatenate([audio, np.zeros(12000, np.int16)])
    settings = SimpleNamespace(dictation_slice_chars=48, dictation_type_delay_ms=0, dictation_post_shortcut_ms=0,
                               dictation_max_session_s=1800, dictation_ignore_layers=("custom-notification",))

    def event(**fields):
        if fields.get("event") == "dictation_chunk":   # the voice loop's mapping, minus its event-loop hop
            trace.emit("voice", "dictation_chunk", None, **{k: v for k, v in fields.items() if k != "event"})

    gpu, io, injector = ThreadPoolExecutor(1), ThreadPoolExecutor(1), Injector()
    d = core.DesktopDictation(stt, gpu, prob, injector, io, settings=settings, event=event)
    start = time.monotonic()
    try:
        d.start(PIN)
        async with asyncio.timeout(10):
            while not (d._session.ready or d.phase == "paused"):
                await asyncio.sleep(0.01)
        assert d.phase == "listening"
        for n in range(30):   # thirty separated utterances, fed one at a time so the backlog guard stays quiet
            for off in range(0, len(utterance), 1280):
                assert d.feed(utterance[off:off + 1280])
            while sum(e.traced for e in d._session.ledger) <= n:   # conftest's 60 s wrapper bounds this
                assert d.phase == "listening", d.phase
                await asyncio.sleep(0.01)
        d.toggle_stop()
        async with asyncio.timeout(30):
            while d.phase != "stopped":
                await asyncio.sleep(0.01)
    finally:
        d.shutdown()
        await asyncio.to_thread(gpu.shutdown, wait=True)
        await asyncio.to_thread(io.shutdown, wait=True)
        trace._close()
    assert injector.sent, "the fake injector received nothing"
    path = tmp_path / "heardback" / "trace.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) >= 30 and all(r["ev"] == "dictation_chunk" for r in rows), f"{len(rows)} rows"
    for key in ("speech_end_to_stt_ms", "stt_ms", "check_ms", "type_ms", "outcome"):
        assert all(key in r for r in rows), f"trace field {key} missing"
    assert {r["outcome"] for r in rows} == {"accepted"}, [r["outcome"] for r in rows]
    query = ('[.[] | select(.ev == "dictation_chunk") | '
             '(.speech_end_to_stt_ms + .stt_ms + .check_ms + .type_ms)] | '
             '{chunks:length, p50_ms:(sort | .[length/2|floor]), '
             'p95_ms:(sort | .[((length-1)*0.95|floor)])}')
    report = json.loads(subprocess.check_output(["jq", "-s", query, str(path)], text=True))
    assert report["chunks"] >= 30 and report["p50_ms"] > 0 and report["p95_ms"] >= report["p50_ms"]
    print(f"synthetic latency chunks={report['chunks']} wall_s={time.monotonic()-start:.2f} "
          f"p50_ms={report['p50_ms']:.0f} p95_ms={report['p95_ms']:.0f}; trace={path}")
