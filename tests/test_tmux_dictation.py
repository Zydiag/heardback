import time

import pytest

from jv import tmux as tmux_mod
from jv.verb import JvError


def wait_for(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def new_cat(tmux_server, name="dictation-target", *, detached=True):
    args = ["new-window", "-t", "0:", "-n", name]
    if detached:
        args.append("-d")
    args.append("cat")
    tmux_server(*args)
    pane = tmux_server("list-panes", "-s", "-t", "0", "-F", "#{pane_id}\t#{window_name}\t#{pane_current_command}")
    return next(p.split("\t")[0] for p in pane.splitlines() if p.split("\t")[1] == name)


def new_raw_receiver(tmux_server, tmp_path, name, output, ready, done):
    script = tmp_path / f"{name}.py"
    script.write_text(f'''#!/usr/bin/env python3
import os, select, termios, time
fd = 0
attrs = termios.tcgetattr(fd)
attrs[3] &= ~(termios.ICANON | termios.ECHO)
attrs[6][termios.VMIN] = 1
attrs[6][termios.VTIME] = 0
termios.tcsetattr(fd, termios.TCSANOW, attrs)
open({str(ready)!r}, "w").write("ready")
data = bytearray()
first_byte_deadline = time.monotonic() + 3.0
while not data and time.monotonic() < first_byte_deadline:
    readable, _, _ = select.select([fd], [], [], max(0, first_byte_deadline - time.monotonic()))
    if not readable:
        break
    chunk = os.read(fd, 4096)
    if not chunk:
        break
    data.extend(chunk)
last_byte = time.monotonic()
while data and time.monotonic() - last_byte < 0.2:
    readable, _, _ = select.select([fd], [], [], max(0, 0.2 - (time.monotonic() - last_byte)))
    if not readable:
        break
    chunk = os.read(fd, 4096)
    if not chunk:
        break
    data.extend(chunk)
    last_byte = time.monotonic()
with open({str(output)!r}, "wb") as f:
    f.write(data)
while not os.path.exists({str(done)!r}):
    time.sleep(0.02)
''')
    script.chmod(0o700)
    tmux_server("new-window", "-d", "-t", "0:", "-n", name, str(script))
    lines = tmux_server("list-panes", "-s", "-t", "0", "-F", "#{pane_id}\t#{window_name}").splitlines()
    return next(line.split("\t")[0] for line in lines if line.split("\t")[1] == name)


def active_selection(tmux_server):
    windows = tmux_server("list-windows", "-t", "0", "-F", "#{window_index}\t#{window_active}").splitlines()
    active = [line.split("\t")[0] for line in windows if line.endswith("\t1")]
    assert len(active) == 1
    panes = tmux_server("list-panes", "-s", "-t", "0",
                        "-F", "#{pane_id}\t#{window_index}\t#{pane_active}\t#{window_active}").splitlines()
    selected = [line.split("\t")[0] for line in panes
                if line.split("\t")[1:] == [active[0], "1", "1"]]
    assert len(selected) == 1
    return active[0], selected[0]


def named_buffers(tmux_server):
    return tmux_server("list-buffers", "-F", "#{buffer_name}").splitlines()


def test_literal_background_paste_does_not_focus_or_press_enter(tmux_server, tmp_path):
    captured = tmp_path / "received.bin"
    ready = tmp_path / "receiver.ready"
    done = tmp_path / "receiver.done"
    pane_id = new_raw_receiver(tmux_server, tmp_path, "dictation-target", captured, ready, done)
    tmux_server("select-window", "-t", "0:0")
    assert wait_for(ready.exists)
    active_before = active_selection(tmux_server)
    try:
        pin = tmux_mod.dictation_target("dictation-target")
        assert pin["pane_id"] == pane_id and pin["pane_pid"].isdigit()
        assert tmux_mod._dictation_server() == tuple(pin["server"])

        result = tmux_mod.dictation_insert(pin, "literal words ; $x", confirmed=False, claim=lambda: True)
        assert result["status"] == "accepted" and result["observed"] is False
        assert wait_for(captured.exists)
        assert captured.read_bytes() == b"literal words ; $x"
        assert b"\n" not in captured.read_bytes()  # proves Enter/newline was not sent to the receiver
        assert active_selection(tmux_server) == active_before
        assert pane_id in tmux_server("list-panes", "-s", "-t", "0", "-F", "#{pane_id}").splitlines()
    finally:
        done.touch()
    assert not any(name.startswith("jvdict-") for name in named_buffers(tmux_server))


def test_alternate_screen_requires_and_accepts_explicit_confirmation(tmux_server, tmp_path):
    script = tmp_path / "alternate-cat"
    script.write_text("#!/bin/sh\nprintf '\\033[?1049h'\nexec cat\n")
    script.chmod(0o700)
    tmux_server("new-window", "-d", "-t", "0:", "-n", "alternate", str(script))
    pane_id = tmux_server("list-panes", "-s", "-t", "0", "-F",
                          "#{pane_id}\t#{window_name}").splitlines()
    pane_id = next(line.split("\t")[0] for line in pane_id if line.split("\t")[1] == "alternate")
    assert wait_for(lambda: tmux_server("display-message", "-p", "-t", pane_id, "#{alternate_on}").strip() == "1")
    pin = tmux_mod.dictation_target("alternate")
    assert pin["requires_commit"] is True
    claimed = []
    result = tmux_mod.dictation_insert(pin, "confirm this", confirmed=False, claim=lambda: claimed.append(True) or True)
    assert result["status"] == "confirmation_required" and claimed == []
    result = tmux_mod.dictation_insert(pin, "confirm this", confirmed=True, claim=lambda: claimed.append(True) or True)
    assert result["status"] == "accepted" and claimed == [True]
    assert wait_for(lambda: "confirm this" in tmux_server("capture-pane", "-p", "-t", pane_id))
    assert tmux_server("display-message", "-p", "-t", pane_id, "#{pane_current_command}").strip() == "cat"


@pytest.mark.parametrize(
    "name,command,expected",
    [
        ("prompt", "printf 'Do you want to proceed?\\n❯ 1. Yes\\n  2. No\\nEsc to cancel\\n'; sleep 30",
         "pane is showing a prompt"),
        ("trust", "printf 'Yes, I trust this folder\\n'; sleep 30",
         "asking whether to trust this folder"),
    ],
)
def test_prompt_and_folder_trust_guards_are_preserved(tmux_server, name, command, expected):
    tmux_server("new-window", "-d", "-t", "0:", "-n", name, command)
    assert wait_for(lambda: name in tmux_server("list-windows", "-F", "#{window_name}"))
    with pytest.raises(JvError, match=expected):
        tmux_mod.dictation_target(name)


def test_pinned_target_loss_or_replacement_never_redirects_paste(tmux_server):
    old_id = new_cat(tmux_server, "replace-me")
    pin = tmux_mod.dictation_target("replace-me")
    assert pin["pane_id"] == old_id
    tmux_server("kill-window", "-t", "0:replace-me")
    new_id = new_cat(tmux_server, "replace-me")
    assert new_id != old_id
    with pytest.raises(JvError, match="pane disappeared"):
        tmux_mod.dictation_insert(pin, "must stay here", confirmed=False, claim=lambda: True)
    assert "must stay here" not in tmux_server("capture-pane", "-p", "-t", new_id)
    assert not any(name.startswith("jvdict-") for name in named_buffers(tmux_server))


def test_failed_staging_does_not_consume_claim_or_send_and_cleans_buffer(tmux_server, monkeypatch):
    pane_id = new_cat(tmux_server, "staging")
    pin = tmux_mod.dictation_target("staging")
    real_tmux = tmux_mod.tmux
    seen = []

    def fail_stage(*args, input=None):
        seen.append(args)
        if args[0] == "load-buffer":
            raise JvError(4, "staging failed")
        return real_tmux(*args, input=input)

    monkeypatch.setattr(tmux_mod, "tmux", fail_stage)
    claimed = []
    with pytest.raises(JvError, match="could not stage"):
        tmux_mod.dictation_insert(pin, "kept preview", confirmed=False, claim=lambda: claimed.append(True) or True)
    assert claimed == [] and not any(args[0] == "paste-buffer" for args in seen)
    assert not any(name.startswith("jvdict-") for name in named_buffers(tmux_server))
    assert "kept preview" not in tmux_server("capture-pane", "-p", "-t", pane_id)


def test_ambiguous_paste_error_is_uncertain_once_and_buffer_is_cleaned(tmux_server, monkeypatch):
    pane_id = new_cat(tmux_server, "uncertain")
    pin = tmux_mod.dictation_target("uncertain")
    real_tmux = tmux_mod.tmux
    pasted = []

    def accept_then_raise(*args, input=None):
        if args[0] == "paste-buffer":
            pasted.append(args)
            real_tmux(*args, input=input)
            raise JvError(1, "simulated lost response")
        return real_tmux(*args, input=input)

    monkeypatch.setattr(tmux_mod, "tmux", accept_then_raise)
    result = tmux_mod.dictation_insert(pin, "maybe arrived", confirmed=False, claim=lambda: True)
    assert result["status"] == "uncertain" and len(pasted) == 1
    assert wait_for(lambda: "maybe arrived" in tmux_server("capture-pane", "-p", "-t", pane_id))
    assert not any(name.startswith("jvdict-") for name in named_buffers(tmux_server))


def test_cancelled_preparation_cleans_staged_buffer_without_dispatch(tmux_server):
    pane_id = new_cat(tmux_server, "cancel-before-send")
    pin = tmux_mod.dictation_target("cancel-before-send")
    result = tmux_mod.dictation_insert(pin, "not dispatched", confirmed=False, claim=lambda: False)
    assert result["status"] == "cancelled"
    assert "not dispatched" not in tmux_server("capture-pane", "-p", "-t", pane_id)
    assert not any(name.startswith("jvdict-") for name in named_buffers(tmux_server))
