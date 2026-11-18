"""`heardback-voice trace`: trace.jsonl as a per-turn timeline (plain text, stdlib only)."""
import json
from collections import defaultdict
from pathlib import Path

from jv import trace

BAD_EVENTS = {"error", "reject", "stuck", "brain_lost", "tmux_bad_line", "claude_exit", "claude_off", "mcp_failed"}
QUOTE = 80


def load(p: Path | None = None) -> list[dict]:
    """Events from trace.jsonl.1 then trace.jsonl (older rotations are for jq); bad lines skipped."""
    p = p or trace.path()
    out = []
    for f in (p.with_name(p.name + ".1"), p):
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict):
                out.append(e)
    return out


def turns(events: list[dict]) -> dict[str, list[dict]]:
    """turn id → its events in time order; dicts keep first-seen order, so turns come out oldest first."""
    by: dict[str, list[dict]] = defaultdict(list)
    for e in sorted(events, key=lambda e: str(e.get("ts", ""))):
        if e.get("turn"):
            by[e["turn"]].append(e)
    return dict(by)


def _rc_bad(rc) -> bool:
    return rc not in (0, 3, None)


def is_bad(evs: list[dict]) -> bool:
    for e in evs:
        k = e.get("ev")
        if k in BAD_EVENTS or (k == "permission" and e.get("decision") == "deny") or \
                (k == "tool_result" and e.get("is_error")) or (k in ("jv", "run") and _rc_bad(e.get("rc"))) or \
                (k == "turn_end" and e.get("outcome") != "done") or (k == "result" and e.get("is_error")):
            return True
    return False


def _q(text) -> str:
    t = " ".join(str(text or "").split())
    return json.dumps(t if len(t) <= QUOTE else t[:QUOTE - 1] + "…", ensure_ascii=False)


def _hms(e: dict) -> str:
    return str(e.get("ts", ""))[11:19] or "--:--:--"


def render(tid: str, evs: list[dict]) -> list[str]:
    kinds = {e["ev"] for e in evs}
    first = evs[0]
    head = f"{_hms(first)} #{tid}"
    parent = next((e.get("parent") for e in evs if e.get("parent")), None)
    heard = next((e for e in evs if e["ev"] == "heard"), None)
    route = next((e for e in evs if e["ev"] == "route"), None)
    if heard:
        head += f" heard {_q(heard.get('text'))} (stt {heard.get('stt_ms', 0) / 1000:.2f}s)"
    elif route:
        head += f" text {_q(route.get('text'))}"
    else:
        head += f" {first['ev']}"
    if parent:
        head += f" (reply to #{parent})"
    acts, ends, denied = [], [], set()
    for e in evs:
        k = e["ev"]
        if k == "route":
            r = e.get("route")
            acts.append(f"claude {e.get('model')}" if r == "claude" else
                        f"fast {e.get('verb')} {json.dumps(e.get('args') or {}, ensure_ascii=False)}" if r == "fast"
                        else f"{r} {e.get('verb') or e.get('why') or ''}".strip())
        elif k == "jv" and "--dry-run" not in (e.get("argv") or []):
            acts.append(f"jv {' '.join(e.get('argv', [])[1:3])} rc={e.get('rc')}"
                        + (f" {_q(e.get('stderr'))}" if _rc_bad(e.get("rc")) and e.get("stderr") else "")
                        + f" {e.get('ms')}ms")
        elif k == "run" and _rc_bad(e.get("rc")):
            acts.append(f"jv run {' '.join(e.get('argv', [])[:2])} rc={e.get('rc')} {_q(e.get('error'))}")
        elif k == "permission":
            if e.get("decision") == "deny":
                denied.add(e.get("tool"))
                acts.append(f"tool {e.get('tool')} DENIED rc={e.get('dry_run_rc')} {_q(e.get('reason'))}")
            elif e.get("readback") and "confirm_asked" not in kinds:
                acts.append(f"asked {_q(e.get('readback'))} → {e.get('answer')}")
        elif k == "tool_result" and e.get("name") not in denied:
            acts.append(f"tool {e.get('name')} " + (f"error {_q(e.get('text'))}" if e.get("is_error") else "ok"))
        elif k == "confirm_asked":
            acts.append(f"asked {_q(e.get('text'))}")
        elif k == "confirm_heard":
            acts.append(f"answer {e.get('answer')} {_q(e.get('text'))}")
        elif k == "result":
            acts.append(f"result {e.get('subtype')} {(e.get('ms') or 0) / 1000:.1f}s ${e.get('cost') or 0:.4f}")
        elif k == "reject":
            acts.append(f"REJECT {e.get('why')}")
        elif k == "error":
            acts.append(f"ERROR {e.get('src')} {e.get('where')}: {e.get('error')}")
        elif k in ("tmux_bad_line", "claude_exit", "claude_off", "mcp_failed", "claude_reset"):
            acts.append(f"{k} {e.get('reason') or e.get('code') or _q(e.get('line'))}")
        elif k in ("stuck", "barge", "brain_lost"):
            ends.append(k.upper())
        elif k == "followup":
            ends.append(f"followup {e.get('outcome')}")
    said = [e for e in evs if e["ev"] == "say" and not e.get("announce") and e.get("text")]
    asked = any(e.get("expect_reply") for e in evs if e["ev"] == "say") or \
        any(e.get("asked") for e in evs if e["ev"] == "reply")
    if said:
        text = " ".join(e["text"] for e in said)
    else:
        text = next((e.get("text") for e in evs if e["ev"] == "reply"), None)
    if text:
        ends.insert(0, f"said {_q(text)}" + (" (asked)" if asked else ""))
    end = next((e for e in evs if e["ev"] == "turn_end"), None)
    if end:
        ends.append(f"end {end.get('outcome')} {end.get('total_s') or 0:.1f}s")
    out = [head]
    if acts:
        out.append("  → " + " · ".join(acts))
    if ends:
        out.append("  ← " + " · ".join(ends))
    return out


def main(last: int = 10, turn: str | None = None, errors: bool = False, path: Path | None = None) -> int:
    ts = turns(load(path))
    ids = [t for t in ts if (turn is None or turn in t) and (not errors or is_bad(ts[t]))]
    if turn is None:
        ids = ids[-last:]
    for t in ids:
        print("\n".join(render(t, ts[t])))
    if not ids:
        print("no matching turns in", path or trace.path())
    return 0
