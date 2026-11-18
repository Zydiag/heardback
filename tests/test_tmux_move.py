"""tmuxmove: rename / move tmux windows by voice. Router -> catalogue.build_argv -> the real jv parser, on a
throwaway tmux server (JV_TMUX_SOCKET)."""
import pytest

from heardback.brain import catalogue, router
from jv.cli import all_verbs, catalogue as jv_catalogue

CAT = catalogue._index(jv_catalogue(all_verbs()))


def voice(said: str) -> list[str]:
    r = router.Router(CAT).route(said)
    assert r, f"{said!r} went to Claude"
    return catalogue.build_argv(CAT[r.verb], r.args, "--json")


def names(tmux_server) -> list[tuple[str, str]]:
    return [tuple(l.split(" ", 1)) for l in tmux_server("list-windows", "-t", "0", "-F", "#I #W").splitlines()]


@pytest.mark.parametrize("said,argv", [
    ("rename window 2 to build", ["tmux", "rename", "--json", "--", "2", "build"]),
    ("Rename window two to web server.", ["tmux", "rename", "--json", "--", "2", "web server"]),
    ("rename this window to logs", ["tmux", "rename", "--json", "--", "current", "logs"]),
    ("rename the current window to logs", ["tmux", "rename", "--json", "--", "current", "logs"]),
    ("call window 3 api", ["tmux", "rename", "--json", "--", "3", "api"]),
    ("call this window scratch", ["tmux", "rename", "--json", "--", "current", "scratch"]),
])
def test_rename_phrases(said, argv):
    assert voice(said) == ["jv", *argv]


def test_rename_end_to_end(tmux_server, run_jv):
    tmux_server("new-window", "-t", "0:", "-n", "two")   # 0 first, 1 two (active)
    code, out, _ = run_jv(*voice("Rename window one to web server.")[1:])
    assert code == 0 and out["say"] == "renamed window 1 to web server"
    code, out, _ = run_jv(*voice("rename this window to #{pane_id}")[1:])   # no format expansion
    assert code == 0 and names(tmux_server) == [("0", "first"), ("1", "#{pane_id}")]


@pytest.mark.parametrize("name,want", [("  build. ", "build"), ("x" * 40, "x" * 30)])
def test_rename_cleans_name(tmux_server, run_jv, name, want):
    assert run_jv("tmux", "rename", "0", name)[0] == 0
    assert names(tmux_server) == [("0", want)]


@pytest.mark.parametrize("name", [" . ", "a\x07b"])
def test_rename_rejects(tmux_server, run_jv, name):
    assert run_jv("tmux", "rename", "0", name)[0] == 2
    assert names(tmux_server) == [("0", "first")]


# --- tmux.move ---------------------------------------------------------------------------------
M = ["tmux", "move", "--json", "--"]


@pytest.mark.parametrize("said,args", [
    ("move window 3 to 1", ["3", "1"]),
    ("Move window three to window one.", ["3", "1"]),
    ("move window 3 to position 1", ["3", "1"]),
    ("swap windows 2 and 4", ["2", "4"]),
    ("swap window 2 with 4", ["2", "4"]),
    ("move this window to 2", ["current", "2"]),
    ("move this window to position two", ["current", "2"]),
    ("move this window left", ["current", "left"]),
    ("move this window to the right", ["current", "right"]),
    ("move window 2 left", ["2", "left"]),
    ("move window 2 to the right", ["2", "right"]),
])
def test_move_phrases(said, args):
    assert voice(said) == ["jv", *M, *args]


@pytest.mark.parametrize("said,verb", [
    ("move this window to workspace 3", "hypr.move"),
    ("move zen to workspace 2", "hypr.move"),
    ("switch to workspace 3", "hypr.workspace"),
])
def test_workspace_moves_stay_hyprland(said, verb):
    assert router.Router(CAT).route(said).verb == verb


@pytest.fixture
def four(tmux_server):
    """Windows 0 first, 1 b, 2 c, 3 d; the user is looking at 0."""
    for n in "bcd":
        tmux_server("new-window", "-d", "-t", "0:", "-n", n)
    return tmux_server


def on(tmux_server) -> str:
    return tmux_server("display", "-p", "-t", "0", "#I #W").strip()


def test_swap_this_window_follows_content(four, run_jv):
    code, out, _ = run_jv(*voice("move this window to 2")[1:])
    assert code == 0 and out["say"] == "moved window 0 to 2"
    assert names(four) == [("0", "c"), ("1", "b"), ("2", "first"), ("3", "d")]
    assert on(four) == "2 first"   # still looking at the same window


def test_swap_into_the_window_on_screen(four, run_jv):
    assert run_jv(*voice("swap windows 3 and 0")[1:])[0] == 0
    assert names(four) == [("0", "d"), ("1", "b"), ("2", "c"), ("3", "first")]
    assert on(four) == "3 first"


def test_swap_two_other_windows_keeps_focus(four, run_jv):
    assert run_jv(*voice("swap windows 1 and 3")[1:])[0] == 0
    assert names(four) == [("0", "first"), ("1", "d"), ("2", "c"), ("3", "b")]
    assert on(four) == "0 first"


def test_move_to_empty_index(four, run_jv):
    code, out, _ = run_jv(*voice("move this window to 7")[1:])
    assert code == 0 and out["say"] == "moved window 0 to 7"
    assert names(four) == [("1", "b"), ("2", "c"), ("3", "d"), ("7", "first")] and on(four) == "7 first"


def test_left_right_and_edges(four, run_jv):
    assert run_jv(*voice("move window 2 left")[1:])[0] == 0
    assert names(four) == [("0", "first"), ("1", "c"), ("2", "b"), ("3", "d")]
    assert run_jv(*voice("move this window right")[1:])[1]["say"] == "moved window 0 to 1"
    assert on(four) == "1 first"
    code, _, err = run_jv(*voice("move window 3 right")[1:])
    assert code == 1 and "already the last window" in err
    four("kill-window", "-t", "0:0")   # no window 0 now: 1 is first
    code, _, err = run_jv(*M[:2], "1", "left")
    assert code == 1 and "already the first window" in err


@pytest.mark.parametrize("to", ["web", "-1", "up"])
def test_move_bad_to(four, run_jv, to):
    assert run_jv(*M[:2], "0", to)[0] == 2


# --- task 3: a Claude session in a moved window still reports its new window ----------------------
def test_status_follows_a_moved_window(four, state_dir, run_jv, monkeypatch):
    from heardback.status import store, who
    monkeypatch.setattr(store, "alive", lambda pid: True)
    pane = four("display", "-p", "-t", "0:3", "#{pane_id}").strip()
    store.write_session({"session_id": "s1", "state": "working", "claude_pid": 4242, "tmux_pane": pane,
                         "tmux_target": "0:3.0", "updated_at": store.now_iso()})
    assert run_jv(*voice("move window 3 to 1")[1:])[0] == 0
    code, out, _ = run_jv("status", "list", "--json")
    assert code == 0 and [who.window(s) for s in out["sessions"]] == ["1"]
    assert out["sessions"][0]["tmux_window"] == "d"
