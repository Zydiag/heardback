"""Single-use authorizations shared by the brain, MCP transport and jv."""
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from jv.verb import JvError, runtime_path


def args_hash(args: dict) -> str:
    # Hash what jv receives through argv: null optionals and false flags dropped, values as text (Claude's 5 and
    # "5" reach jv the same, and jv's validate() turns both into 5).
    args = {k: v if v is True else " ".join(map(str, v)) if isinstance(v, list) else str(v)
            for k, v in args.items() if v is not None and v is not False}
    return hashlib.sha256(json.dumps(args, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _key(verb: str, args: dict, turn: str, attempt: str) -> str:
    return hashlib.sha256(json.dumps([verb, args_hash(args), turn, attempt]).encode()).hexdigest()


def mint(verb: str, args: dict, turn: str, attempt: str) -> str:
    token = _key(verb, args, turn, attempt) + "-" + uuid.uuid4().hex
    path = runtime_path("heardback-grant-" + token)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        json.dump({"verb": verb, "args_hash": args_hash(args), "turn": turn,
                   "expires": time.time() + 30}, f)
    return token


def find(verb: str, args: dict, turn: str, attempt: str) -> str:
    """The transport discovers only grants minted for this call and execution attempt."""
    root = Path(runtime_path("."))
    prefix = "heardback-grant-" + _key(verb, args, turn, attempt) + "-"
    for path in root.glob("heardback-grant-*"):
        try:
            data = json.loads(path.read_text())
            if data["expires"] <= time.time():
                path.unlink()
            elif path.name.startswith(prefix):
                return path.name.removeprefix("heardback-grant-")
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return ""


def consume(token: str, verb: str, args: dict, turn: str) -> None:
    try:
        if not turn or not re.fullmatch(r"[0-9a-f]{64}-[0-9a-f]{32}", token):
            raise ValueError
        path = Path(runtime_path("heardback-grant-" + token))
        with path.open() as f:
            path.unlink()   # only one contender can unlink; consume before any preflight or action
            data = json.load(f)
        if data["verb"] != verb or data["args_hash"] != args_hash(args) or data["turn"] != turn or \
                not isinstance(data["expires"], (int, float)) or not time.time() < data["expires"]:
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise JvError(4, "heardback refused: missing, invalid, expired or already used call grant.") from None
