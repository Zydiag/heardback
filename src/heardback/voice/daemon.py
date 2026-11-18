"""Wires the real parts into `Voice`: warm-up, mic + hot-plug watcher, signals. Started by `heardbackd`."""
import asyncio
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from .audio_in import RATE, MicSource
from .config import Config, log
from .control import ControlServer
from .cues import Cues
from .devices import Watcher, other_recorders, pactl_json, pick_mic
from . import desktop_input
from .endpoint import silero
from .link import Link
from .loop import Voice
from .media import Media
from .stt import Stt, load_catalogue, local_words
from .speaker import load_speaker
from .text import hotwords
from .tts import Tts
from .wake import load_wake
from heardback.sdnotify import sd_notify, watchdog


def warm(cfg: Config, gpu) -> tuple[Stt, Tts, bool]:
    """Load STT + Kokoro and run each once (the first CUDA call is slow). Returns (stt, tts, on_cpu)."""
    stt = Stt(cfg, hotwords(load_catalogue(), cfg.stt_hotwords, local_words()))      # falls back to CPU by itself
    on_cpu = stt.device == "cpu"
    try:
        tts = Tts(cfg.tts_voice, "cpu" if on_cpu else cfg.stt_device, pool=gpu, volume=cfg.tts_volume)
        tts.synth("ready")
    except Exception as e:  # CUDA error: Kokoro on CPU
        log("tts_load_failed", error=str(e)[:200])
        tts, on_cpu = Tts(cfg.tts_voice, "cpu", pool=gpu, volume=cfg.tts_volume), True
        tts.synth("ready")
    return stt, tts, on_cpu


def _pick(cfg: Config, fallback: bool = False) -> str | None:
    try:
        node, fell_back = pick_mic(pactl_json("list", "sources"), cfg.mic, cfg.mic_node, fallback=fallback)
    except ValueError:
        log("no_mic")
        return None
    if fell_back:
        log("mic_fallback", node=node)
    return node


async def run(cfg: Config, sock: Path, voice_token: str | None = None) -> None:
    async def reset_lock() -> bool:
        try:
            reset = await asyncio.to_thread(desktop_input.reset_lock)
        except Exception:
            reset = False
        if not reset:
            log("dictation_lock_reset_failed")
        return reset

    reset_task = asyncio.create_task(reset_lock())
    try:
        await _run(cfg, sock, reset_task, voice_token)
    finally:
        # Drain bounded subprocess cleanup even if startup or voice exits early.
        await asyncio.shield(reset_task)


async def _run(cfg: Config, sock: Path, reset_task: asyncio.Task, voice_token: str | None = None) -> None:
    loop = asyncio.get_running_loop()
    gpu, cpu = ThreadPoolExecutor(1, "heardback-gpu"), ThreadPoolExecutor(1, "heardback-cpu")
    t0 = time.monotonic()
    stt, tts, on_cpu = await loop.run_in_executor(gpu, warm, cfg, gpu)
    prob = await loop.run_in_executor(gpu, silero)
    wake = await loop.run_in_executor(cpu, load_wake, cfg)   # None until hey_claude.onnx exists
    speaker = load_speaker(cfg)                              # None while speaker_check = "off"
    if speaker is not None:                                  # spec §2.7 warm-up: one ECAPA embedding
        await loop.run_in_executor(cpu, speaker.embed, np.zeros(RATE, np.int16))
    cues = Cues(cfg.notify)
    voice = Voice(cfg, Link(sock, voice_token), stt=stt, tts=tts, cues=cues, media=Media(cfg.duck_media), prob=prob,
                  wake=wake, speaker=speaker, gpu=gpu, cpu=cpu)
    voice._desktop_reset_task = reset_task
    mic_ready = threading.Event()
    voice.mic_available_probe = mic_ready.is_set

    def failed(n: int) -> None:
        if n == 3:
            cues.play("error")
            cues.notify("heardback lost the mic")

    def mic_frame(frame: np.ndarray) -> None:
        mic_ready.set()
        loop.call_soon_threadsafe(voice.mic_frame, frame)

    def mic_restart() -> None:
        # A reader restart means the selected device is no longer supplying frames.
        # Availability returns only with a real frame; dictation stays paused meanwhile.
        mic_ready.clear()
        loop.call_soon_threadsafe(voice.set_mic_available, False)

    mic = MicSource(_pick(cfg) or "", on_frame=mic_frame,
                    on_fail=lambda n: loop.call_soon_threadsafe(failed, n), on_restart=mic_restart)

    def retarget(node: str | None) -> None:
        if not node:
            mic_ready.clear()
            voice.set_mic_available(False)
            if mic.running:
                loop.run_in_executor(None, mic.stop)
            return
        if node != mic.node:
            mic_ready.clear()
            log("mic_switch", node=node)
            voice.mic_lost()
            mic.retarget(node)
        if voice.mic_on and not mic.running:
            mic.start()

    def on_change(facility: str) -> None:            # watcher thread: pactl calls block
        if facility == "source-output":
            busy = bool(other_recorders(pactl_json("list", "source-outputs"), pactl_json("list", "sources")))
            loop.call_soon_threadsafe(voice.set_automute, busy and cfg.automute_when_mic_in_use)
        else:
            loop.call_soon_threadsafe(retarget, _pick(cfg))

    def toggled(on: bool) -> None:
        if on:
            mic.start()
        else:
            loop.run_in_executor(None, mic.stop)    # joins the reader thread

    def lost_fallback() -> None:
        async def reselect():
            node = await loop.run_in_executor(None, _pick, cfg, True)
            if node and node != mic.node:
                retarget(node)
                cues.notify(f"Microphone lost, using {node}")
        voice._bg(reselect())

    voice.on_mic_toggle, voice.on_mic_lost_fallback = toggled, lost_fallback
    if mic.node:
        mic.start()
    watcher = Watcher(on_change)
    watcher.start()
    loop.add_signal_handler(signal.SIGUSR1, voice.ptt)
    loop.add_signal_handler(signal.SIGUSR2, voice.toggle_mic)
    loop.add_signal_handler(signal.SIGURG, voice.cancel)   # Super+Escape
    loop.add_signal_handler(signal.SIGWINCH, voice.toggle_dictation)
    from heardback.dash import sources   # what this process really loaded, for the dashboard
    sources.LIVE.update(voice=voice, mic=mic, cfg=cfg, wake=wake is not None, speaker=speaker is not None, models=[
        {"name": f"whisper {getattr(stt, 'repo', '?').split('/')[-1]}", "device": getattr(stt, "device", None)},
        {"name": f"kokoro {cfg.tts_voice}", "device": "cpu" if on_cpu else cfg.stt_device},
        {"name": "silero vad (onnx)", "device": "cpu"},
        *([{"name": "openwakeword hey_claude", "device": "cpu"}] if wake is not None else []),
        *([{"name": "ecapa speaker check", "device": getattr(speaker, "device", None)}] if speaker is not None else [])])
    cues.play("ready")
    log("ready", warmup_s=round(time.monotonic() - t0, 2), mic=mic.node, cpu=on_cpu, wake=wake is not None,
        speaker=speaker is not None)
    if on_cpu:
        voice._speak_local("Running on CPU.")
    if wake is None:
        cues.notify("No wake model configured. Use push-to-talk to speak.")
    control = ControlServer(voice)
    sd_notify("READY=1")
    wd = asyncio.create_task(watchdog(gpu, cpu, voice, mic))
    try:
        await control.start()
        await voice.run()
    finally:
        wd.cancel()
        voice.dictation_shutdown()
        # A claimed slice settles before its worker leaves the lock and disarms the timer.
        await asyncio.sleep(0)
        if voice.desktop._worker is not None:
            await asyncio.shield(voice.desktop._worker)
        await control.stop()
        loop.remove_signal_handler(signal.SIGUSR1)
        loop.remove_signal_handler(signal.SIGUSR2)
        loop.remove_signal_handler(signal.SIGURG)
        loop.remove_signal_handler(signal.SIGWINCH)
        watcher.stop()
        await loop.run_in_executor(None, mic.stop)
        voice._dictation_io.shutdown(wait=False, cancel_futures=True)
        gpu.shutdown(wait=False, cancel_futures=True)
        cpu.shutdown(wait=False, cancel_futures=True)

