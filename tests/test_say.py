import json
import socket
import threading
import time
from pathlib import Path

import pytest

from jv.cli import _parser, all_verbs, catalogue
from jv.verb import Verb


def start_line_server(path: Path) -> list[str]:
    got: list[str] = []
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        with conn, srv:
            got.extend(conn.makefile().read().splitlines())
    threading.Thread(target=serve, daemon=True).start()
    return got


def test_say_sends_line(runtime_dir, run_jv):
    got = start_line_server(Path(runtime_dir) / "heardback.sock")
    assert run_jv("say", "hello", "there")[0] == 0
    time.sleep(0.1)
    assert json.loads(got[0]) == {"type": "say", "text": "hello there", "final": True}


def test_say_no_daemon(runtime_dir, run_jv):
    code, _, err = run_jv("say", "x")
    assert code == 1 and "not running" in err


def test_say_refused(runtime_dir, run_jv):
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(Path(runtime_dir) / "heardback.sock"))   # bound, never listening -> refused
    try:
        code, _, err = run_jv("say", "x")
    finally:
        s.close()
    assert code == 1 and "not running" in err


def test_catalogue_say_entry():
    e = next(v for v in catalogue(all_verbs())["verbs"] if v["name"] == "say")
    assert e["domain"] == "say" and e["verb"] == "say" and e["argv"] == ["text"] and not e["voice"]


def _v(name):
    return Verb(name=name, fn=lambda a: {}, description="d",
                params={"type": "object", "properties": {}}, argv=[])


@pytest.mark.parametrize("names", [["say", "say.go"], ["say.go", "say"], ["list"]])
def test_top_level_collision(names):
    with pytest.raises(ValueError):
        _parser([_v(n) for n in names])
