"""`jv ask TEXT`: send a typed utterance to heardbackd and print its replies (not a catalogue verb)."""
import argparse
import json
import socket
import sys
import time

from jv.verb import runtime_path


def _send(f, text: str) -> None:
    # The brain treats typed input as untrusted, regardless of this legacy flag.
    f.write(json.dumps({"type": "utterance", "text": text, "speaker_ok": True, "t": time.time()}) + "\n")
    f.flush()


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="jv ask", description="Send a typed utterance to heardbackd.")
    p.add_argument("text", nargs="+")
    p.add_argument("--sock", default=None)
    p.add_argument("--timeout", type=float, default=120)
    try:
        ns = p.parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    deadline = time.monotonic() + ns.timeout
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(ns.timeout)
    try:
        s.connect(ns.sock or runtime_path("heardback.sock"))
    except OSError:
        print("heardbackd not running", file=sys.stderr)
        return 1
    with s, s.makefile("rw") as f:
        try:
            _send(f, " ".join(ns.text))
            while True:
                s.settimeout(max(0.001, deadline - time.monotonic()))
                line = f.readline()
                if not line:
                    print("heardbackd closed the connection", file=sys.stderr)
                    return 1
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                kind = msg.get("type")
                if kind == "say":
                    print(msg.get("text", ""), flush=True)
                elif kind == "confirm":
                    print(f"{msg.get('text', '')} [yes/no] ", end="", flush=True)
                    _send(f, sys.stdin.readline().strip())
                elif kind == "done":
                    return 0
        except TimeoutError:
            print("timed out", file=sys.stderr)
            return 1
        except OSError as e:
            print(f"heardbackd: {e}", file=sys.stderr)
            return 1
