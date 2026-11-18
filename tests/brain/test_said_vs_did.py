"""The trace checker uses fixture events only and never opens the live trace."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch
import pytest


checker_path = Path(__file__).parents[2] / "scripts/said_vs_did.py"
if not checker_path.is_file():
    pytest.skip("developer trace checker isn't included in the public export", allow_module_level=True)
spec = importlib.util.spec_from_file_location("said_vs_did", checker_path)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def event(ev, second, turn="parent", src="brain", **fields):
    return dict(ev=ev, ts=f"2026-11-16T00:00:{second:02d}+00:00", turn=turn, src=src, **fields)


def test_four_properties_and_voice_parent_correlation():
    rows = [event("listen", 0, src="voice"), event("route", 1, route="claude"),
            event("jv", 2, rc=3, argv=["jv", "tmux", "close", "--dry-run"]),
            event("confirm_asked", 2, src="voice", text="Close?"),
            event("permission", 3, decision="deny", readback="Close?"),
            event("say", 4, src="voice", text="Okay."), event("turn_end", 5, src="voice"),
            event("route", 6, turn="child", route="claude", parent="parent"),
            event("jv", 7, turn="child", rc=3),
            event("permission", 12, turn="child", decision="deny", readback="Close 6?"),
            event("reply", 13, turn="child", spoken=["I didn't do it."])]
    assert list(checker.counts(rows).values()) == [1, 1, 1, 1]
    # Keeping the parent voice turn open through completion removes detachment.
    rows[6] = event("turn_end", 15, src="voice")
    rows.append(event("confirm_asked", 8, src="voice", text="Close 6?"))
    assert list(checker.counts(rows).values()) == [1, 1, 0, 0]


def test_checker_path_argument_read_only_and_partial_tail(tmp_path, capsys):
    path = tmp_path / "trace.jsonl"
    content = json.dumps(event("listen", 0, src="voice")) + '\n{"partial":'
    path.write_text(content)
    with patch("sys.argv", ["said_vs_did.py", str(path)]):
        checker.main()
    assert path.read_text() == content
    assert "Skipped malformed lines: 1" in capsys.readouterr().out
