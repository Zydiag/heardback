"""Last few prompts/replies from a Claude Code transcript (JSONL), for `jv status detail`."""
import json
import os

WRAPPERS = ("<command-", "<local-command-", "<task-notification", "<system-reminder", "<bash-")

DETAIL_PART_MAX = 400
DETAIL_TOTAL_MAX = 2048
TAIL_BYTES = 512 * 1024


def _text(d: dict) -> str:
    m = d.get("message")
    c = m.get("content") if isinstance(m, dict) else None
    if isinstance(c, list):
        c = "\n".join(p.get("text") or "" for p in c if isinstance(p, dict) and p.get("type") == "text")
    return c.strip() if isinstance(c, str) else ""


def _wrapper(d: dict) -> bool:
    # Command / task-notification wrappers come in as user lines with string content "<tag...".
    m = d.get("message")
    c = m.get("content") if isinstance(m, dict) else None
    return d["type"] == "user" and isinstance(c, str) and c.lstrip().startswith(WRAPPERS)


def last_turns(path: str | None, n: int = 3) -> list[dict]:
    try:
        with open(path, "rb") as f:
            start = max(0, os.fstat(f.fileno()).st_size - TAIL_BYTES)
            f.seek(start)
            raw = f.read().split(b"\n")[1 if start else 0:]
    except (OSError, TypeError):
        return []
    turns = []
    for line in raw:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("type") not in ("user", "assistant") or d.get("isSidechain") or d.get("isMeta") \
                or d.get("isCompactSummary") or _wrapper(d):
            continue
        if text := _text(d):
            turns.append({"role": d["type"], "text": text[:DETAIL_PART_MAX]})
    keep = {id(t) for role in ("user", "assistant") for t in [t for t in turns if t["role"] == role][-n:] if n > 0}
    turns = [t for t in turns if id(t) in keep]
    while sum(len(t["text"]) for t in turns) > DETAIL_TOTAL_MAX:
        turns.pop(0)
    return turns
