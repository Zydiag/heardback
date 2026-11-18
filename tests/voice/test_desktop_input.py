"""Desktop boundary tests: all process/compositor calls are fakes, sockets are pairs."""
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from heardback.voice import desktop_input as di

REAL_POPEN = subprocess.Popen
REAL_RUN = subprocess.run
REAL_RELEASE = di.release_keys


PIN = ("0xabc", "kitty", 123)
IGNORED = ("waybar", "awww-daemon", "swaync-notification-window")


@pytest.fixture(autouse=True)
def boundary(monkeypatch):
    monkeypatch.setattr(di, "release_keys", Mock())
    state = {"activewindow": dict(zip(("address", "class", "pid"), PIN), xwayland=False),
             "layers": {}, "submap": "default"}

    def ctl(*args, **kwargs):
        if args[0] == "dispatch":
            state["submap"] = "typinglock" if '"typinglock"' in args[1] else "default"
            return "ok"
        value = state[args[0]]
        if isinstance(value, Exception):
            raise value
        return value

    run = Mock(return_value=subprocess.CompletedProcess([], 1, "", ""))
    monkeypatch.setattr(di.hypr, "hyprctl", ctl)
    monkeypatch.setattr(di.hypr, "instance", lambda: "fresh-instance")
    monkeypatch.setattr(di.subprocess, "run", run)
    monkeypatch.setattr(di.subprocess, "Popen", Mock(side_effect=AssertionError("unfaked Popen")))
    monkeypatch.setattr(di.shutil, "which", lambda name: "/usr/bin/" + name)
    # No test may accidentally create a live socket connection.
    monkeypatch.setattr(di, "socket", SimpleNamespace(
        socket=Mock(side_effect=AssertionError("use socketpair")), timeout=socket.timeout,
        AF_UNIX=socket.AF_UNIX, SOCK_STREAM=socket.SOCK_STREAM, SHUT_RDWR=socket.SHUT_RDWR))
    return SimpleNamespace(state=state, run=run)


def refuses(code, call):
    with pytest.raises(di.InputRefused) as caught:
        call()
    assert caught.value.code == code
    assert str(caught.value) == code


def test_read_pin_empty_workspace_refuses(boundary):
    boundary.state["activewindow"] = {}
    refuses("no_active_window", lambda: di.read_pin((), (), IGNORED))


def test_read_pin_denied_and_unsupported_classes(boundary):
    refuses("denied_class", lambda: di.read_pin(("KiTtY",), (), IGNORED))
    refuses("unsupported_class", lambda: di.read_pin((), ("KITTY",), IGNORED))
    assert di.read_pin((), (), IGNORED) == di.Pin(*PIN)


def test_read_pin_refuses_xwayland_window(boundary):
    boundary.state["activewindow"]["xwayland"] = True
    refuses("unsupported_class", lambda: di.read_pin((), (), IGNORED))


def test_read_pin_requires_wtype(monkeypatch):
    monkeypatch.setattr(di.shutil, "which", lambda _: None)
    refuses("wtype_missing", lambda: di.read_pin((), (), IGNORED))


def test_read_pin_uses_configured_ignore_layers(boundary):
    boundary.state["layers"] = {"monitor": {"levels": {"top": [{"namespace": "custom"}]}}}
    assert di.read_pin((), (), ("custom",)) == di.Pin(*PIN)
    refuses("layer_open", lambda: di.read_pin((), (), ()))


def test_reset_lock_leaves_then_disarms(monkeypatch, boundary):
    monkeypatch.delenv("JV_DRY", raising=False)
    boundary.state["submap"] = "typinglock"
    calls = []
    monkeypatch.setattr(di, "lock_leave", lambda: calls.append("leave") or True)
    monkeypatch.setattr(di, "disarm_lock_timer", lambda: calls.append("disarm") or True)
    assert di.reset_lock()
    assert calls == ["leave", "disarm"]


def test_reset_lock_keeps_backstop_if_leave_fails(monkeypatch, boundary):
    monkeypatch.delenv("JV_DRY", raising=False)
    boundary.state["submap"] = "typinglock"
    monkeypatch.setattr(di, "lock_leave", lambda: False)
    disarm = Mock()
    monkeypatch.setattr(di, "disarm_lock_timer", disarm)
    assert not di.reset_lock()
    disarm.assert_not_called()


@pytest.mark.parametrize("submap", ["resize", "default", "typinglock"])
def test_reset_lock_only_dispatches_for_owned_submap(monkeypatch, boundary, submap):
    monkeypatch.delenv("JV_DRY", raising=False)
    boundary.state["submap"] = submap
    ctl = Mock(wraps=di.hypr.hyprctl)
    monkeypatch.setattr(di.hypr, "hyprctl", ctl)
    disarm = Mock(return_value=True)
    monkeypatch.setattr(di, "disarm_lock_timer", disarm)
    assert di.reset_lock()
    assert boundary.state["submap"] == ("default" if submap == "typinglock" else submap)
    assert any(c.args[0] == "dispatch" for c in ctl.call_args_list) == (submap == "typinglock")
    assert ctl.call_args_list[0].args == ("submap",)
    disarm.assert_called_once_with()


def test_reset_lock_read_failure_keeps_submap_and_timer(monkeypatch):
    monkeypatch.delenv("JV_DRY", raising=False)
    ctl = Mock(side_effect=OSError("unavailable"))
    monkeypatch.setattr(di.hypr, "hyprctl", ctl)
    disarm = Mock(return_value=True)
    monkeypatch.setattr(di, "disarm_lock_timer", disarm)
    assert not di.reset_lock()
    ctl.assert_called_once_with("submap", json_out=True)
    disarm.assert_not_called()


def test_reset_lock_dry_run_never_calls_desktop(monkeypatch):
    leave = Mock(side_effect=AssertionError("dry run must not reach the compositor"))
    monkeypatch.setattr(di, "lock_leave", leave)
    assert di.reset_lock()
    leave.assert_not_called()


@pytest.mark.parametrize("field,value", [("address", "0xdef"), ("class", "zen"), ("pid", 456)])
def test_check_refuses_each_of_address_class_pid_mismatch(boundary, field, value):
    boundary.state["activewindow"][field] = value
    refuses("focus_changed", lambda: di.check(di.Pin(*PIN)))


@pytest.mark.parametrize("locker", ["hyprlock", "swaylock"])
def test_check_refuses_when_hyprlock_or_swaylock_running(boundary, locker):
    def run(argv, **kwargs):
        assert argv == ["pgrep", "-x", "hyprlock|swaylock"]
        assert locker in argv[-1]
        return subprocess.CompletedProcess(argv, 0, "", "")
    boundary.run.side_effect = run
    refuses("locked", lambda: di.check(di.Pin(*PIN)))


@pytest.mark.parametrize("value", [OSError("failed"), None, [], "bad", {"address": "0xabc"},
                                       {"address": "0xabc", "class": "kitty", "pid": True}])
def test_check_treats_hyprctl_failure_as_refusal(boundary, value):
    boundary.state["activewindow"] = value
    refuses("focus_changed", lambda: di.check(di.Pin(*PIN)))


@pytest.mark.parametrize("failure", [OSError("failed"), subprocess.TimeoutExpired("pgrep", 1), 2])
def test_check_lock_read_failure_refuses(boundary, failure):
    if isinstance(failure, Exception):
        boundary.run.side_effect = failure
    else:
        boundary.run.return_value.returncode = failure
    refuses("locked", lambda: di.check(di.Pin(*PIN)))


def layers(*names):
    return {"DP-1": {"levels": {"2": [{"namespace": name} for name in names], "0": []}}}


def test_check_refuses_on_rofi_layer(boundary):
    boundary.state["layers"] = layers("rofi")
    refuses("layer_open", lambda: di.check(di.Pin(*PIN), ignore_layers=IGNORED))


def test_check_ignores_waybar_awww_swaync_layers(boundary):
    boundary.state["layers"] = layers(*IGNORED)
    di.check(di.Pin(*PIN), ignore_layers=IGNORED)


@pytest.mark.parametrize("value", [OSError("failed"), None, [], "bad", {"DP-1": {}},
                                       {"DP-1": {"levels": {"2": {}}}}, layers(None)])
def test_check_treats_layer_read_failure_as_refusal(boundary, value):
    boundary.state["layers"] = value
    refuses("layer_open", lambda: di.check(di.Pin(*PIN)))


def test_check_empty_workspace_is_destination_closed(boundary):
    boundary.state["activewindow"] = {}
    refuses("destination_closed", lambda: di.check(di.Pin(*PIN)))


def test_sanitize_nfc_and_keeps_emoji_rupee_leading_dash():
    assert di.sanitize("- cafe\u0301 ₹ 👩🏽‍💻 ‘hello’—") == "- café ₹ 👩🏽‍💻 ‘hello’—"


@pytest.mark.parametrize("char", [chr(n) for n in (*range(32), *range(127, 160),
                                                 *range(0x202A, 0x202F), *range(0x2066, 0x206A))])
def test_sanitize_refuses_controls_and_bidi_overrides(char):
    refuses("unsafe_text", lambda: di.sanitize("private" + char))


def test_sanitize_refuses_surrogates():
    refuses("unsafe_text", lambda: di.sanitize("\ud800"))


def test_join_spacing_and_closing_punctuation():
    assert di.join("hello", first=True) == "hello"
    assert di.join("world", first=False) == " world"
    for text in (", next", ".", "!", "?", ":", ";", ")", "]", "}", "”", "’"):
        assert di.join(text, first=False) == text
    assert di.join("", first=False) == ""


@pytest.mark.parametrize("text", ["", " a  b c ", "- café ₹ hello 😀 " * 10, "abc\tdef"])
def test_slices_rejoin_exactly(text):
    parts = di.slices(text, limit=12)
    assert "".join(parts) == text
    assert all(0 < len(part) <= 12 for part in parts)
    assert di.slices("one two three", limit=8) == ["one two ", "three"]


def test_slices_word_longer_than_limit():
    assert di.slices("abcdefghijk", limit=4) == ["abcd", "efgh", "ijk"]


def test_slices_do_not_split_surrogate_or_combining_sequences():
    for cluster in ("e\u0301", "😀", "👩🏽‍💻", "🇮🇳", "\ud83d\ude00"):
        parts = di.slices("abc" + cluster + "def", limit=5)
        assert "".join(parts) == "abc" + cluster + "def"
        assert any(cluster in part for part in parts)
    refuses("unsafe_text", lambda: di.slices("e" + "\u0301" * 8, limit=4))
    with pytest.raises(ValueError):
        di.slices("abc", limit=0)


@pytest.fixture
def process(monkeypatch):
    proc = Mock(returncode=0)
    proc.communicate.return_value = (None, None)
    popen = Mock(return_value=proc)
    monkeypatch.setattr(di.subprocess, "Popen", popen)
    return proc, popen


def test_send_uses_stdin_never_argv(process, caplog, capsys):
    proc, popen = process
    secret = "- private café ₹ 😀"
    assert di.send(secret, delay_ms=20)
    args, kwargs = popen.call_args
    assert args[0] == ["wtype", "-d", "20", "-"]
    assert secret not in repr((args, kwargs))
    assert kwargs["stdin"] == subprocess.PIPE
    assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
    assert "shell" not in kwargs
    assert proc.communicate.call_args.kwargs == {"input": secret.encode("utf-8"), "timeout": 5.0}
    assert secret not in caplog.text + repr(capsys.readouterr())


@pytest.mark.parametrize("failure", ["timeout", "nonzero", "crash", "missing", "communicate"])
def test_send_timeout_nonzero_and_crash_return_false(process, failure, caplog, capsys):
    proc, popen = process
    secret = "private secret"
    if failure == "timeout":
        proc.communicate.side_effect = subprocess.TimeoutExpired(["wtype", secret], 1, output=secret)
    elif failure == "nonzero":
        proc.returncode = 1
    elif failure == "crash":
        proc.returncode = -11
    elif failure == "missing":
        popen.side_effect = FileNotFoundError(secret)
    else:
        proc.communicate.side_effect = OSError(secret)
    assert not di.send(secret, 20, timeout_s=0.01)
    if failure in ("timeout", "communicate"):
        proc.kill.assert_called_once_with()
        assert proc.wait.call_count == 1
        assert 0 <= proc.wait.call_args.kwargs["timeout"] <= 0.25
    assert secret not in caplog.text + repr(capsys.readouterr())


def test_send_refuses_unsafe_text_without_launch(process):
    assert not di.send("secret\n", 20)
    assert not di.send("secret", -1)
    process[1].assert_not_called()


def test_send_cleanup_error_cannot_expose_text(process, caplog, capsys):
    proc, _ = process
    proc.communicate.side_effect = subprocess.TimeoutExpired("wtype", 1)
    proc.kill.side_effect = ProcessLookupError("private text")
    proc.wait.side_effect = OSError("private text")
    refuses("key_release_failed", lambda: di.send("private text", 20))
    assert "private text" not in caplog.text + repr(capsys.readouterr())


def test_send_timeout_reaps_real_harmless_child(monkeypatch):
    children = []

    def popen(argv, **kwargs):
        assert argv == ["wtype", "-d", "20", "-"]
        child = REAL_POPEN([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(di.subprocess, "Popen", popen)
    try:
        assert di.send("private", 20, timeout_s=0.05) is False
        assert children[0].poll() == -9
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait()


@pytest.fixture
def watch(monkeypatch, tmp_path):
    left, right = socket.socketpair()
    connected = []

    class Connection:
        def connect(self, path):
            connected.append(path)
        def __getattr__(self, name):
            return getattr(left, name)

    monkeypatch.setattr(di.socket, "socket", lambda *args: Connection())
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "stale-instance")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    lost, fired = [], threading.Event()

    def on_lost(reason):
        lost.append((reason, threading.current_thread()))
        fired.set()

    watcher = di.FocusWatch(di.Pin(*PIN), on_lost, ignore_layers=IGNORED)
    watcher.start()
    yield SimpleNamespace(watcher=watcher, peer=right, lost=lost, fired=fired,
                          connected=connected, root=tmp_path)
    watcher.stop()
    left.close()
    right.close()


def finish_event(watch, data, reason):
    watch.peer.sendall(data)
    assert watch.fired.wait(2), "watch did not report loss"
    watch.watcher.stop()
    assert [r for r, _ in watch.lost] == [reason]
    assert watch.lost[0][1] is not threading.current_thread()


def test_focus_watch_flick_a_b_a_fires_once(watch):
    finish_event(watch, b"activewindowv2>>def\nactivewindowv2>>abc\n", "focus_changed")


def test_focus_watch_ignored_layer_is_silent(watch):
    watch.peer.sendall(b"openlayer>>waybar\nopenlayer>>awww-daemon\nopenlayer>>swaync-notification-window\n")
    # A final close proves preceding ignored events were consumed without firing.
    finish_event(watch, b"closewindow>>abc\n", "destination_closed")


def test_focus_watch_other_layer_fires(watch):
    finish_event(watch, b"openlayer>>rofi\n", "layer_opened")


def test_focus_watch_socket_eof_fires_focus_watch_lost(watch):
    watch.peer.shutdown(socket.SHUT_WR)
    assert watch.fired.wait(2)
    assert [r for r, _ in watch.lost] == ["focus_watch_lost"]


def test_focus_watch_stop_is_silent(watch):
    watch.watcher.stop()
    watch.watcher.stop()
    assert not watch.lost


def test_focus_watch_uses_resolved_instance_despite_stale_environment(watch):
    assert watch.connected == [str(watch.root / "hypr/fresh-instance/.socket2.sock")]


def test_focus_watch_split_line_and_unknown_event_are_silent(watch):
    watch.peer.sendall(b"unknown>>payload\nactivewindowv2>>a")
    assert not watch.fired.wait(0.05)
    watch.peer.sendall(b"bc\nclosewindow>>def\n")
    assert not watch.fired.wait(0.05)
    finish_event(watch, b"activewindowv2>>def\n", "focus_changed")


def test_focus_watch_focus_change_and_eof_race_fires_at_most_once(watch):
    watch.peer.sendall(b"activewindowv2>>def\n")
    watch.peer.shutdown(socket.SHUT_WR)
    assert watch.fired.wait(2)
    watch.watcher.stop()
    assert [r for r, _ in watch.lost] == ["focus_changed"]


def test_focus_watch_connect_failure_reports_loss(monkeypatch):
    monkeypatch.setattr(di.socket, "socket", Mock(side_effect=OSError("unavailable")))
    lost = []
    watcher = di.FocusWatch(di.Pin(*PIN), lost.append, ())
    watcher.start()
    watcher.stop()
    assert lost == ["focus_watch_lost"]


@pytest.mark.parametrize("fn", ["lock_enter", "lock_leave"])
def test_lock_success_is_confirmed(boundary, fn):
    assert getattr(di, fn)()
    assert boundary.state["submap"] == ("typinglock" if fn == "lock_enter" else "default")


@pytest.mark.parametrize("submap,held", [("typinglock", True), ("default", False), (None, False),
                                       ({"submap": "typinglock"}, False), ("typinglock\n", False),
                                       (OSError("private text"), False)])
def test_lock_held_only_confirms_exact_submap_without_dispatch(monkeypatch, submap, held):
    ctl = Mock(side_effect=submap) if isinstance(submap, Exception) else Mock(return_value=submap)
    monkeypatch.setattr(di.hypr, "hyprctl", ctl)
    assert di.lock_held() is held
    ctl.assert_called_once_with("submap", json_out=True)


@pytest.mark.parametrize("result", [OSError("failed"), "error", "ok"])
def test_lock_enter_failure_returns_false(monkeypatch, result):
    ctl = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
    monkeypatch.setattr(di.hypr, "hyprctl", ctl)
    assert di.lock_enter() is False


@pytest.mark.parametrize("result", [OSError("failed"), "error", "ok"])
def test_lock_leave_failure_returns_false(monkeypatch, result):
    ctl = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
    monkeypatch.setattr(di.hypr, "hyprctl", ctl)
    assert di.lock_leave() is False


@pytest.fixture
def timer(boundary):
    units, calls = set(), []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "systemctl":
            unit = argv[-1]
            if "show" in argv:
                output = "loaded\n" if unit in units else "not-found\n"
            else:
                assert "stop" in argv
                units.discard(unit)
                output = ""
        else:
            assert argv[0] == "systemd-run"
            unit = next(a.split("=", 1)[1] for a in argv if a.startswith("--unit="))
            assert not units, "previous timer/service was not stopped before rearm"
            units.update((unit + ".timer", unit + ".service"))
            output = ""
        return subprocess.CompletedProcess(argv, 0, output, "")
    boundary.run.side_effect = run
    return SimpleNamespace(units=units, calls=calls)


def test_lock_timer_rearm_replaces_previous_timer(timer):
    assert di.arm_lock_timer(10)
    assert di.arm_lock_timer(20)
    launches = [a for a in timer.calls if a[0] == "systemd-run"]
    assert len(launches) == 2
    assert [a for a in launches[0] if a.startswith("--unit=")] == [
        a for a in launches[1] if a.startswith("--unit=")]
    assert "--on-active=10s" in launches[0] and "--on-active=20s" in launches[1]
    for argv in launches:
        assert "--user" in argv and "--collect" in argv and "--scope" not in argv
        assert "--slice-inherit" not in argv
        assert "--expand-environment=no" in argv
        command = argv[argv.index("--") + 1:]
        assert command[:2] == ["/bin/sh", "-c"]
        assert command[-2:] == ["/usr/bin/hyprctl", "fresh-instance"]
    assert di.disarm_lock_timer()
    assert not timer.units


@pytest.mark.parametrize("submap", ["resize", "default", "typinglock", "read-failure"])
def test_lock_timer_firing_only_leaves_owned_submap(timer, tmp_path, monkeypatch, submap):
    # Run only the captured timer payload, against a harmless fake executable.
    fake = tmp_path / "compositor"
    dispatched = tmp_path / "dispatched"
    reads = tmp_path / "reads"
    fake.write_text('''#!/bin/sh
if [ "$3" = "-j" ] && [ "$4" = "submap" ]; then
    printf 'read\\n' >> "$FAKE_READS"
    [ "$FAKE_SUBMAP" != "read-failure" ] || exit 1
    printf '"%s"\\n' "$FAKE_SUBMAP"
elif [ "$3" = "dispatch" ] && [ "$4" = 'hl.dsp.submap("")' ]; then
    printf 'reset\\n' >> "$FAKE_DISPATCHED"
else
    exit 2
fi
''')
    fake.chmod(0o755)
    monkeypatch.setenv("FAKE_SUBMAP", submap)
    monkeypatch.setenv("FAKE_DISPATCHED", str(dispatched))
    monkeypatch.setenv("FAKE_READS", str(reads))
    monkeypatch.setattr(di.shutil, "which", lambda _: str(fake))
    assert di.arm_lock_timer(10)
    argv = next(a for a in timer.calls if a[0] == "systemd-run")
    command = argv[argv.index("--") + 1:]

    def launch(argv, **kwargs):
        assert argv == command and str(fake) in argv
        assert argv[0] in (str(fake), "/bin/sh")
        return REAL_POPEN(argv, **kwargs)

    monkeypatch.setattr(di.subprocess, "Popen", launch)
    REAL_RUN(command, capture_output=True, text=True, timeout=2)
    assert reads.read_text() == "read\n"
    assert dispatched.exists() == (submap == "typinglock")


def test_lock_timer_failure_returns_false(boundary):
    assert di.arm_lock_timer(10) is False
    assert di.disarm_lock_timer() is False
    boundary.run.side_effect = OSError("failed")
    assert di.arm_lock_timer(10) is False
    assert di.disarm_lock_timer() is False


@pytest.mark.parametrize("command", ["stop", "systemd-run"])
def test_lock_timer_stop_and_arm_failure_refuse(boundary, timer, command):
    original = boundary.run.side_effect
    if command == "stop":
        assert di.arm_lock_timer(10)

    def run(argv, **kwargs):
        if command in argv:
            return subprocess.CompletedProcess(argv, 1, "", "failed")
        return original(argv, **kwargs)

    boundary.run.side_effect = run
    assert di.arm_lock_timer(20) is False
    if command == "stop":
        assert len([a for a in timer.calls if a[0] == "systemd-run"]) == 1


@pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan")])
def test_lock_timer_invalid_delay_never_runs(boundary, seconds):
    assert di.arm_lock_timer(seconds) is False
    boundary.run.assert_not_called()


@pytest.mark.parametrize("outcome", ["timeout", "crash", "success"])
def test_send_recovers_only_abnormal_completion_after_reap(monkeypatch, process, outcome):
    proc, _ = process
    calls = []
    proc.wait.side_effect = lambda **_: calls.append("reaped")
    recover = Mock(side_effect=lambda: calls.append("recovered"))
    monkeypatch.setattr(di, "release_keys", recover)
    if outcome == "timeout":
        proc.communicate.side_effect = subprocess.TimeoutExpired("wtype", 1)
    elif outcome == "crash":
        proc.returncode = -9
    assert di.send("abc", 20) == (outcome == "success")
    assert calls == {"timeout": ["reaped", "recovered"], "crash": ["recovered"], "success": []}[outcome]


@pytest.mark.parametrize("failure", [None, "missing", "timeout", "nonzero"])
def test_release_keys_bounded_and_release_only(monkeypatch, failure, capsys):
    proc = Mock(returncode=0)
    popen = Mock(return_value=proc)
    monkeypatch.setattr(di.subprocess, "Popen", popen)
    if failure == "missing":
        popen.side_effect = FileNotFoundError("private detail")
    elif failure == "timeout":
        proc.communicate.side_effect = subprocess.TimeoutExpired("private detail", 1)
    elif failure == "nonzero":
        proc.returncode = 1
    if failure:
        refuses("key_release_failed", REAL_RELEASE)
        assert "dictation_key_release_failed" in capsys.readouterr().out
    else:
        REAL_RELEASE()  # No held key is needed; this is idempotent.
    argv = popen.call_args.args[0]
    assert argv == ["wtype", *(v for c in di._RELEASE_SYMBOLS for v in ("-p", c))]
    assert len({symbol.casefold() for symbol in argv[2::2]}) == 48
    if failure != "missing":
        proc.communicate.assert_called_once_with(timeout=1.0)
    if failure == "timeout":
        proc.kill.assert_called_once_with()
        assert 0 <= proc.wait.call_args.kwargs["timeout"] <= 0.25
    assert "private detail" not in capsys.readouterr().out


def test_two_release_recoveries_are_independent(monkeypatch):
    barrier = threading.Barrier(2)
    processes = []
    def popen(*args, **kwargs):
        proc = Mock(returncode=0)
        proc.communicate.side_effect = lambda **_: barrier.wait(timeout=2)
        processes.append(proc)
        return proc
    monkeypatch.setattr(di.subprocess, "Popen", popen)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(REAL_RELEASE) for _ in range(2)]
        for future in futures:
            future.result(timeout=3)
    assert len(processes) == 2


def test_reset_recovers_before_leaving_lock(monkeypatch, boundary):
    monkeypatch.delenv("JV_DRY", raising=False)
    calls = []
    boundary.state["submap"] = "typinglock"
    monkeypatch.setattr(di, "release_keys", lambda: calls.append("keys"))
    monkeypatch.setattr(di, "lock_leave", lambda: calls.append("leave") or True)
    monkeypatch.setattr(di, "disarm_lock_timer", lambda: calls.append("disarm") or True)
    assert di.reset_lock()
    assert calls == ["keys", "leave", "disarm"]
    def failed():
        raise di.InputRefused("key_release_failed")
    monkeypatch.setattr(di, "release_keys", failed)
    calls.clear()
    assert not di.reset_lock()
    assert calls == ["leave", "disarm"]


def test_reset_failed_release_outside_lock_still_disarms(monkeypatch, boundary):
    monkeypatch.delenv("JV_DRY", raising=False)
    boundary.state["submap"] = "default"
    monkeypatch.setattr(di, "release_keys", Mock(side_effect=di.InputRefused("key_release_failed")))
    leave = Mock()
    disarm = Mock(return_value=True)
    monkeypatch.setattr(di, "lock_leave", leave)
    monkeypatch.setattr(di, "disarm_lock_timer", disarm)
    assert not di.reset_lock()
    leave.assert_not_called()
    disarm.assert_called_once_with()
