import json
import time

import pytest

from heardback.status import store
from pathlib import Path


@pytest.fixture(autouse=True)
def desktop_config(monkeypatch, tmp_path):
    config = tmp_path / "desktop.toml"
    config.write_text('[tmux]\nsession = "0"\nproject_roots = ["~/work/projects", "~/work", "~/claude"]\n'
                      'claude_dirs = ["~/work", "~/claude"]\nclaude_home = "~/claude"\n')
    monkeypatch.setenv("HEARDBACK_CONFIG", str(config))


def wait_for(cond, timeout=3.0):
    # Poll instead of a fixed sleep: shells start at different speeds.
    end = time.monotonic() + timeout
    while time.monotonic() < end and not cond():
        time.sleep(0.05)


def test_list_shows_windows(tmux_server, run_jv):
    tmux_server("new-window", "-t", "0:", "-n", "web")
    code, out, _ = run_jv("tmux", "list", "--json")
    assert code == 0 and {p["name"] for p in out["panes"]} >= {"first", "web"}

def test_select_by_number_and_name(tmux_server, run_jv):
    tmux_server("new-window", "-t", "0:", "-n", "web")
    assert run_jv("tmux", "select", "0", "--no-focus")[0] == 0
    assert tmux_server("display", "-p", "-t", "0", "#{window_name}").strip() == "first"
    assert run_jv("tmux", "select", "we", "--no-focus")[0] == 0
    assert tmux_server("display", "-p", "-t", "0", "#{window_name}").strip() == "web"

def test_resolve_two_claude_windows(tmux_server, run_jv):
    tmux_server("new-window", "-t", "0:", "-n", "claude"); tmux_server("new-window", "-t", "0:", "-n", "claude")
    code, out, _ = run_jv("tmux", "select", "claude", "--no-focus", "--json")
    assert code == 2 and len(out["candidates"]) == 2

def test_select_unknown_window_lists_windows(tmux_server, run_jv):
    code, out, err = run_jv("tmux", "select", "9", "--no-focus", "--json")
    assert code == 1 and "first" in err

def test_new_dir_must_be_under_home(tmux_server, run_jv):
    assert run_jv("tmux", "new", "--dir", "/etc", "--json")[0] == 2

def test_new_run_closed_enum(tmux_server, run_jv):
    assert run_jv("tmux", "new", "--run", "htop")[0] == 2

def test_new_and_read(tmux_server, run_jv, tmp_path, monkeypatch):
    from jv import tmux
    monkeypatch.setattr(tmux, "_focus_kitty", lambda: pytest.fail("plain new must keep desktop focus"))
    monkeypatch.setenv("HOME", str(tmp_path)); (tmp_path / "proj").mkdir()
    code, out, _ = run_jv("tmux", "new", "--dir", str(tmp_path / "proj"), "--name", "p", "--json")
    assert code == 0
    tmux_server("send-keys", "-t", out["target"], "echo hi-there", "Enter")
    read = lambda: run_jv("tmux", "read", out["target"], "--json")[1]["text"]
    wait_for(lambda: read().count("hi-there") >= 2)
    assert "hi-there" in run_jv("tmux", "read", out["target"], "--json")[1]["text"]

def test_claude_pane_from_store(tmux_server, state_dir, run_jv, monkeypatch):
    monkeypatch.setattr(store, "alive", lambda pid: True)  # fixture files have no live claude
    pid = tmux_server("display", "-p", "-t", "0:0", "#{pane_id}").strip()
    (state_dir / "s1.json").write_text(json.dumps({"session_id": "s1", "tmux_pane": pid, "state": "working"}))
    out = run_jv("tmux", "list", "--json")[1]
    assert out["panes"][0]["claude"] is True and out["panes"][0]["state"] == "working"


@pytest.fixture
def cat_pane(tmux_server, tmp_path):
    f = tmp_path / "got.txt"
    tmux_server("new-window", "-t", "0:", "-n", "cat", f"cat > {f}")
    return f

def got(f, want):
    wait_for(lambda: f.exists() and f.read_text() == want)
    return f.read_text()

PROMPT = "printf 'Do you want to proceed?\\n❯ 1. Yes\\n  2. No\\n'; sleep 30"

def prompt_pane(tmux_server):
    tmux_server("new-window", "-t", "0:", "-n", "p", PROMPT)
    wait_for(lambda: "2. No" in tmux_server("capture-pane", "-p", "-t", "0:p"))

def test_type_single_line_with_enter_needs_yes(cat_pane, run_jv):
    assert run_jv("tmux", "type", "cat", "hello", "--enter", "--json")[0] == 3
    assert run_jv("tmux", "type", "cat", "hello", "--enter", "--yes")[0] == 0
    assert got(cat_pane, "hello\n") == "hello\n"

def test_type_literal_metachars(cat_pane, run_jv):
    s = "; rm -rf ~ $HOME C-c Enter -x"
    assert run_jv("tmux", "type", "cat", s, "--enter", "--yes")[0] == 0
    assert got(cat_pane, s + "\n") == s + "\n"

def test_type_multiline_bracketed(cat_pane, run_jv):
    assert run_jv("tmux", "type", "cat", "a\nb", "--enter", "--yes")[0] == 0
    assert got(cat_pane, "a\nb\n").replace("\x1b[200~", "").replace("\x1b[201~", "") == "a\nb\n"

def test_type_refuses_on_prompt(tmux_server, run_jv):
    prompt_pane(tmux_server)
    assert run_jv("tmux", "type", "p", "x", "--json")[0] == 4

def test_type_cancels_copy_mode(cat_pane, tmux_server, run_jv):
    tmux_server("copy-mode", "-t", "0:cat")
    assert run_jv("tmux", "type", "cat", "z", "--enter", "--yes")[0] == 0
    assert got(cat_pane, "z\n") == "z\n"

def test_type_without_enter_is_not_queued_when_working(cat_pane, tmux_server, state_dir, run_jv, monkeypatch):
    monkeypatch.setattr(store, "alive", lambda pid: True)
    pid = tmux_server("display", "-p", "-t", "0:cat", "#{pane_id}").strip()
    (state_dir / "s.json").write_text(json.dumps({"session_id": "s", "tmux_pane": pid, "state": "working"}))
    code, out, _ = run_jv("tmux", "type", "cat", "later", "--json")
    assert code == 0 and out["queued"] is False
    assert out["say"] == "typed into window 1 without pressing Enter"

def test_answer_reads_option_and_gates(tmux_server, run_jv):
    prompt_pane(tmux_server)
    code, out, _ = run_jv("tmux", "answer", "p", "2", "--json")
    assert code == 3 and "'No'" in out["confirm"]

def test_answer_choice_pattern(tmux_server, run_jv):
    assert run_jv("tmux", "answer", "first", "12", "--yes")[0] == 2

def test_answer_without_prompt_refused(tmux_server, run_jv):
    assert run_jv("tmux", "answer", "first", "1", "--yes")[0] == 4

def test_text_cap(cat_pane, run_jv):
    assert run_jv("tmux", "type", "cat", "x" * 4001)[0] == 2

def test_select_fuzzy_miss_lists_windows(tmux_server, run_jv):
    code, out, err = run_jv("tmux", "select", "zzzz", "--no-focus", "--json")
    assert code == 1 and "first" in err

def test_type_rejects_control_chars(cat_pane, run_jv):
    assert run_jv("tmux", "type", "cat", "ls\rrm -rf ~", "--yes")[0] == 2
    time.sleep(0.3); assert not cat_pane.exists() or cat_pane.read_text() == ""

def test_type_multiline_without_enter_needs_yes(cat_pane, run_jv):
    assert run_jv("tmux", "type", "cat", "a\nb", "--json")[0] == 3

def test_type_prompt_refused_before_confirm(tmux_server, run_jv):
    prompt_pane(tmux_server)
    assert run_jv("tmux", "type", "p", "x", "--enter", "--json")[0] == 4


def test_select_session_window_form(tmux_server, run_jv):
    tmux_server("new-window", "-t", "0:", "-n", "web")
    tmux_server("select-window", "-t", "0:0")
    assert run_jv("tmux", "select", "0:1", "--no-focus")[0] == 0
    assert tmux_server("display", "-p", "-t", "0", "#{window_name}").strip() == "web"

def test_new_project_symlink_outside_home(tmux_server, run_jv, tmp_path, tmp_path_factory, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "work/projects").mkdir(parents=True)
    (tmp_path / "work/projects/evil").symlink_to(tmp_path_factory.mktemp("outside"))
    assert run_jv("tmux", "new", "--dir", "evil", "--json")[0] == 2

def test_new_hash_in_name_and_dir(tmux_server, run_jv, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path)); (tmp_path / "a#b").mkdir()
    code, out, _ = run_jv("tmux", "new", "--dir", str(tmp_path / "a#b"), "--name", "C#", "--json")
    assert code == 0
    assert tmux_server("display", "-p", "-t", out["target"], "#{window_name}").strip() == "C#"
    cwd = lambda: tmux_server("display", "-p", "-t", out["target"], "#{pane_current_path}").strip()
    wait_for(lambda: cwd() == str(tmp_path / "a#b"))
    assert cwd() == str(tmp_path / "a#b")


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """`claude` on PATH, set before tmux_server starts (tmux execs window commands with the server's PATH):
    records its argv to tmp_path/argv and exits."""
    bin_ = tmp_path / "bin"; bin_.mkdir()
    (bin_ / "claude").write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {tmp_path}/argv\n')
    (bin_ / "claude").chmod(0o755)
    import os
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    return tmp_path / "argv"


def test_new_claude_prompt_on_argv_and_type_refused_while_starting(fake_claude, tmux_server, run_jv, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path)); monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "work" / "p").mkdir(parents=True)
    code, out, _ = run_jv("tmux", "new", "--dir", str(tmp_path / "work" / "p"), "--run", "claude",
                          "--prompt", "list the files; rm -rf ~ 'q'", "--json")
    assert code == 0
    wait_for(fake_claude.exists)
    assert fake_claude.read_text() == "--\nlist the files; rm -rf ~ 'q'\n"   # one argv word, no shell
    time.sleep(0.3)
    assert out["target"] in tmux_server("list-panes", "-s", "-t", "0", "-F", "#{session_name}:#{window_index}.#{pane_index}")  # claude exited, window stays
    code, _, err = run_jv("tmux", "type", out["target"], "hello", "--enter", "--yes")
    assert code == 4 and "still starting" in err


def test_new_claude_outside_work_dirs_reads_back(tmux_server, run_jv, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path)); monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    for d in ("work/p", "Downloads", "notes/x"):
        (tmp_path / d).mkdir(parents=True)
    new = lambda d, *x: run_jv("tmux", "new", "--dir", str(tmp_path / d), *x, "--dry-run", "--json")
    assert new("work/p", "--run", "claude")[0] == 0 and new("Downloads")[0] == 0   # shell anywhere under home
    code, out, _ = new("Downloads", "--run", "claude")
    assert code == 3 and out["confirm"] == "Open Claude in Downloads? It's outside your work folders."
    (tmp_path / "cfg" / "heardback").mkdir(parents=True)
    config = tmp_path / "cfg" / "heardback" / "config.toml"
    config.write_text('[tmux]\nclaude_dirs = ["~/notes"]\n')
    monkeypatch.setenv("HEARDBACK_CONFIG", str(config))
    assert new("notes/x", "--run", "claude")[0] == 0 and new("work/p", "--run", "claude")[0] == 3


def test_new_prompt_needs_claude(tmux_server, run_jv):
    assert run_jv("tmux", "new", "--prompt", "hi")[0] == 2


def test_dir_project_by_spoken_name():
    from pathlib import Path
    from jv import tmux
    from jv.verb import JvError
    root = Path.home() / "work/projects"
    for n in ("webapp", "heardback"):
        (root / n).mkdir(parents=True, exist_ok=True)
    assert tmux._dir("web app") == (root / "webapp").resolve()
    assert "project name" in next(v for v in tmux.VERBS if v.name == "tmux.new").description
    try:
        tmux._dir("zzqq")
    except JvError as e:
        assert e.code == 1 and "heardback" in e.extra["closest"]
    else:
        raise AssertionError("no match expected")


def test_dir_short_names_are_unambiguous_and_contained(tmp_path, monkeypatch):
    from pathlib import Path
    from jv import tmux
    from jv.verb import JvError

    monkeypatch.setenv("HOME", str(tmp_path))
    roots = [tmp_path / "work" / "projects", tmp_path / "work", tmp_path / "claude"]
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
    lower = roots[0] / "lc"
    lower.mkdir()
    assert tmux._dir("LC") == lower.resolve()

    collision_a, collision_b = roots[0] / "shared", roots[1] / "shared"
    collision_a.mkdir()
    collision_b.mkdir()
    try:
        tmux._dir("shared")
    except JvError as e:
        assert e.code == 2 and set(e.extra["candidates"]) == {str(collision_a), str(collision_b)}
    else:
        raise AssertionError("duplicate direct children must be ambiguous")

    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (roots[0] / "escape").symlink_to(outside, target_is_directory=True)
    try:
        tmux._dir("escape")
    except JvError as e:
        assert e.code == 2 and "under your home" in e.msg
    else:
        raise AssertionError("a project symlink outside home must be rejected")


def test_new_claude_without_dir_opens_in_home_claude(tmux_server, run_jv, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path)); monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    new = lambda *x: run_jv("tmux", "new", *x, "--dry-run", "--json")
    code, out, _ = new("--run", "claude")     # no ~/claude yet: home, which needs a read-back
    assert code == 3 and "home folder" in out["confirm"]
    (tmp_path / "claude").mkdir()
    assert new("--run", "claude")[0] == 0     # ~/claude: no read-back
    (tmp_path / "claude" / "x").mkdir()
    assert new("--dir", str(tmp_path / "claude" / "x"), "--run", "claude")[0] == 0
    code, out, _ = run_jv("tmux", "new", "--run", "shell", "--json")   # a shell still opens in home
    assert code == 0
    cwd = tmux_server("display-message", "-p", "-t", out["target"], "#{pane_current_path}").strip()
    assert cwd == str(tmp_path.resolve())


def test_prompt_no_dir_means_home_claude():
    from model_text import description
    assert "No folder named means the configured Claude home" in description("tmux.new")


# --- context: type the clipboard / selection (fake wl-paste, never the real clipboard) ---
@pytest.fixture
def clip(monkeypatch):
    from jv import ctx
    data = {"clipboard": "echo from clipboard\n", "selection": None}
    monkeypatch.setattr(ctx, "paste", lambda primary=False: data["selection" if primary else "clipboard"] or "")
    monkeypatch.setattr(ctx, "_selection_guard", lambda: None)
    return data

def test_type_clipboard_reads_back_and_types(cat_pane, clip, run_jv):
    code, out, _ = run_jv("tmux", "type", "--enter", "--clipboard", "--json", "--", "cat")
    assert code == 3 and out["confirm"] == "Run 'echo from clipboard' in window 1?"   # trailing newline dropped
    assert run_jv("tmux", "type", "--enter", "--clipboard", "--source-revision", out["source_revision"],
                  "--yes", "--", "cat")[0] == 0
    assert got(cat_pane, "echo from clipboard\n") == "echo from clipboard\n"


def test_type_clipboard_yes_without_revision_only_returns_readback(cat_pane, clip, run_jv):
    code, out, _ = run_jv("tmux", "type", "--enter", "--clipboard", "--yes", "--json", "--", "cat")
    assert code == 3 and out["source_revision"].startswith("clipboard:")
    assert got(cat_pane, "") == ""


def test_type_clipboard_changed_after_readback_is_refused(cat_pane, clip, run_jv):
    _, preflight, _ = run_jv("tmux", "type", "--enter", "--clipboard", "--json", "--", "cat")
    clip["clipboard"] = "echo changed\n"
    code, out, _ = run_jv("tmux", "type", "--enter", "--clipboard", "--source-revision",
                          preflight["source_revision"], "--yes", "--json", "--", "cat")
    assert code == 4 and "changed after read-back" in out["error"]
    assert got(cat_pane, "") == ""


def test_type_clipboard_without_readback_still_pins_check_to_dispatch(cat_pane, clip, monkeypatch, run_jv):
    from jv import ctx
    reads = iter(("first", "changed"))
    monkeypatch.setattr(ctx, "read", lambda src: (text := next(reads), ctx.revision(src, text)))
    code, out, _ = run_jv("tmux", "type", "--clipboard", "--json", "--", "cat")
    assert code == 4 and "changed after read-back" in out["error"]
    assert got(cat_pane, "") == ""

def test_type_selection_empty_says_so(cat_pane, clip, run_jv):
    code, out, _ = run_jv("tmux", "type", "--selection", "--json", "--", "cat")
    assert code == 1 and out["say"] == "nothing is selected"

def test_type_clipboard_and_text_conflict(cat_pane, clip, run_jv):
    assert run_jv("tmux", "type", "--clipboard", "--", "cat", "hi")[0] == 2

def test_type_needs_text_or_source(cat_pane, run_jv):
    code, _, err = run_jv("tmux", "type", "--", "cat")
    assert code == 2 and "missing argument: text" in err

def test_type_clipboard_refused_on_prompt(tmux_server, clip, run_jv):
    prompt_pane(tmux_server)
    assert run_jv("tmux", "type", "--clipboard", "--enter", "--", "p")[0] == 4
