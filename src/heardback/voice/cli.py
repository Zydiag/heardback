"""`heardback-voice` subcommands."""
import argparse
import json


_DICTATION_METADATA = frozenset({"session_id", "phase", "reason", "target", "pane_id", "chars",
                                 "revision", "final", "commit_started", "operation", "mode", "class",
                                 "delivered_chars", "held_chars"})
_DICTATION_ERRORS = frozenset({
    "commit_consumed", "confirmation_not_pending", "control_error", "duration_limit",
    "inference_busy", "insert_uncertain", "insertion_in_progress", "invalid_action",
    "invalid_fields", "invalid_request", "invalid_response", "invalid_revision",
    "invalid_session", "invalid_target", "mic_unavailable", "no_session", "not_finishable",
    "not_listening", "not_paused", "not_reviewable", "request_too_large", "response_too_large",
    "session_active", "stale_revision", "stale_session", "target_timeout", "target_unavailable",
    "timeout", "voice_busy", "wrong_user", "unsafe_text",
})


def main(argv=None) -> int:
    from .extra import INSTALL, MODULES
    try:
        return _main(argv)
    except ModuleNotFoundError as e:
        if (e.name or "").split(".")[0] not in MODULES:
            raise
        print(INSTALL)
        return 1


def _main(argv=None) -> int:
    from heardback.config import load as desktop_config
    ap = argparse.ArgumentParser(prog="heardback-voice")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("setup", help="download the pinned models")
    p.add_argument("--impostors", action="store_true", help="also fetch 30 LibriSpeech speakers for tune")
    p = sub.add_parser("say", help="speak text with the configured voice")
    p.add_argument("--voice")
    p.add_argument("--volume", type=float, help="gain on the voice (default: tts_volume)")
    p.add_argument("text")
    p = sub.add_parser("run-file", help="push a wav through endpoint, STT and the brain socket")
    p.add_argument("wav")
    p.add_argument("--sock")
    p.add_argument("--wake", action="store_true", help="start the turn on the wake word, not push-to-talk")
    p = sub.add_parser("miccheck", help="mic mode word error rate (you speak 20 prompts)")
    p.add_argument("--mic", choices=list(desktop_config().modes), required=True)
    p.add_argument("--distance", choices=["desk", "room"], required=True)
    sub.add_parser("devices", help="show the chosen mic, the default sink, Bluetooth profile/codec")
    p = sub.add_parser("enroll", help="record your wake, reading, yes/no and background clips")
    p.add_argument("--part", choices=["wake_desk", "wake_room", "reading", "yesno", "background"])
    sub.add_parser("tune", help="pick thresholds from the enrollment clips")
    p = sub.add_parser("daemon", help="voice only, against a brain socket that is already up")
    p.add_argument("--sock")
    p = sub.add_parser("trace", help="trace.jsonl as a per-turn timeline")
    p.add_argument("--last", type=int, default=10, metavar="N", help="last N turns (default 10)")
    p.add_argument("--turn", metavar="ID", help="turns whose id contains ID")
    p.add_argument("--errors", action="store_true", help="only turns with an error, denial, timeout or reject")
    p.add_argument("--file", help="trace file (default ~/.local/state/heardback/trace.jsonl)")
    p = sub.add_parser("dictation", help="control preview-first local dictation")
    dictation = p.add_subparsers(dest="dictation_cmd", required=True)
    q = dictation.add_parser("start", help="start dictation for a pinned tmux target")
    q.add_argument("--target", required=True, metavar="SELECTOR")
    q = dictation.add_parser("status", help="show the active dictation session metadata")
    q.add_argument("--session", metavar="ID", help="require this session to be active")
    for name, description in (
        ("pause", "pause audio capture"),
        ("resume", "resume a paused session"),
        ("finish", "transcribe and queue the final insertion"),
        ("cancel", "discard the session preview"),
    ):
        q = dictation.add_parser(name, help=description)
        q.add_argument("--session", required=True, metavar="ID")
    q = dictation.add_parser("commit", help="confirm a check-required insertion")
    q.add_argument("--session", required=True, metavar="ID")
    q.add_argument("--revision", required=True, type=int, metavar="N")
    sub.add_parser("dictation-reset-lock", help="leave a stale dictation typing lock")
    a = ap.parse_args(argv)
    if a.cmd in {"setup", "say", "run-file", "miccheck", "enroll", "tune", "daemon"}:
        from .extra import INSTALL, missing
        if missing():
            print(INSTALL)
            return 1
    if a.cmd == "dictation-reset-lock":
        from .desktop_input import reset_lock
        return 0 if reset_lock() else 1
    elif a.cmd == "setup":
        from . import models
        models.setup(a.impostors)
    elif a.cmd == "say":
        import asyncio
        from .config import load
        from .tts import Tts
        cfg = load()

        async def speak():
            tts = Tts(a.voice or cfg.tts_voice, cfg.stt_device,
                      volume=cfg.tts_volume if a.volume is None else a.volume)
            await tts.say(a.text)
            await tts.finish()
        asyncio.run(speak())
    elif a.cmd == "run-file":
        import asyncio
        from pathlib import Path
        from .runfile import default_sock, run_file
        try:
            return asyncio.run(run_file(Path(a.wav), Path(a.sock) if a.sock else default_sock(), a.wake))
        except ValueError as e:
            print(f"run-file: {e}")
            return 1
    elif a.cmd == "miccheck":
        from .miccheck import run
        return run(a.mic, a.distance)
    elif a.cmd == "devices":
        from .config import load
        from .devices import describe
        cfg = load()
        print(describe(cfg.mic, cfg.mic_node))
    elif a.cmd == "enroll":
        from .enroll import run
        return run([a.part] if a.part else None)
    elif a.cmd == "tune":
        from .tune import tune
        tune()
    elif a.cmd == "trace":
        from pathlib import Path
        from .traceview import main as trace_main
        return trace_main(a.last, a.turn, a.errors, Path(a.file) if a.file else None)
    elif a.cmd == "dictation":
        return _dictation_command(a)
    elif a.cmd == "daemon":
        import asyncio
        from pathlib import Path
        from .config import load
        from .daemon import run
        from .runfile import default_sock
        try:
            asyncio.run(run(load(), Path(a.sock) if a.sock else default_sock()))
        except KeyboardInterrupt:
            pass
    return 0


def _dictation_command(args) -> int:
    """Send one allowlisted request and print bounded metadata, never preview text."""
    from .control import request

    action = args.dictation_cmd
    payload = {"action": action}
    if action == "start":
        payload["target"] = args.target
    elif action == "status":
        if args.session:
            payload["session_id"] = args.session
    else:
        payload["session_id"] = args.session
        if action == "commit":
            payload["revision"] = args.revision

    try:
        result = request(payload)
    except TimeoutError:
        if action == "status":
            _dictation_status_timeout(args.session)
        else:
            _dictation_uncertain(args.session if action != "start" else None)
        return 2
    except OSError:
        print("dictation: control socket unavailable; check that the voice service is running")
        return 1
    except ValueError:
        print("dictation: invalid response")
        return 1

    if not result.get("ok"):
        candidate = result.get("error")
        error = candidate if isinstance(candidate, str) and candidate in _DICTATION_ERRORS else "control_error"
        if error == "timeout":
            if action == "status":
                _dictation_status_timeout(args.session)
            else:
                _dictation_uncertain(args.session if action != "start" else None)
        else:
            print(f"dictation: {error}")
        return 2 if error == "timeout" else 1

    metadata = {key: result[key] for key in _DICTATION_METADATA if key in result}
    print(json.dumps(metadata, ensure_ascii=False, sort_keys=True))
    if action in {"finish", "commit"}:
        print("dictation: accepted work; insertion/rendering is not yet confirmed")
    return 0


def _dictation_uncertain(session_id: str | None) -> None:
    selector = f" --session {session_id}" if session_id else ""
    print("dictation: response timed out; operation may have been accepted. "
          f"Check `heardback-voice dictation status{selector}`; do not retry blindly.")


def _dictation_status_timeout(session_id: str | None) -> None:
    selector = f" --session {session_id}" if session_id else ""
    print("dictation: status request timed out; repeat the read-only check with "
          f"`heardback-voice dictation status{selector}`.")
