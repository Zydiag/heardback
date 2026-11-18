import json
import pathlib
import re

from jv.cli import run, catalogue
from jv.verb import Verb, JvError
calls = []
def mk(name, check=None, confirm=False, fn=None):
    return Verb(name=name, description="d", confirm=confirm, check=check,
                fn=fn or (lambda a: calls.append((name, a)) or {"say": "done"}),
                params={"type":"object","properties":{"x":{"type":"string"},"all":{"type":"boolean"}},"required":["x"]},
                argv=["x", "--all"], phrases=["do {x:str}"], args={"x": {"type": "str"}})
V = [mk("t.go"), mk("t.rm", check=lambda a: f"Remove {a['x']}?", confirm=True),
     mk("t.bad", fn=lambda a: (_ for _ in ()).throw(JvError(4, "pane is showing a prompt")))]

def test_ok_json(capsys):
    assert run(["t", "go", "a", "--all", "--json"], V) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "say": "done"}
    assert calls[-1] == ("t.go", {"x": "a", "all": True})

def test_confirm_gate_blocks_without_yes(capsys):
    calls.clear()
    assert run(["t", "rm", "a", "--json"], V) == 3
    assert json.loads(capsys.readouterr().out)["confirm"] == "Remove a?"
    assert calls == []

def test_confirm_gate_yes_runs():
    assert run(["t", "rm", "a", "--yes"], V) == 0 and calls[-1][0] == "t.rm"

def test_dry_run_never_acts(capsys):
    calls.clear()
    assert run(["t", "rm", "a", "--dry-run", "--yes", "--json"], V) == 3
    assert run(["t", "go", "a", "--dry-run", "--json"], V) == 0
    assert calls == []

def test_jverror_exit_code_and_stderr(capsys):
    assert run(["t", "bad", "a", "--json"], V) == 4
    out = capsys.readouterr()
    assert json.loads(out.out) == {"ok": False, "error": "pane is showing a prompt"}
    assert out.err.strip() == "pane is showing a prompt"

def test_usage_error_is_2():
    assert run(["t", "nope"], V) == 2

def test_list_json(capsys):
    assert run(["list", "--json"], V) == 0
    cat = json.loads(capsys.readouterr().out)
    assert cat["version"] == 1 and cat["verbs"][0]["argv"] == ["x", "--all"]
    assert cat["verbs"][1]["confirm"] is True and cat["verbs"][0]["domain"] == "t"

def test_no_shell_true_anywhere():
    files = list((pathlib.Path(__file__).resolve().parents[1] / "src" / "jv").rglob("*.py"))
    assert files
    assert not [p for p in files if re.search(r"shell\s*=\s*True", p.read_text())]

def test_positional_dash_and_ok_not_overridable(capsys):
    v = Verb(name="t.x", fn=lambda a: {"ok": False, "say": "s"}, description="d",
             params={"type": "object", "properties": {"pane_id": {"type": "string"}},
                     "required": ["pane_id"]}, argv=["pane-id"])
    assert run(["t", "x", "p", "--json"], [v]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

def test_text_typed_positional_joins_words():
    v = Verb(name="t.say", description="d", fn=lambda a: calls.append(a) or {"say": "ok"},
             params={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
             argv=["q"], args={"q": {"type": "text"}})
    assert run(["t", "say", "rust", "borrow", "checker"], [v]) == 0 and calls[-1] == {"q": "rust borrow checker"}
