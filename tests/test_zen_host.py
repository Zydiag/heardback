import io
import json
import os
import socket
import stat
import threading
import time

import pytest

from jv import zen_host
from jv.zen_host import encode, read_frame


def test_frame_roundtrip():
    b = io.BytesIO(encode({"a": "é"}) + encode({"b": 1}))
    assert read_frame(b) == {"a": "é"} and read_frame(b) == {"b": 1} and read_frame(b) is None


def _relay(runtime_dir, ext):
    """Run serve() in a thread over two pipes; ext(msg) -> reply dict or None. Returns the socket path."""
    to_host_r, to_host_w = os.pipe()
    to_ext_r, to_ext_w = os.pipe()
    host_in, host_out = os.fdopen(to_host_r, "rb"), os.fdopen(to_ext_w, "wb")
    ext_in, ext_out = os.fdopen(to_ext_r, "rb"), os.fdopen(to_host_w, "wb")

    def fake_ext():
        while (m := read_frame(ext_in)) is not None:
            if (r := ext(m)) is not None:
                ext_out.write(encode(r))
                ext_out.flush()

    path = os.path.join(runtime_dir, "heardback-zen.sock")
    threading.Thread(target=fake_ext, daemon=True).start()
    threading.Thread(target=zen_host.serve, args=(host_in, host_out, path), daemon=True).start()
    for _ in range(200):   # the file exists after bind() but before listen(): wait for a real connect
        try:
            with socket.socket(socket.AF_UNIX) as s:
                s.connect(path)
            return path
        except (FileNotFoundError, ConnectionRefusedError):
            time.sleep(0.01)
    pytest.fail("socket never appeared")


def _ask(path, req):
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(path)
        s.sendall((json.dumps(req) + "\n").encode())
        return json.loads(s.makefile().readline())


def test_sock_path(runtime_dir):
    assert zen_host.sock_path() == f"{runtime_dir}/heardback-zen.sock"


def test_relay_roundtrip(runtime_dir):
    p = _relay(runtime_dir, lambda m: {"id": m["id"], "ok": True, "tabs": 3})
    assert _ask(p, {"op": "ping"}) == {"ok": True, "tabs": 3}


def test_unknown_op_rejected(runtime_dir):
    p = _relay(runtime_dir, lambda m: pytest.fail("must not reach extension"))
    assert _ask(p, {"op": "eval", "args": {}})["error"] == "unknown op"


def test_bad_request(runtime_dir):
    p = _relay(runtime_dir, lambda m: pytest.fail("must not reach extension"))
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(p)
        s.sendall(b"not json\n")
        assert json.loads(s.makefile().readline())["error"] == "bad request"


def test_socket_mode_0600(runtime_dir):
    p = _relay(runtime_dir, lambda m: {"id": m["id"], "ok": True})
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_no_reply_times_out(runtime_dir, monkeypatch):
    monkeypatch.setattr(zen_host, "REPLY_TIMEOUT", 0.2)
    p = _relay(runtime_dir, lambda m: None)
    assert _ask(p, {"op": "ping"})["error"] == "zen did not reply"


def test_slow_ops_wait_longer(runtime_dir, monkeypatch):   # zenplay: click_result waits for a page to load
    monkeypatch.setattr(zen_host, "REPLY_TIMEOUT", 0.05)
    monkeypatch.setattr(zen_host, "SLOW", {"click_result": 2.0})

    def late(m):
        time.sleep(0.3)
        return {"id": m["id"], "ok": True, "url": "https://youtube.com/watch?v=x"}
    p = _relay(runtime_dir, late)
    assert _ask(p, {"op": "click_result", "args": {"n": 1}})["ok"] is True
    assert _ask(p, {"op": "video", "args": {"action": "play"}})["error"] == "zen did not reply"


def test_exits_clean_on_eof(runtime_dir):
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "-c", "from jv.zen_host import main; main()"],
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
    assert r.returncode == 0 and r.stdout == b""
    assert not os.path.exists(f"{runtime_dir}/heardback-zen.sock")


@pytest.mark.parametrize("frame", [b"not json", b"[1, 2]"])
def test_bad_frame_exits_not_deaf(runtime_dir, frame):
    import struct
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "-c", "from jv.zen_host import main; main()"],
                       input=struct.pack("=I", len(frame)) + frame, capture_output=True, timeout=5)
    assert r.returncode == 1 and r.stdout == b"" and b"bad frame" in r.stderr
    assert not os.path.exists(f"{runtime_dir}/heardback-zen.sock")
