import json
import os
import stat

from jv import trace


def lines(p):
    return [json.loads(l) for l in p.read_text().splitlines()]


def test_text_and_stdout_keep_the_tail():
    """Live 15:35: tmux_read's tool_result lost the bottom of the screen (the part that matters)."""
    long = "H" * 150 + "m" * 400 + "T" * 350
    got = trace.cut({"text": long, "stdout": long, "stderr": long, "args": {"text": long}})
    for k in ("text", "stdout"):
        assert got[k] == "H" * 150 + "…" + "T" * 350
    assert got["args"]["text"] == "H" * 150 + "…" + "T" * 350
    assert got["stderr"] == long[:500] + "…"
    assert trace.cut({"text": "short"}) == {"text": "short"}


def test_emit_appends_one_line_0600_and_cuts_strings(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    trace.emit("brain", "reply", "t-1", text="x" * 600, nested={"a": ["y" * 501]}, n=3)
    trace.emit("jv", "run", None, argv=["jv", "list"])
    p = tmp_path / "heardback" / "trace.jsonl"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    a, b = lines(p)
    assert a["src"] == "brain" and a["ev"] == "reply" and a["turn"] == "t-1" and a["n"] == 3
    assert len(a["text"]) == 501 and a["text"][150] == "…"
    assert len(a["nested"]["a"][0]) == 501
    assert a["ts"][10] == "T" and "." in a["ts"][19:]   # local ISO with ms
    assert b["turn"] is None and b["argv"] == ["jv", "list"]


def test_write_failure_never_raises_and_warns_once(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(trace, "_warned", False)
    (tmp_path / "heardback").write_text("a file where the dir should be")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    trace.emit("voice", "heard", "t", text="hi")
    trace.emit("voice", "heard", "t", text="hi")
    assert capsys.readouterr().err.count("heardback trace: cannot write") == 1


def test_rotate_keeps_five(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    p = tmp_path / "heardback" / "trace.jsonl"
    assert trace.rotate(max_bytes=10) is False   # no file yet
    for i in range(7):
        trace.emit("brain", "e", None, i=i)
        assert trace.rotate(max_bytes=10) is True
        assert not p.exists()
    trace.emit("brain", "e", None, i=99)          # reopened after the rotate
    assert lines(p)[0]["i"] == 99
    kept = sorted(f.name for f in p.parent.iterdir() if f.name != "trace.jsonl")
    assert kept == [f"trace.jsonl.{i}" for i in range(1, 6)]
    assert lines(p.with_name("trace.jsonl.1"))[0]["i"] == 6
    assert lines(p.with_name("trace.jsonl.5"))[0]["i"] == 2
    assert trace.rotate(max_bytes=10_000) is False   # small: left alone


def test_error_carries_traceback(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    try:
        raise KeyError("boom")
    except KeyError as e:
        trace.error("voice", "t", "bg", e)
    (rec,) = lines(tmp_path / "heardback" / "trace.jsonl")
    assert rec["ev"] == "error" and "KeyError" in rec["traceback"] and rec["where"] == "bg"
