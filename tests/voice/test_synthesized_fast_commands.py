"""Offline end-to-end speech checks for representative fast-path commands.

Run explicitly with HEARDBACK_MODELS=1 and HF_HUB_OFFLINE=1. Synthesized PCM stays in
memory and every action boundary is replaced with a local fake.
"""
import json
import subprocess
from copy import deepcopy

import pytest

np = pytest.importorskip("numpy")
torch = pytest.importorskip("torch")
torchaudio = pytest.importorskip("torchaudio")

from heardback.brain import catalogue as brain_catalogue, router
from heardback.voice.config import Config
from heardback.voice.models import REPOS, snapshot
from heardback.voice.stt import Stt
from heardback.voice.text import hotwords
from heardback.voice.tts import load_kokoro
from jv import media
from jv.cli import all_verbs, catalogue as jv_catalogue


@pytest.mark.models
def test_synthesized_fast_commands_reach_the_real_cli(monkeypatch, run_jv, capsys):
    def report(line):
        with capsys.disabled():
            print(line)

    hotword_hint = hotwords(None)
    if torch.cuda.is_available():
        cfg = Config(stt_device="cuda", tts_voice="bm_george")
        stt = Stt(cfg, hotword_hint)
        device = "cuda"
    else:
        # Keep the pinned Whisper model and run the same weights on CPU when this
        # isolated runner cannot see CUDA; do not fall back to the smaller model.
        from faster_whisper import WhisperModel
        cfg = Config(stt_device="cpu", tts_voice="bm_george")
        stt = Stt.__new__(Stt)
        stt.model = WhisperModel(str(snapshot(REPOS["stt"])), device="cpu", compute_type="int8")
        stt.hotwords, stt.device, stt.fell_back, stt.repo = hotword_hint, "cpu", False, REPOS["stt"]
        device = "cpu"
        report("CUDA unavailable in this isolated runner; using cached large-v3-turbo weights on CPU.")
    assert stt.repo == REPOS["stt"], f"unexpected Whisper model selected: {stt.repo}"
    synth = load_kokoro(cfg.tts_voice, device)
    verbs = deepcopy(all_verbs())
    next(v for v in verbs if v.name == "tmux.select").args["window"]["choices"] = lambda: []
    cat = brain_catalogue._index(jv_catalogue(verbs))
    fast = router.Router(cat)
    expected = {
        "play next song": ("media", {"action": "next"}),
        "previous song": ("media", {"action": "previous"}),
        "lock the screen": ("local.lock", {}),
        "lock screen": ("local.lock", {}),
    }

    for spoken, wanted in expected.items():
        audio_24k = synth(spoken)
        assert audio_24k.size > 0
        audio_tensor = torch.from_numpy(np.asarray(audio_24k, dtype=np.float32))
        pcm16 = (torchaudio.functional.resample(audio_tensor, 24000, 16000).clamp(-1, 1).numpy()
                 * 32767).astype(np.int16)
        transcript = stt.transcribe(pcm16)
        route = fast.route(transcript)
        assert route is not None and (route.verb, route.args) == wanted, (
            f"{spoken!r} transcribed as {transcript!r}, routed as {route!r}")
        argv = brain_catalogue.build_argv(cat[route.verb], route.args, "--json")

        if route.verb == "media":
            state = {"title": "Before skip", "status": "Playing"}

            def players():
                return [{"name": "firefox.synthetic", "status": state["status"], "title": state["title"],
                         "artist": "Synth Artist", "url": "https://open.spotify.com/", "ducked": False}]

            def playerctl(*args):
                assert args == ("-p", "firefox.synthetic", route.args["action"])
                state["title"] = f"Synthesized {route.args['action']} result"
                return ""

            monkeypatch.setattr(media, "players", players)
            monkeypatch.setattr(media, "_pc", playerctl)
        if route.verb == "local.lock":
            with monkeypatch.context() as lock_patch:
                lock_patch.setattr(subprocess, "Popen",
                                    lambda *a, **kw: pytest.fail("removed screen-lock command launched a subprocess"))
                code, result, _ = run_jv(*argv[1:])
        else:
            code, result, _ = run_jv(*argv[1:])
        if route.verb == "local.lock":
            assert code == 1 and result["ok"] is False
            assert result["say"] == "Screen locking is disabled in heardback. Please use your desktop lock shortcut."
            report(json.dumps({"spoken": spoken, "transcript": transcript, "route": route.verb,
                               "args": route.args, "refused": True, "say": result["say"]}, ensure_ascii=False))
            continue
        assert code == 0 and result.get("ok") is True, result
        report(json.dumps({"spoken": spoken, "transcript": transcript, "route": route.verb,
                           "args": route.args, "argv": argv, "say": result["say"]}, ensure_ascii=False))
