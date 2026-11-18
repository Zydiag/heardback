"""Daemon integration with real session workers and entirely fake desktop I/O."""
import asyncio
import json
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

np = pytest.importorskip("numpy")

from conftest import FakeCues, FakeMedia, FakeTts
from heardback.voice import cli, config, desktop_dictation as core, desktop_input as boundary
from heardback.voice import loop as voice_loop
from heardback.voice import dictation_ctl
from heardback.voice.chunker import Chunk
from heardback.voice.dictation import DictationError
from heardback.voice.link import Link

PIN = boundary.Pin("0x123", "kitty", 42)
SECRET = "private dictated phrase"


async def checkpoint():
    future = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(future.set_result, None)
    await future


@asynccontextmanager
async def rig(tmp_path, monkeypatch, **settings):
    gpu, cpu = ThreadPoolExecutor(1), ThreadPoolExecutor(1)
    cues, media, tts = FakeCues(), FakeMedia(), FakeTts()
    stt = SimpleNamespace(transcribe_dictation=Mock(return_value=SECRET), transcribe=Mock())
    clock = SimpleNamespace(now=100.0)
    for module in (voice_loop, dictation_ctl):
        monkeypatch.setattr(module, "time", SimpleNamespace(
            monotonic=lambda: clock.now, time=time.time, strftime=time.strftime))
    monkeypatch.setattr(core, "monotonic", lambda: clock.now)
    io = SimpleNamespace(locked=False, armed=False, calls=[], sent=[],
                         entered=asyncio.Event(), release=threading.Event())
    main_loop = asyncio.get_running_loop()

    def read_pin(denied, unsupported, ignore_layers):
        assert threading.current_thread() is not threading.main_thread()
        assert (denied, unsupported, ignore_layers) == (
            v.cfg.dictation_denied_classes, v.cfg.dictation_unsupported_classes,
            v.cfg.dictation_ignore_layers)
        return PIN

    def lock_enter():
        io.locked = True
        return True

    def lock_leave():
        io.locked = False
        io.calls.append("leave")
        return True

    def arm(_seconds):
        io.armed = True
        return True

    def disarm():
        assert not io.locked
        io.armed = False
        io.calls.append("disarm")
        return True

    class Watch:
        def __init__(self, *_args):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    for name, fn in dict(read_pin=read_pin, check=lambda *_: None, FocusWatch=Watch,
                         lock_enter=lock_enter, lock_leave=lock_leave, lock_held=lambda: io.locked,
                         arm_lock_timer=arm, disarm_lock_timer=disarm,
                         send=lambda text, *_: io.sent.append(text) or True).items():
        monkeypatch.setattr(boundary, name, fn)
    cfg = replace(config.Config(duck_media=False), **settings)
    v = voice_loop.Voice(cfg, Link(tmp_path / "unused.sock"), stt=stt, tts=tts, cues=cues,
                         media=media, prob=lambda _: 1.0, gpu=gpu, cpu=cpu)
    v.set_mic_available(True)
    events = asyncio.Queue()
    original = v.desktop.event

    def event(**fields):
        original(**fields)
        main_loop.call_soon_threadsafe(events.put_nowait, fields)

    v.desktop.event = event

    async def wait_event(phase, reason=None):
        async with asyncio.timeout(3):
            while True:
                item = await events.get()
                if item.get("phase") == phase and (reason is None or item.get("reason") == reason):
                    return item

    async def start():
        v.toggle_dictation()
        await wait_event("listening", "listening")

    async def notices():
        await main_loop.run_in_executor(v._io, lambda: None)

    r = SimpleNamespace(v=v, stt=stt, cues=cues, media=media, tts=tts, io=io, clock=clock,
                        start=start, event=wait_event, notices=notices)
    try:
        yield r
    finally:
        io.release.set()
        v.dictation_shutdown()
        await checkpoint()
        if v.desktop._worker:
            await asyncio.wait_for(asyncio.shield(v.desktop._worker), 4)
        await asyncio.to_thread(v._dictation_io.shutdown, wait=True)
        await asyncio.to_thread(v._io.shutdown, wait=True)
        await asyncio.to_thread(gpu.shutdown, wait=True)
        await asyncio.to_thread(cpu.shutdown, wait=True)


def queue_chunk(r):
    r.v.desktop._enqueue(r.v.desktop._session, Chunk(
        0, np.ones(6400, np.int16), "silence", 0.4, 0.4))
    r.v.desktop._kick()


async def test_toggle_starts_then_stops(tmp_path, monkeypatch):
    async with rig(tmp_path, monkeypatch) as r:
        await r.start()
        assert r.v.dictation.engaged and r.v._dictation_mode_active
        r.clock.now += 0.301
        r.v.toggle_dictation()
        await r.event("stopped")
        await checkpoint()
        assert not r.v.dictation.engaged and not r.v._dictation_mode_active
        assert not r.stt.transcribe_dictation.called and not r.io.sent
        await r.notices()
        assert r.cues.played == ["wake", "ready"]
        assert r.cues.sticky == ["Dictating into kitty"]


@pytest.mark.parametrize("reason", ["lock_leave_failed", "disarm_lock_timer_failed", "focus_watch_stop_failed"])
async def test_internal_failure_while_listening_never_announces_a_start(tmp_path, monkeypatch, reason):
    async with rig(tmp_path, monkeypatch) as r:
        await r.start()
        await r.notices()
        played = list(r.cues.played)
        sticky = list(r.cues.sticky)
        r.v.desktop._emit(r.v.desktop._session, reason)
        await r.notices()
        assert r.cues.played == played + ["error"]
        assert r.cues.sticky[:len(sticky)] == sticky
        assert all("Dictating into" not in notice for notice in r.cues.sticky[len(sticky):])
        assert len(r.cues.sticky) == len(sticky) + 1
        assert "failed" in r.cues.sticky[-1].lower()
        assert SECRET not in repr(r.cues.sticky + r.cues.notes)


async def test_press_within_300ms_ignored(tmp_path, monkeypatch):
    async with rig(tmp_path, monkeypatch) as r:
        await r.start()
        r.clock.now += 0.299
        r.v.toggle_dictation()
        assert r.v.desktop.phase == "listening"


async def blocked_pin(r, monkeypatch):
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def read(*_):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(3)
        return PIN

    r.io.release = release
    monkeypatch.setattr(boundary, "read_pin", read)
    r.v.toggle_dictation()
    await asyncio.wait_for(entered.wait(), 2)
    return release


async def test_press_during_start_ignored(tmp_path, monkeypatch):
    async with rig(tmp_path, monkeypatch) as r:
        release = await blocked_pin(r, monkeypatch)
        r.clock.now += 0.5
        r.v.toggle_dictation()
        r.v.toggle_dictation()
        release.set()
        await r.event("listening", "listening")
        assert r.v.desktop.phase == "listening" and not r.v.desktop._session.stop_requested


async def test_sigwinch_during_read_pin(tmp_path, monkeypatch):
    # A signal schedules this synchronous callback; it must return during a blocked lookup.
    await test_press_during_start_ignored(tmp_path, monkeypatch)


async def test_two_presses_inside_one_read_pin(tmp_path, monkeypatch):
    await test_press_during_start_ignored(tmp_path, monkeypatch)


async def test_press_while_stopping_notifies_only(tmp_path, monkeypatch):
    async with rig(tmp_path, monkeypatch) as r:
        await r.start()
        r.clock.now += 1
        r.v.toggle_dictation()
        assert r.v.desktop.phase == "stopping"
        r.clock.now += 1
        r.v.toggle_dictation()
        await r.notices()
        assert any("stopping" in note.lower() for note in r.cues.notes)
        assert r.v.desktop._session.session_id


@pytest.mark.parametrize("field", ["state", "ANNOUNCE", "_busy", "_wake_gate_pending", "_local_speech_count"])
async def test_start_refused_when_voice_busy(tmp_path, monkeypatch, field):
    async with rig(tmp_path, monkeypatch) as r:
        if field == "ANNOUNCE":
            r.v.state = "ANNOUNCE"
        else:
            setattr(r.v, field, "THINK" if field == "state" else True)
        read = Mock(side_effect=AssertionError("must not resolve target"))
        monkeypatch.setattr(boundary, "read_pin", read)
        r.v.toggle_dictation()
        await r.notices()
        assert not read.called and r.v.desktop.metadata() == {"phase": "idle"}
        assert r.cues.notes


async def test_start_refused_when_mic_unavailable(tmp_path, monkeypatch):
    async with rig(tmp_path, monkeypatch) as r:
        r.v.set_mic_available(False)
        r.v.toggle_dictation()
        await r.notices()
        assert not r.v.desktop.engaged and r.cues.notes


async def test_start_refused_when_tmux_session_active(tmp_path, monkeypatch):
    async with rig(tmp_path, monkeypatch) as r:
        r.v.dictation.start({"target": "1", "pane_id": "%1"})
        r.v.toggle_dictation()
        await r.notices()
        assert not r.v.desktop.engaged and r.v.dictation.engaged and r.cues.notes


@pytest.mark.parametrize("code", ["no_active_window", "denied_class", "unsupported_class", "locked",
                                  "wtype_missing", "focus_changed", "destination_closed", "layer_open"])
async def test_read_pin_refusal_notifies_without_capture(tmp_path, monkeypatch, code):
    async with rig(tmp_path, monkeypatch) as r:
        monkeypatch.setattr(boundary, "read_pin", Mock(side_effect=boundary.InputRefused(code)))
        r.v.toggle_dictation()
        await r.v._desktop_start_task
        await r.notices()
        assert not r.v.desktop.engaged and not r.v._dictation_mode_active
        assert not r.stt.transcribe_dictation.called and r.cues.notes


async def test_start_refused_when_wtype_missing(tmp_path, monkeypatch):
    await test_read_pin_refusal_notifies_without_capture(tmp_path, monkeypatch, "wtype_missing")


async def test_voice_turn_during_pin_read_refuses_start(tmp_path, monkeypatch):
    async with rig(tmp_path, monkeypatch) as r:
        release = await blocked_pin(r, monkeypatch)
        r.v.state = "LISTEN"
        release.set()
        await r.v._desktop_start_task
        await r.notices()
        assert not r.v.desktop.engaged and r.cues.notes


async def test_cancel_before_pin_task_runs_prevents_start(tmp_path, monkeypatch):
    async with rig(tmp_path, monkeypatch) as r:
        r.v.toggle_dictation()
        r.v.cancel()
        await r.v._desktop_start_task
        assert not r.v.desktop.engaged


async def test_mic_loss_during_pin_read_refuses_even_after_recovery(tmp_path, monkeypatch):
    async with rig(tmp_path, monkeypatch) as r:
        release = await blocked_pin(r, monkeypatch)
        r.v.set_mic_available(False)
        r.v.set_mic_available(True)
        release.set()
        await r.v._desktop_start_task
        assert not r.v.desktop.engaged


async def test_mic_retarget_during_pin_read_refuses_after_recovery_frame(tmp_path, monkeypatch):
    async with rig(tmp_path, monkeypatch) as r:
        release = await blocked_pin(r, monkeypatch)
        r.v.mic_lost()
        r.v.mic_frame(np.ones(1280, np.int16))
        assert r.v._mic_available
        release.set()
        await r.v._desktop_start_task
        assert not r.v.desktop.engaged


async def test_shutdown_during_pin_read_never_captures(tmp_path, monkeypatch):
    async with rig(tmp_path, monkeypatch) as r:
        release = await blocked_pin(r, monkeypatch)
        r.v.dictation_shutdown()
        release.set()
        await asyncio.gather(r.v._desktop_start_task, return_exceptions=True)
        assert not r.v.desktop.engaged and not r.stt.transcribe_dictation.called


async def test_ptt_pauses_desktop_session(tmp_path, monkeypatch):
    async with rig(tmp_path, monkeypatch) as r:
        await r.start()
        r.v.ptt()
        assert r.v.desktop.phase == "paused" and r.v.desktop.metadata()["reason"] == "ptt"
        assert r.v.state == "IDLE"


async def test_mic_toggle_suspends(tmp_path, monkeypatch):
    async with rig(tmp_path, monkeypatch) as r:
        await r.start()
        r.v.toggle_mic()
        assert r.v.desktop.phase == "paused" and r.v.desktop.metadata()["reason"] == "mic_off"


async def test_automute_suspends(tmp_path, monkeypatch):
    async with rig(tmp_path, monkeypatch) as r:
        await r.start()
        r.v.set_automute(True)
        assert r.v.desktop.phase == "paused" and r.v.desktop.metadata()["reason"] == "auto_mute"


async def test_device_loss_suspends(tmp_path, monkeypatch):
    async with rig(tmp_path, monkeypatch) as r:
        await r.start()
        r.v.set_mic_available(False)
        r.v.set_mic_available(True)
        assert r.v.desktop.phase == "paused" and r.v.desktop.metadata()["reason"] == "device_loss"


async def test_super_escape_latches_cancel_and_leaves_mode(tmp_path, monkeypatch):
    async with rig(tmp_path, monkeypatch) as r:
        await r.start()
        r.v.cancel()
        await r.event("cancelled")
        await checkpoint()
        assert not r.v._dictation_mode_active and not r.v.dictation.engaged
        await r.notices()
        assert any("typed text stays" in note.lower() for note in r.cues.notes)


async def test_mode_left_on_stop_restores_wake_and_drains_pending(tmp_path, monkeypatch):
    async with rig(tmp_path, monkeypatch, duck_media=True) as r:
        reset = Mock()
        r.v.wake = SimpleNamespace(reset=reset, ring=[])
        r.v._dictation_leave = Mock(wraps=r.v._dictation_leave)
        resumed = asyncio.Event()

        async def resume(*_):
            r.media.resume()
            resumed.set()

        monkeypatch.setattr(r.v, "_resume_later", resume)
        # The fake finishes only when the test explicitly acknowledges announcement playback.
        finished = asyncio.Event()

        async def finish_announcement(_turn):
            await finished.wait()
            r.v._idle()

        monkeypatch.setattr(r.v, "_finish_announcement", finish_announcement)
        await r.start()
        r.v._announce("deferred announcement")
        r.clock.now += 1
        r.v.toggle_dictation()
        await r.event("stopped")
        await checkpoint()
        assert r.v._dictation_leave.call_count == 1 and reset.call_count == 2
        assert not r.v.pending and r.v.state == "ANNOUNCE"
        finished.set()
        await asyncio.wait_for(resumed.wait(), 2)
        assert r.v.state == "IDLE" and not r.v._busy
        await r.notices()
        assert "resume" in r.media.calls


async def test_wake_and_command_get_no_frames_while_engaged(tmp_path, monkeypatch):
    async with rig(tmp_path, monkeypatch) as r:
        await r.start()
        score, feed = Mock(), Mock()
        r.v.wake, r.v.gate = SimpleNamespace(score=score), object()
        r.v.ep = SimpleNamespace(feed=feed)
        r.v.feed(np.ones(1280, np.int16))
        assert not score.called and not feed.called and not r.stt.transcribe.called


async def test_mic_frame_reaches_desktop_chunker(tmp_path, monkeypatch):
    async with rig(tmp_path, monkeypatch) as r:
        await r.start()
        chunker = r.v.desktop._session.chunker
        feed = Mock(wraps=chunker.feed)
        monkeypatch.setattr(chunker, "feed", feed)
        frame = np.ones(1280, np.int16)
        r.v.mic_frame(frame)
        feed.assert_called_once()
        assert feed.call_args.args[0] is frame


async def test_announcement_deferred_during_dictation(tmp_path, monkeypatch):
    async with rig(tmp_path, monkeypatch) as r:
        await r.start()
        await r.v._on_message({"type": "say", "announce": True, "text": "notice"})
        assert list(r.v.pending) == ["notice"] and not r.tts.said


async def test_shutdown_during_slice(tmp_path, monkeypatch):
    async with rig(tmp_path, monkeypatch, dictation_slice_chars=8) as r:
        await r.start()
        r.clock.now += 1
        loop = asyncio.get_running_loop()

        def send(text, *_):
            assert r.io.locked
            r.io.sent.append(text)
            loop.call_soon_threadsafe(r.io.entered.set)
            assert r.io.release.wait(3)
            return True

        monkeypatch.setattr(boundary, "send", send)
        queue_chunk(r)
        await asyncio.wait_for(r.io.entered.wait(), 2)
        r.v.dictation_shutdown()
        assert r.v.desktop._session.cancel_requested
        r.io.release.set()
        await r.event("cancelled")
        assert len(r.io.sent) == 1 and not r.io.locked and not r.io.armed


async def test_stop_while_lock_held_records_shortcut_and_waits(tmp_path, monkeypatch):
    async with rig(tmp_path, monkeypatch, dictation_slice_chars=8) as r:
        await r.start()
        r.clock.now += 1
        loop = asyncio.get_running_loop()
        left = asyncio.Event()
        leave = boundary.lock_leave

        def send(text, *_):
            r.io.sent.append((text, r.clock.now))
            if len(r.io.sent) == 1:
                loop.call_soon_threadsafe(r.io.entered.set)
                assert r.io.release.wait(3)
            return True

        def lock_leave():
            result = leave()
            loop.call_soon_threadsafe(left.set)
            return result

        monkeypatch.setattr(boundary, "send", send)
        monkeypatch.setattr(boundary, "lock_leave", lock_leave)
        queue_chunk(r)
        await asyncio.wait_for(r.io.entered.wait(), 2)
        r.v.toggle_dictation()
        pressed = r.clock.now
        assert r.v.desktop._shortcut_at == pressed
        r.io.release.set()
        await asyncio.wait_for(left.wait(), 2)
        assert len(r.io.sent) == 1
        r.clock.now += 0.492
        await r.event("stopped")
        assert len(r.io.sent) > 1 and all(at >= pressed + .491 for _, at in r.io.sent[1:])


async def test_status_reports_desktop_session(tmp_path, monkeypatch):
    async with rig(tmp_path, monkeypatch) as r:
        await r.start()
        result = await r.v.dictation_control({"action": "status"})
        assert result["mode"] == "desktop" and result["class"] == "kitty"
        assert result["phase"] == "listening"
        assert SECRET not in json.dumps(result)
        assert r.v.dictation_dashboard()["mode"] == "desktop"


def test_dashboard_desktop_and_tmux_text_remain_read_only():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to exercise the dashboard renderer")
    page = Path("src/heardback/dash/page.html").read_text()
    renderer = "function dictation(d) {" + page.split("function dictation(d) {", 1)[1].split("function ", 1)[0]
    script = """
const assert = require('node:assert/strict');
const nodes = new Map();
const $ = selector => {
  if (!nodes.has(selector)) nodes.set(selector, {textContent: '', hidden: false});
  return nodes.get(selector);
};
const NA = 'not available';
""" + renderer + r"""
dictation({mode: 'desktop', phase: 'stopped', class: 'kitty',
  delivered: '<script>delivered</script>', held: 'held words', delivered_chars: 26, held_chars: 10});
assert.equal($('#dictation').hidden, false);
assert.equal($('#dictation-target').textContent, 'kitty');
assert.match($('#dictation-text').textContent, /Delivered.*\n<script>delivered<\/script>/);
assert.match($('#dictation-text').textContent, /Held.*\nheld words/);
assert.equal($('#dictation-text').innerHTML, undefined);
dictation({phase: 'review', provisional: true, text: 'tmux preview', target: '1:0.0', pane_id: '%1'});
assert.equal($('#dictation-text').textContent, 'tmux preview');
assert.equal($('#dictation-target').textContent, '1:0.0, pane %1');
dictation(null);
assert.equal($('#dictation').hidden, true);
assert.equal($('#dictation-text').textContent, '');
"""
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)


def test_dashboard_stopped_sessions_keep_held_text_and_clear_when_empty():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to exercise the dashboard renderer")
    page = Path("src/heardback/dash/page.html").read_text()
    renderer = "function dictation(d) {" + page.split("function dictation(d) {", 1)[1].split("function ", 1)[0]
    script = """
const assert = require('node:assert/strict');
const nodes = new Map();
const $ = selector => {
  if (!nodes.has(selector)) nodes.set(selector, {textContent: '', hidden: false});
  return nodes.get(selector);
};
const NA = 'not available';
""" + renderer + r"""
dictation({mode: 'desktop', phase: 'listening', class: 'kitty', delivered: 'private words'});
assert.equal($('#dictation').hidden, false);
dictation({mode: 'desktop', phase: 'stopped', class: 'kitty', delivered: 'private words', held: 'held words'});
assert.equal($('#dictation').hidden, false);
assert.equal($('#dictation-detail').hidden, false);
assert.match($('#dictation-text').textContent, /Held.*\nheld words/);
assert.equal($('#dictation-text').innerHTML, undefined);
dictation({mode: 'desktop', phase: 'stopped', class: 'kitty', delivered: 'private words', held: ''});
assert.equal($('#dictation').hidden, true);
assert.equal($('#dictation-detail').hidden, true);
assert.equal($('#dictation-text').textContent, '');
for (const name of ['session', 'phase', 'revision', 'target', 'reason', 'text', 'kind']) {
  assert.equal($(`#dictation-${name}`).textContent, '');
}
"""
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)


def test_shutdown_spec_documents_systemd_killing_inflight_wtype():
    path = Path("docs/plans/10-desktop-dictation-spec.md")
    if not path.is_file():
        pytest.skip("internal desktop plan is omitted from the public export")
    spec = path.read_text()
    shutdown = next(line for line in spec.splitlines() if line.startswith("| Daemon stop or crash |"))
    assert "in-flight `wtype` is killed" in shutdown
    assert "`ExecStopPost`" in shutdown and "typing lock" in shutdown
    assert "gets its timeout on clean shutdown" not in shutdown


async def test_cli_resume_requires_session_and_reaches_desktop(tmp_path, monkeypatch, capsys):
    async with rig(tmp_path, monkeypatch) as r:
        with pytest.raises(SystemExit):
            cli.main(["dictation", "resume"])
        await r.start()
        r.v.ptt()
        sid = r.v.desktop.metadata()["session_id"]
        with pytest.raises(DictationError, match="stale_session"):
            await r.v.dictation_control({"action": "resume", "session_id": "f" * 32})
        from heardback.voice import control
        requests = []
        monkeypatch.setattr(control, "request", lambda request: requests.append(request) or {
            "ok": True, "mode": "desktop", "class": "kitty", "phase": "paused"})
        assert cli.main(["dictation", "resume", "--session", sid]) == 0
        await r.v.dictation_control(requests[0])
        await r.event("listening", "listening")
        assert r.v.desktop._session.ready
        assert '"mode": "desktop"' in capsys.readouterr().out


async def test_tmux_dictation_unchanged(tmp_path, monkeypatch):
    async with rig(tmp_path, monkeypatch) as r:
        from jv import tmux
        monkeypatch.setattr(tmux, "dictation_target", lambda _: {"target": "1", "pane_id": "%1"})
        result = await r.v.dictation_control({"action": "start", "target": "1"})
        sid = result["session_id"]
        r.v.ptt()
        assert r.v.dictation.phase == "paused" and not r.v.desktop.engaged
        await r.v.dictation_control({"action": "resume", "session_id": sid})
        assert r.v.dictation.phase == "listening"
        r.v.cancel()
        assert not r.v.dictation.engaged


async def test_notifications_never_contain_text(tmp_path, monkeypatch, capsys):
    async with rig(tmp_path, monkeypatch) as r:
        await r.start()
        r.clock.now += 1
        queue_chunk(r)
        r.v.toggle_dictation()
        r.clock.now += 0.492
        await r.event("stopped")
        await r.notices()
        assert "".join(r.io.sent) == SECRET
        assert SECRET not in json.dumps(r.cues.notes + r.cues.sticky)
        assert SECRET not in capsys.readouterr().out


@pytest.mark.parametrize("key,value", [
    ("dictation_silence_s", "0"), ("dictation_soft_cap_s", "21"),
    ("dictation_soft_silence_s", "0.7"), ("dictation_hard_cap_s", "inf"),
    ("dictation_min_burst_s", "-1"), ("dictation_min_voiced_s", "nan"),
    ("dictation_slice_chars", "0"), ("dictation_slice_chars", "49"),
    ("dictation_type_delay_ms", "-1"), ("dictation_type_delay_ms", "100"),
    ("dictation_post_shortcut_ms", "299"), ("dictation_max_session_s", "0"),
    ("dictation_denied_classes", '["ok", 1]'), ("dictation_paste_classes", '"kitty"'),
    ("dictation_ignore_layers", '[""]'), ("dictation_type_delay_ms", "true"),
])
def test_config_rejects_bad_dictation_values(tmp_path, key, value):
    path = tmp_path / "voice.toml"
    path.write_text(f"{key} = {value}\n")
    with pytest.raises(ValueError, match="dictation"):
        config.load(path)


def test_paste_classes_default_empty():
    assert config.Config().dictation_paste_classes == ()


def test_service_unit_has_execstoppost_submap_reset(monkeypatch):
    unit = Path("systemd/heardback.service").read_text()
    assert "ExecStopPost=-%h/.local/bin/heardback-voice dictation-reset-lock" in unit
    assert "HYPRLAND_INSTANCE_SIGNATURE" not in unit
    reset = Mock(return_value=True)
    monkeypatch.setattr(boundary, "reset_lock", reset)
    assert cli.main(["dictation-reset-lock"]) == 0
    reset.assert_called_once_with()


async def run_fake_daemon(tmp_path, monkeypatch, voice=None, *, reset=None, warm=None):
    from heardback.voice import daemon
    loop = asyncio.get_running_loop()
    added, removed = {}, []
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, fn: added.setdefault(sig, fn))
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: removed.append(sig))
    if voice is None:
        voice = Mock()
        voice.run = AsyncMock()
        voice.desktop._worker = None
    monkeypatch.setattr(daemon, "Voice", Mock(return_value=voice))
    monkeypatch.setattr(daemon, "warm", warm or (lambda *_: (object(), object(), False)))
    monkeypatch.setattr(daemon, "silero", lambda: object())
    monkeypatch.setattr(daemon, "load_wake", lambda _: None)
    monkeypatch.setattr(daemon, "load_speaker", lambda _: None)
    monkeypatch.setattr(daemon, "_pick", lambda _: "fake-mic")
    for name in ("MicSource", "Watcher", "Cues", "Media"):
        monkeypatch.setattr(daemon, name, Mock())
    daemon.MicSource.return_value.node = "fake-mic"
    from heardback.dash import sources
    monkeypatch.setattr(sources, "LIVE", {})
    server = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(daemon, "ControlServer", lambda _: server)
    reset = reset or Mock(return_value=True)
    monkeypatch.setattr(boundary, "reset_lock", reset)
    await daemon.run(config.Config(), tmp_path / "unused.sock")
    return added, removed, voice, reset


async def test_signal_handler_removed_on_exit(tmp_path, monkeypatch):
    import signal
    added, removed, voice, _ = await run_fake_daemon(tmp_path, monkeypatch)
    assert added[signal.SIGWINCH] == voice.toggle_dictation
    assert set(added) == set(removed)


async def test_startup_resets_stale_lock(tmp_path, monkeypatch):
    _, _, _, reset = await run_fake_daemon(tmp_path, monkeypatch)
    reset.assert_called_once_with()


@pytest.mark.parametrize("outcome", [True, False, OSError("unavailable")])
async def test_startup_cleanup_does_not_block_voice(tmp_path, monkeypatch, outcome):
    from heardback.voice import daemon as module
    async with rig(tmp_path, monkeypatch) as r:
        loop = asyncio.get_running_loop()
        entered, warmed, ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
        finish = asyncio.Event()
        release = threading.Event()

        def reset():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        def warm(*_):
            loop.call_soon_threadsafe(warmed.set)
            return r.stt, r.tts, False

        async def run():
            ready.set()
            await finish.wait()

        monkeypatch.setattr(r.v, "run", run)
        task = asyncio.create_task(run_fake_daemon(tmp_path, monkeypatch, r.v, reset=reset, warm=warm))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.wait_for(warmed.wait(), 2)
            await asyncio.wait_for(ready.wait(), 2)
            module.MicSource.return_value.start.assert_called_once_with()
            # A real callback frame restores the daemon's mic-ready probe.
            module.MicSource.call_args.kwargs["on_frame"](np.ones(1280, np.int16))
            await checkpoint()
            read = Mock(return_value=PIN)
            monkeypatch.setattr(boundary, "read_pin", read)
            r.v.toggle_dictation()
            await r.notices()
            read.assert_not_called()
            assert not r.v.desktop.engaged and r.cues.notes
            release.set()
            # Drain the owned cleanup task rather than guessing at thread timing.
            assert await asyncio.wait_for(asyncio.shield(r.v._desktop_reset_task), 2) is (outcome is True)
            r.clock.now += 1
            r.v.toggle_dictation()
            if outcome is True:
                await r.event("listening", "listening")
                assert r.v.desktop.engaged
                r.v.cancel()
                await r.event("cancelled")
            else:
                await r.notices()
                read.assert_not_called()
                assert not r.v.desktop.engaged
                # Both existing tmux capture and the command voice path remain usable.
                r.v.dictation.start({"target": "1", "pane_id": "%1"})
                assert r.v.dictation.engaged
                r.v.cancel()
                said = asyncio.Event()
                say = r.tts.say

                async def speak(text):
                    await say(text)
                    said.set()

                monkeypatch.setattr(r.tts, "say", speak)
                r.v._speak_local("Voice still works.")
                await asyncio.wait_for(said.wait(), 2)
                assert "Voice still works." in r.tts.said
        finally:
            release.set()
            finish.set()
            await asyncio.wait_for(task, 3)


async def test_shutdown_drains_blocked_startup_cleanup(tmp_path, monkeypatch):
    from heardback.voice import daemon as module
    async with rig(tmp_path, monkeypatch) as r:
        loop = asyncio.get_running_loop()
        entered, ready, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
        finish = asyncio.Event()
        release = threading.Event()

        def reset():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
            return True

        async def run():
            ready.set()
            await finish.wait()

        shutdown = r.v.dictation_shutdown

        def shutdown_mark():
            shutdown()
            stopped.set()

        monkeypatch.setattr(r.v, "run", run)
        monkeypatch.setattr(r.v, "dictation_shutdown", shutdown_mark)
        task = asyncio.create_task(run_fake_daemon(tmp_path, monkeypatch, r.v, reset=reset))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.wait_for(ready.wait(), 2)
            finish.set()
            await asyncio.wait_for(stopped.wait(), 2)
            assert not task.done() and not r.v._desktop_reset_task.done()
            release.set()
            added, removed, _, _ = await asyncio.wait_for(task, 3)
            assert r.v._desktop_reset_task.done() and not r.v._desktop_reset_task.cancelled()
            assert set(added) == set(removed)
            module.MicSource.return_value.stop.assert_called_once_with()
        finally:
            release.set()
            finish.set()
            await asyncio.wait_for(task, 3)


async def test_daemon_waits_for_claimed_slice_cleanup(tmp_path, monkeypatch):
    async with rig(tmp_path, monkeypatch, dictation_slice_chars=8) as r:
        loop = asyncio.get_running_loop()
        shutting_down = asyncio.Event()
        shutdown = r.v.dictation_shutdown

        def shutdown_mark():
            shutdown()
            shutting_down.set()

        def send(text, *_):
            r.io.sent.append(text)
            loop.call_soon_threadsafe(r.io.entered.set)
            assert r.io.release.wait(3)
            return True

        async def run():
            r.v.mic_available_probe = lambda: True
            await r.start()
            r.clock.now += 1
            queue_chunk(r)
            await r.io.entered.wait()

        monkeypatch.setattr(r.v, "dictation_shutdown", shutdown_mark)
        monkeypatch.setattr(r.v, "run", run)
        monkeypatch.setattr(boundary, "send", send)
        daemon = asyncio.create_task(run_fake_daemon(tmp_path, monkeypatch, r.v))
        try:
            await asyncio.wait_for(shutting_down.wait(), 2)
            assert not daemon.done() and r.io.locked
            r.io.release.set()
            await asyncio.wait_for(daemon, 3)
            assert len(r.io.sent) == 1 and not r.io.locked and not r.io.armed
        finally:
            r.io.release.set()
            await asyncio.gather(daemon, return_exceptions=True)


async def test_startup_uses_real_reset_key_recovery(tmp_path, monkeypatch):
    monkeypatch.delenv("JV_DRY", raising=False)
    recover = Mock()
    monkeypatch.setattr(boundary, "release_keys", recover)
    monkeypatch.setattr(boundary.hypr, "hyprctl", lambda *a, **kw: "default")
    monkeypatch.setattr(boundary, "disarm_lock_timer", lambda: True)
    real_reset = boundary.reset_lock
    await run_fake_daemon(tmp_path, monkeypatch, reset=real_reset)
    recover.assert_called_once_with()
