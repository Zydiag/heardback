"""Zen native-messaging host: relays jv's unix-socket requests to the heardback extension.

stdout is the extension channel: nothing but framed messages may be written to it.
Errors go to stderr (Zen's browser console).
"""
import atexit
import contextlib
import itertools
import json
import os
import queue
import socketserver
import struct
import sys
import threading

from jv.verb import runtime_path

OPS = {"ping", "list", "activate", "create", "search", "remove", "type", "page_text", "click_result", "video",
       "yt_first"}
REPLY_TIMEOUT = 3.0
SLOW = {"click_result": 13.0, "page_text": 5.0, "yt_first": 7.0}   # page render / fetch (add-on: 12 s / 6 s)


def sock_path() -> str:
    return runtime_path("heardback-zen.sock")


def encode(msg: dict) -> bytes:
    b = json.dumps(msg).encode()
    return struct.pack("=I", len(b)) + b   # native byte order, per native messaging


def _read(f, n: int) -> bytes | None:
    b = f.read(n)
    return b if b and len(b) == n else None


def read_frame(f) -> dict | None:
    if (h := _read(f, 4)) is None:
        return None
    b = _read(f, struct.unpack("=I", h)[0])
    return None if b is None else json.loads(b)


def serve(ext_in, ext_out, path: str) -> None:
    pending, lock, ids = {}, threading.Lock(), itertools.count(1)

    def reader():   # extension -> waiting client
        code = 0
        try:   # a bad frame ends the host (Zen restarts it) rather than leaving it deaf
            while (m := read_frame(ext_in)) is not None:
                if q := pending.pop(m.get("id"), None):
                    q.put(m)
        except Exception as e:
            print(f"jarvis_zen: bad frame from extension: {e!r}", file=sys.stderr)   # stdout is the channel
            code = 1
        finally:
            _unlink(path)   # os._exit skips atexit, so unlink first
            os._exit(code)

    class H(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                req = json.loads(self.rfile.readline())
            except ValueError:
                return self._reply({"ok": False, "error": "bad request"})
            if not isinstance(req, dict) or req.get("op") not in OPS:
                return self._reply({"ok": False, "error": "unknown op"})
            i, q = next(ids), queue.Queue()
            pending[i] = q
            with lock:
                ext_out.write(encode({"id": i, "op": req["op"], "args": req.get("args") or {}}))
                ext_out.flush()
            try:
                rep = q.get(timeout=SLOW.get(req["op"], REPLY_TIMEOUT))
            except queue.Empty:
                pending.pop(i, None)
                rep = {"ok": False, "error": "zen did not reply"}
            rep.pop("id", None)
            self._reply(rep)

        def _reply(self, d):
            self.wfile.write((json.dumps(d) + "\n").encode())

    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)   # newest host wins
    old = os.umask(0o177)
    try:
        srv = socketserver.ThreadingUnixStreamServer(path, H)
    finally:
        os.umask(old)
    atexit.register(_unlink, path)
    threading.Thread(target=reader, daemon=True).start()
    srv.serve_forever()


def _unlink(path: str) -> None:
    # ponytail: may unlink a newer host's socket if hosts overlap; compare st_ino if that ever happens
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


def main() -> None:
    serve(sys.stdin.buffer, sys.stdout.buffer, sock_path())
