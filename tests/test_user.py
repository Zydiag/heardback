import importlib
import json
import os
import time
from pathlib import Path

import pytest

from jv import selftest, user
from jv.cli import catalogue

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "tests" / "bin"

OK = '''
[[action]]
name = "echo-it"
description = "Echo a title."
phrases = ["echo {title:text}"]
confirm = false
run = [["jvt-echo", "--", "{title}"]]
[action.args.title]
type = "text"
'''


@pytest.fixture(autouse=True)
def _bin_on_path(monkeypatch):
    monkeypatch.setenv("PATH", f"{BIN}{os.pathsep}{os.environ['PATH']}")


@pytest.fixture
def actions(tmp_path, monkeypatch):
    f = tmp_path / "actions.toml"
    monkeypatch.setenv("HEARDBACK_ACTIONS", str(f))

    def write(text):
        f.write_text(text)
        return f
    return write


def test_valid_entry_defaults():
    (e,), probs = user.load(OK)
    assert probs == [] and e["background"] is False and e["timeout_s"] == 10 and e["args"]["title"]["type"] == "text"


@pytest.mark.parametrize("patch,needle", [
    (('name = "echo-it"', 'name = "Echo"'), "bad name"),
    (('name = "echo-it"', 'name = "check"'), "bad name"),
    (('"{title}"', '"{nope}"'), "nope"),
    (('"jvt-echo"', '"no-such-bin-xyz"'), "no-such-bin-xyz not found"),
    (('confirm = false', 'confrim = false'), "unknown key confrim"),
    (('["jvt-echo", "--", "{title}"]', '["bash", "-c", "echo {title}"]'), "bash"),
    (('["jvt-echo", "--", "{title}"]', '["python3", "-c", "print(1)", "{title}"]'), "python3"),
    (('["jvt-echo", "--", "{title}"]', '["jv", "do", "x"]'), "do steps are not allowed"),
    (('["jvt-echo", "--", "{title}"]', '["jv", "tmux", "answer", "1", "y"]'), "confirm = true"),
    (('confirm = false', 'confirm = false\ntimeout_s = 61'), "timeout_s"),
])
def test_bad_entry_is_one_problem(patch, needle):
    entries, probs = user.load(OK.replace(*patch))
    assert entries == [] and len(probs) == 1 and needle in probs[0], probs


def test_fixed_shell_step_allowed_for_user():
    entries, probs = user.load(OK.replace('["jvt-echo", "--", "{title}"]', '["bash", "-c", "true"]'))
    assert probs == [] and len(entries) == 1


def test_duplicate_and_syntax_error():
    assert user.load(OK + OK)[1] == ["echo-it: duplicate name"]
    entries, probs = user.load("[[action]\nname=")
    assert entries == [] and "line" in probs[0]


@pytest.fixture(autouse=True)
def _reload_user_after(monkeypatch):
    yield
    monkeypatch.delenv("HEARDBACK_ACTIONS", raising=False)
    importlib.reload(user)


def test_to_verb_shape(actions):
    actions(OK)
    verbs, probs = user.build()
    v = next(v for v in verbs if v.name == "do.echo-it")
    assert probs == [] and v.argv == ["title"] and v.confirm is False
    assert v.params == {"type": "object", "properties": {"title": {"type": "string", "maxLength": 200}}, "required": ["title"]}
    assert v.args == {"title": {"type": "text"}} and v.phrases == ["echo {title:text}"]
    assert selftest.problems(catalogue(verbs)) == []


def test_missing_file_only_fixed(actions, monkeypatch):
    monkeypatch.setenv("HEARDBACK_ACTIONS", "/nonexistent/actions.toml")
    verbs, probs = user.build()
    assert {v.name for v in verbs} == {v.name for v in user.FIXED} and probs == []


def test_broken_file_keeps_builtins(actions, run_jv, monkeypatch):
    actions(OK + '\n[[action]]\nname = "bad"\ndescription = "x"\nrun = [["no-such-bin-xyz"]]\n')
    importlib.reload(user)
    names = {v["name"] for v in run_jv("list", "--json")[1]["verbs"]}
    assert {"tmux.select", "do.echo-it", "do.check"} <= names and "do.bad" not in names
    code, out, _ = run_jv("do", "check", "--json")
    assert code == 1 and "no-such-bin-xyz" in out["error"]


def test_check_clean(actions, run_jv):
    actions(OK)
    importlib.reload(user)
    assert run_jv("do", "check", "--json")[1]["say"] == "1 actions, no problems."


def test_loader_never_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("HEARDBACK_ACTIONS", str(tmp_path))      # a directory: read_text raises
    importlib.reload(user)
    assert "do.check" in [v.name for v in user.VERBS] and user.PROBLEMS


def test_phrase_clash_warns_but_loads(actions):
    actions('[[action]]\nname = "nw"\ndescription = "d"\nphrases = ["next wall"]\nrun = [["jvt-echo"]]\n')
    verbs, probs = user.build()
    assert "do.nw" in {v.name for v in verbs} and any("wall.next" in p for p in probs)


def test_value_is_one_argv_element(actions, run_jv_with):          # Review Focus 1
    actions(OK)
    code, out, _ = run_jv_with(user.build()[0], "do", "echo-it", "Amélie; rm -rf ~ Ocean's 11", "--json")
    assert code == 0 and json.loads(out["out"]) == ["--", "Amélie; rm -rf ~ Ocean's 11"]


def test_leading_dash_and_control_chars(actions, run_jv_with):
    actions(OK)
    v = user.build()[0]
    assert run_jv_with(v, "do", "echo-it", "--json", "--", "-rf")[0] == 2
    assert run_jv_with(v, "do", "echo-it", "a\x07b", "--json")[0] == 2


def test_dash_allowed_when_declared(actions, run_jv_with):
    actions(OK + "dash = true\n")
    assert run_jv_with(user.build()[0], "do", "echo-it", "--json", "--", "-rf")[0] == 0


def test_out_is_last_nonempty_line(actions, run_jv_with):           # Review Focus 5
    actions(OK.replace('"jvt-echo", "--"', '"jvt-echo", "--lines"'))   # jvt-echo --lines prints "a\n....\nlast\n\n\n"
    assert run_jv_with(user.build()[0], "do", "echo-it", "x", "--json")[1]["out"] == "last"


def test_last_line():
    assert user.last_line("a\n....") == "...." and user.last_line(" \n\n") == "" and user.last_line("x\n  y  \n\n") == "y"


def test_optional_slot_dropped():
    assert user.step_argv(["a", "{x}", "pre-{x}", "{home}"], {"home": "/h"}) == ["a", "pre-{x}", "/h"]
    assert user.step_argv(["{x}"], {"x": "a b"}) == ["a b"]


def test_fail_say_and_stop_on_first_failure(actions, run_jv_with, tmp_path, monkeypatch):
    monkeypatch.setenv("JVT_LOG", str(tmp_path / "log"))
    actions(OK.replace('run = [["jvt-echo", "--", "{title}"]]',
                       'run = [["jvt-echo", "--fail"], ["jvt-echo", "second"]]\nfail_say = "No: {err}."'))
    code, out, _ = run_jv_with(user.build()[0], "do", "echo-it", "x", "--json")
    assert code == 1 and out["error"] == "No: boom." and "second" not in (tmp_path / "log").read_text()


def test_timeout_kills(actions, run_jv_with):
    actions(OK.replace('"--", "{title}"', '"--sleep", "30"').replace("confirm = false", "confirm = false\ntimeout_s = 1"))
    t = time.monotonic()
    code, out, _ = run_jv_with(user.build()[0], "do", "echo-it", "x", "--json")
    assert code == 1 and "timed out" in out["error"] and time.monotonic() - t < 5


def test_confirm_gate_and_dry_run(actions, run_jv_with, tmp_path, monkeypatch):
    monkeypatch.setenv("JVT_LOG", str(tmp_path / "log"))
    actions(OK.replace("confirm = false", "confirm = true"))
    v = user.build()[0]
    code, out, _ = run_jv_with(v, "do", "echo-it", "dune", "--json")
    assert code == 3 and out["confirm"] == "Run echo-it with dune?"
    assert run_jv_with(v, "do", "echo-it", "dune", "--dry-run", "--yes", "--json")[0] == 3
    assert not (tmp_path / "log").exists()
    assert run_jv_with(v, "do", "echo-it", "dune", "--yes", "--json")[0] == 0


def test_jv_step_in_process(actions, run_jv_with, tmux_server):
    tmux_server("new-window", "-t", "0:", "-n", "second")
    tmux_server("select-window", "-t", "0:first")
    actions('[[action]]\nname = "win"\ndescription = "d"\nconfirm = false\nrun = [["jv", "tmux", "select", "--", "second"]]\n')
    code, out, _ = run_jv_with(user.build()[0], "do", "win", "--json")
    assert code == 0 and tmux_server("display", "-p", "-t", "0", "#W").strip() == "second"


def test_jv_step_failure_is_error(actions, run_jv_with, tmux_server):
    actions('[[action]]\nname = "win"\ndescription = "d"\nconfirm = false\nrun = [["jv", "tmux", "select", "nosuchwin"]]\n')
    code, out, _ = run_jv_with(user.build()[0], "do", "win", "--json")
    assert code == 1 and "nosuchwin" in out["error"]


def test_run_logged(actions, run_jv_with):
    actions(OK)
    run_jv_with(user.build()[0], "do", "echo-it", "x")
    line = json.loads((Path(os.environ["XDG_STATE_HOME"]) / "heardback/actions.log").read_text().splitlines()[-1])
    assert line["name"] == "echo-it" and line["rc"] == 0 and line["bg"] is False and "out" not in line


def test_example_file_loads(monkeypatch):
    if not (ROOT / "examples/actions.toml").is_file():
        pytest.skip("private action example is omitted from the public export")
    monkeypatch.setenv("HEARDBACK_ACTIONS", str(ROOT / "examples/actions.toml"))
    verbs, probs = user.build()
    assert "do.open-zen" in {v.name for v in verbs}
    assert all(p.endswith("not found") for p in probs), probs   # tb-play / tb-search until M5


def test_step_not_executable(tmp_path):
    f = tmp_path / "plain"
    f.write_text("")
    rc, _, err = user.run_step([str(f)], 1, False)
    assert rc == 1 and err
