import importlib.util
import json
from datetime import datetime
from pathlib import Path
import pytest

checker_path = Path(__file__).parent.parent / "scripts" / "capability-usage.py"
if not checker_path.is_file():
    pytest.skip("developer usage checker isn't included in the public export", allow_module_level=True)
spec = importlib.util.spec_from_file_location("cu", checker_path)
cu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cu)


def run(ts, argv, rc=0, turn=None, dry=False, error=None):
    return {"ts": ts, "src": "jv", "ev": "run", "turn": turn, "argv": argv, "rc": rc, "dry_run": dry, "error": error}


def test_rows(tmp_path):
    ev = [{"ts": "2026-11-16T10:00:00.000+05:30", "src": "voice", "ev": "heard", "turn": "t1", "text": "x"},
          {"ts": "2026-11-16T11:00:00.000+05:30", "src": "voice", "ev": "heard", "turn": "t2", "text": "x"},
          run("2026-11-16T10:00:01+05:30", ["volume", "--json"], turn="t1"),                         # success
          run("2026-11-16T10:00:02+05:30", ["tmux", "list", "--dry-run"], turn="t1", dry=True),     # dry run
          run("2026-11-16T10:00:03+05:30", ["wall", "next"], turn=None),                            # typed
          run("2026-11-16T11:00:01+05:30", ["tmux", "read"], rc=1, turn="t2", error="no pane")]     # failure
    t = tmp_path / "trace.jsonl"
    t.write_text("\n".join(map(json.dumps, ev)) + "\n")
    names = ["volume", "tmux.list", "tmux.read", "wall.next"]
    u = cu.usage(cu.read(t), names, datetime.fromisoformat("2026-11-17T12:00:00+05:30"))
    assert u["volume"]["last_ok"] and u["volume"]["runs"] == 1
    assert u["tmux.list"]["last_ok"] is None and u["tmux.list"]["runs"] == 0
    assert u["wall.next"]["last_ok"] is None and u["wall.next"]["runs"] == 1
    assert (u["tmux.read"]["fails"], u["tmux.read"]["err"]) == (1, "no pane")
    md = cu.render(u, "2026-11-17")
    assert "| volume | 2026-11-16T10:00 | 1 | 0 |" in md
    assert "- tmux.list" in md and "- wall.next" in md and "- tmux.read" in md
