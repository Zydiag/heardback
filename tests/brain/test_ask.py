import asyncio
import json
import socket
import subprocess
import sys
import threading

import pytest

from brainutil import jv_calls, script, wait_socket
from heardback.brain import brain
from jv import cli

ASK = "import sys; from jv.cli import main; main()"


def run_ask(env, *args, input=None, timeout=10):
    return subprocess.run([sys.executable, "-c", ASK, "ask", *args], capture_output=True, text=True,
                          input=input, timeout=timeout)


@pytest.fixture
def brain_server(env):
    loop = asyncio.new_event_loop()
    path = brain.sock_path()
    task = loop.create_task(brain.Brain().serve(path))

    def run():
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
        loop.close()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    asyncio.run(wait_socket(path))
    yield path
    loop.call_soon_threadsafe(task.cancel)
    t.join(5)


@pytest.fixture
def fake_server(env):
    """One-connection scripted server: `handler(rfile, wfile)` runs in a thread."""
    srv = socket.socket(socket.AF_UNIX)
    path = env / "f.sock" if len(str(env / "f.sock")) < 100 else brain.sock_path()
    srv.bind(str(path))
    srv.listen(1)
    got = []

    def start(handler):
        def go():
            c, _ = srv.accept()
            with c, c.makefile("rw") as f:
                handler(f, got)
        threading.Thread(target=go, daemon=True).start()
        return str(path)
    yield start, got
    srv.close()


def test_ask_prints_say(env, brain_server):
    script(env, {"wall current": {"rc": 0, "stdout": '{"ok":true,"say":"Current wallpaper."}'}})
    r = run_ask(env, "what's my wallpaper")
    assert r.returncode == 0 and r.stdout.strip() == "Current wallpaper."


def test_ask_refuses_actions(env, brain_server):
    r = run_ask(env, "next wallpaper")
    assert r.returncode == 0 and "authenticated voice" in r.stdout
    assert jv_calls(env) == [["list", "--json"]]


def test_ask_without_daemon(env):
    r = run_ask(env, "hi")
    assert r.returncode == 1 and "heardbackd not running" in r.stderr


def test_ask_answers_confirm_from_stdin(fake_server):
    start, got = fake_server

    def handler(f, got):
        got.append(json.loads(f.readline()))
        f.write('{"type":"confirm","text":"Close 7 tabs?","timeout_s":8}\n')
        f.flush()
        got.append(json.loads(f.readline()))
        f.write('{"type":"say","text":"Closed.","final":true}\n{"type":"done"}\n')
        f.flush()
    path = start(handler)
    r = run_ask(None, "close unused tabs", "--sock", path, input="yes\n")
    assert r.returncode == 0 and r.stdout == "Close 7 tabs? [yes/no] Closed.\n"
    assert got[0]["type"] == "utterance" and got[0]["text"] == "close unused tabs" and got[0]["speaker_ok"] is True
    assert got[1]["type"] == "utterance" and got[1]["text"] == "yes"


def test_ask_times_out(fake_server):
    start, _ = fake_server
    path = start(lambda f, got: f.readline() and f.readline())   # never replies
    r = run_ask(None, "hi", "--sock", path, "--timeout", "0.3")
    assert r.returncode == 1 and "timed out" in r.stderr


def test_ask_not_in_catalogue(capsys):
    cli.run(["list", "--json"])
    assert all(v["domain"] != "ask" for v in json.loads(capsys.readouterr().out)["verbs"])
