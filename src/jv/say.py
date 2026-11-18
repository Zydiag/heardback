"""`jv say <text>`: hand a line to the heardback daemon, which speaks it."""
import json
import socket

from jv.verb import JvError, Verb, runtime_path


def say(a: dict) -> dict:
    path = runtime_path("heardback.sock")
    line = json.dumps({"type": "say", "text": a["text"], "final": True}) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.connect(path)
            s.sendall(line.encode())
    except OSError:   # missing, refused, timeout (TimeoutError is an OSError)
        raise JvError(1, "voice daemon not running")
    return {"say": a["text"]}


VERBS = [
    Verb("say", say, "Speak a line through the voice daemon",
         {"type": "object", "properties": {"text": {"type": "string", "maxLength": 4000}}, "required": ["text"]},
         ["text"], args={"text": {"type": "text"}}, voice=False),
]
