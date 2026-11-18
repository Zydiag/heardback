import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from jv import user
from jv.verb import runtime_path
from test_user import OK, _bin_on_path, _reload_user_after, actions  # noqa: F401  (fixtures)

BG = OK.replace("confirm = false", 'confirm = false\nbackground = true\nsay = "Looking for {title}."\ndone_say = "Got {out}."')


class FakeSock:
    def __init__(self, path: str):
        self.lines: list[str] = []
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        self.srv.listen(8)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            with conn:
                self.lines += conn.makefile().read().splitlines()

    def wait_say(self, timeout: float = 10) -> str:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.lines:
                return json.loads(self.lines.pop(0))["text"]
            time.sleep(0.05)
        raise AssertionError("no say within timeout")


@pytest.fixture
def fake_sock():
    s = FakeSock(runtime_path("heardback.sock"))
    yield s
    s.srv.close()


def _wait_gone(p: Path, timeout: float = 5) -> bool:
    end = time.monotonic() + timeout
    while p.exists() and time.monotonic() < end:
        time.sleep(0.05)
    return not p.exists()


def test_bg_says_now_then_announces(actions, run_jv_with, fake_sock):
    actions(BG)
    code, out, _ = run_jv_with(user.build()[0], "do", "echo-it", "dune", "--json")
    assert code == 0 and out["background"] is True and out["say"] == "Looking for dune." and out["pid"] > 0
    assert fake_sock.wait_say() == 'Got ["--", "dune"].'
    assert _wait_gone(user.pidfile("echo-it"))


def test_bg_fail_say_on_timeout(actions, run_jv_with, fake_sock):
    actions(BG.replace('"--", "{title}"', '"--sleep", "30"').replace("background = true", "background = true\ntimeout_s = 1")
              .replace('done_say', 'fail_say = "No: {err}."\ndone_say'))
    run_jv_with(user.build()[0], "do", "echo-it", "x")
    assert fake_sock.wait_say() == "No: timed out."


def test_bg_default_fail_say(actions, run_jv_with, fake_sock):
    actions(BG.replace('"--", "{title}"', '"--fail"'))
    run_jv_with(user.build()[0], "do", "echo-it", "x")
    assert fake_sock.wait_say() == "echo-it failed: boom"


def test_second_start_refused(actions, run_jv_with, fake_sock):
    actions(BG.replace('"--", "{title}"', '"--sleep", "3"'))
    v = user.build()[0]
    assert run_jv_with(v, "do", "echo-it", "x")[0] == 0
    code, out, _ = run_jv_with(v, "do", "echo-it", "x", "--json")
    assert code == 4 and out["error"] == "Still on the last one."


def test_stale_pidfile_not_alive(actions):
    p = user.pidfile("echo-it")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("garbage")
    assert not user.alive("echo-it")
    p.write_text("999999999")
    assert not user.alive("echo-it")


def test_entry_gone_before_runner(actions, fake_sock):               # Review Focus 3
    actions(BG)
    p = user.pidfile("nope")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(os.getpid()))
    assert user.bg_main("nope", "{}") == 1
    assert fake_sock.wait_say() == "nope is gone from your actions file."
    assert not p.exists()


def test_daemon_down_only_logged(actions, run_jv_with):
    actions(BG)
    run_jv_with(user.build()[0], "do", "echo-it", "x")
    log = Path(os.environ["XDG_STATE_HOME"]) / "heardback/actions.log"
    end = time.monotonic() + 10
    while time.monotonic() < end:
        lines = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
        if any("say_failed" in x for x in lines) and any(x.get("bg") and x["out"] for x in lines):
            break
        time.sleep(0.1)
    run = next(x for x in lines if x.get("bg"))
    assert run["rc"] == 0 and run["out"] == ['["--", "x"]'] and any(x.get("say_failed") == 'Got ["--", "x"].' for x in lines)
