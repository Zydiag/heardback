"""Single-use local playback speech; only an opaque receipt goes to Claude."""
import json
import os
from pathlib import Path
import re
import uuid

from jv.verb import runtime_path


def save(say: str, turn: str, attempt: str) -> str:
    # shortcut: interrupted results can leave receipts until logout; add cleanup if they accumulate.
    receipt = uuid.uuid4().hex
    path = runtime_path("heardback-play-" + receipt)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"say": say, "turn": turn, "attempt": attempt}, stream)
    return receipt


def take(receipt, turn: str, attempt: str) -> str | None:
    if not isinstance(receipt, str) or not re.fullmatch(r"[0-9a-f]{32}", receipt):
        return None
    path = Path(runtime_path("heardback-play-" + receipt))
    try:
        data = json.loads(path.read_text())
        path.unlink()
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and (data.get("turn"), data.get("attempt")) == (turn, attempt) \
            and isinstance(data.get("say"), str):
        return data["say"]
    return None
