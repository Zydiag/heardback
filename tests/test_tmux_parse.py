"""panes() parsing: titles with line separators, short records skipped + traced (task 6, live KeyError 'active')."""
import json
import os

from jv import tmux
from jv.cli import run
from jv.verb import Verb


def rec(pid, title, name="claude", cwd="~/user/u/p"):
    return "\t".join([pid, f"0:1.{pid[1:]}", "1", "1", "0", "0", "1", "0", "0", "79", "23", "claude", name, cwd, "", title])


def test_title_with_unicode_line_separators_and_newline():
    raw = "\n".join([rec("%1", "✳ fix the\x1ething\x85now"), rec("%2", "two\nlines"), rec("%3", "tab\there")])
    p1, p2, p3 = tmux.parse_panes(raw + "\n")
    assert p1["title"] == "✳ fix the\x1ething\x85now" and p1["active"] is True and p1["cwd"] == "~/user/u/p"
    assert p2["id"] == "%2" and p2["title"] == "two lines" and p2["window_active"] is True
    assert p3["title"] == "tab\there"


def test_short_record_skipped_and_traced(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("HEARDBACK_TURN", "t-9")
    ps = tmux.parse_panes(rec("%1", "ok") + "\n%2\t0:1.2\t1\n")
    assert [p["id"] for p in ps] == ["%1"]
    (ev,) = [json.loads(l) for l in (tmp_path / "heardback" / "trace.jsonl").read_text().splitlines()]
    assert ev["ev"] == "tmux_bad_line" and ev["turn"] == "t-9" and ev["line"].startswith("%2\t")


def test_unexpected_exception_reports_type_and_traces(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    v = Verb(name="t.boom", description="d", confirm=False, check=None, fn=lambda a: {}["active"],
             params={"type": "object", "properties": {}}, argv=[], phrases=[], args={})
    assert run(["t", "boom", "--json"], [v]) == 1
    out = capsys.readouterr()
    assert out.err.strip() == "KeyError: 'active'"
    assert json.loads(out.out)["error"] == "KeyError: 'active'"
    (ev,) = [json.loads(l) for l in (tmp_path / "heardback" / "trace.jsonl").read_text().splitlines()]
    assert ev["ev"] == "error" and ev["src"] == "jv" and "KeyError" in ev["traceback"]


def test_tmux_runs_with_utf8_flag(monkeypatch):
    """Live 15:15: the MCP env has no LANG, so tmux printed tab separators as "_" → KeyError 'active'."""
    import subprocess
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **k: seen.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    monkeypatch.setenv("JV_TMUX_SOCKET", "s")
    tmux.tmux("list-panes")
    assert seen == [["tmux", "-u", "-L", "s", "list-panes"]]
