"""Local dashboard: GET observations and one guarded note POST, on its own thread (never the voice loop)."""
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import sources

PANELS = {"live": sources.live, "sessions": sources.sessions, "brain": sources.brain, "service": sources.service,
          "turns": sources.turns, "reminders": sources.reminders}
log = logging.getLogger("heardbackd")


def state() -> dict:
    """Every panel in its own try: a broken source is that panel's `error`, never a 500."""
    out: dict = {"now": time.time()}
    for name, fn in PANELS.items():
        try:
            out[name] = fn()
        except Exception as e:
            out[name] = {"error": f"{type(e).__name__}: {e}"[:300]}
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "heardback-dash"

    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        return self.headers.get_all("Host") in ([f"127.0.0.1:{port}"], [f"localhost:{port}"])

    def _send(self, code: int, body: bytes, ctype: str = "text/plain; charset=utf-8", **headers) -> None:
        self.send_response(code)
        for k, v in {"Content-Type": ctype, "Content-Length": str(len(body)), "Cache-Control": "no-store",
                     "X-Content-Type-Options": "nosniff", **headers}.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:
        # DNS rebinding: a web page that re-points its own name at 127.0.0.1 still sends its own Host.
        if not self._host_ok():
            return self._send(403, b"bad host\n")
        path = self.path.partition("?")[0]
        if path == "/":
            return self._send(200, (Path(__file__).with_name("page.html")).read_bytes(), "text/html; charset=utf-8")
        if path == "/api/state":
            return self._send(200, json.dumps(state(), default=str).encode(), "application/json")
        self._send(404, b"not found\n")

    def _json_body(self):
        """The shared POST guard: Host, Origin, Content-Type, one Content-Length, 8 KB, JSON dict. None = already answered."""
        if not self._host_ok():
            return self._send(403, b"bad host\n")
        port = self.server.server_address[1]
        origins = ([f"http://127.0.0.1:{port}"], [f"http://localhost:{port}"])
        if self.headers.get_all("Origin") not in origins:
            return self._send(403, b"bad or missing origin\n")
        if self.headers.get_all("Content-Type") != ["application/json"]:
            return self._send(415, b"application/json required\n")
        lengths = self.headers.get_all("Content-Length") or []
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
            return self._send(400, b"one content length required\n")
        try:
            size = int(lengths[0])
        except ValueError:
            return self._send(400, b"invalid content length\n")
        if size > 8192:
            return self._send(413, b"body exceeds 8 KB\n")
        try:
            self.connection.settimeout(5)
            body = self.rfile.read(size)
            if len(body) != size:
                return self._send(400, b"incomplete body\n")
            data = json.loads(body.decode("utf-8"))
        except (ValueError, OSError, RecursionError):
            return self._send(400, b"invalid JSON body\n")
        if not isinstance(data, dict):
            return self._send(400, b"JSON object required\n")
        return data

    def do_POST(self) -> None:
        path = self.path.partition("?")[0]
        if path not in ("/api/note", "/api/reminder"):
            return self._405()
        data = self._json_body()
        if data is None:
            return
        if path == "/api/reminder":
            code, why = sources.change_reminder(data)
            return self._send(code, why.encode("utf-8", "replace") + (b"\n" if why else b""))
        if not isinstance(data.get("text"), str):
            return self._send(400, b"text required\n")
        text, turn = data["text"].strip(), data.get("turn")
        if not 1 <= len(text) <= 2000:
            return self._send(400, b"text must be 1 to 2000 characters\n")
        if turn is not None and (not isinstance(turn, str) or not sources.TURN_ID.fullmatch(turn)):
            return self._send(400, b"bad turn id\n")
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            return self._send(400, b"invalid Unicode text\n")
        try:
            sources.append_note(turn, text)
        except OSError:
            log.exception("dash: note could not be saved")
            return self._send(500, b"note could not be saved; try again\n")
        self._send(204, b"")

    def _405(self) -> None:
        self._send(405, b"GET only; notes use POST /api/note, reminders POST /api/reminder\n",
                    Allow="GET, POST" if self.path in ("/api/note", "/api/reminder") else "GET")

    do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _405

    def log_message(self, *a) -> None:   # no access log in the journal
        pass


def start(port: int) -> ThreadingHTTPServer:
    """Serve on 127.0.0.1:`port` (0 = any free port, for tests) from a daemon thread; `.shutdown()` stops it."""
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="heardback-dash", daemon=True).start()
    log.info("dash: http://127.0.0.1:%d/", srv.server_address[1])
    return srv
