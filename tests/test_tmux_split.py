"""tmuxsplit: pane splits, pane focus, pane targets and closing a pane by voice. Router -> catalogue.build_argv ->
the real jv parser, on a throwaway tmux server (JV_TMUX_SOCKET)."""
import pytest

from heardback.brain import router
from test_tmux import desktop_config, fake_claude, wait_for  # noqa: F401  (fixtures)
from test_tmux_move import CAT, voice

S = ["tmux", "split", "--window"]


def layout(tmux_server, w="0") -> list[str]:
    """Panes of window w as 'left,top active cwd'."""
    return tmux_server("list-panes", "-t", f"0:{w}", "-F",
                       "#{pane_left},#{pane_top} #{pane_active} #{pane_current_path}").splitlines()


# --- task 1: tmux.split --------------------------------------------------------------------------
@pytest.mark.parametrize("said,argv", [
    ("split this window horizontally", [*S, "current", "--json", "--", "horizontal"]),
    ("split the window side by side", [*S, "current", "--json", "--", "horizontal"]),
    ("split the current window vertically", [*S, "current", "--json", "--", "vertical"]),
    ("Split this window top and bottom.", [*S, "current", "--json", "--", "vertical"]),
    ("split window 2 vertically", [*S, "2", "--json", "--", "vertical"]),
    ("split window two side by side", [*S, "2", "--json", "--", "horizontal"]),
    ("split this window horizontally and open claude", [*S, "current", "--run", "claude", "--json", "--", "horizontal"]),
    ("split window 3 top and bottom and run claude", [*S, "3", "--run", "claude", "--json", "--", "vertical"]),
    ("split the window vertically and start claude", [*S, "current", "--run", "claude", "--json", "--", "vertical"]),
])
def test_split_phrases(said, argv):
    assert voice(said) == ["jv", *argv]


def test_split_side_by_side_same_folder(tmux_server, run_jv, tmp_path):
    tmux_server("new-window", "-t", "0:", "-n", "two", "-c", str(tmp_path))   # 1 two (active), in tmp_path
    code, out, _ = run_jv(*voice("split this window side by side")[1:])
    assert code == 0 and out["say"] == "split window 1"
    (a, b) = layout(tmux_server, "1")
    assert a.startswith("0,0 0 ") and b.split()[0] != "0,0" and b.split()[0].endswith(",0")   # side by side
    assert b.split()[1] == "1" and b.split()[2] == str(tmp_path)   # the new pane has the focus, same folder
    assert out["target"] == tmux_server("display", "-p", "-t", "0:1", "#{pane_id}").strip()


def test_split_other_window_stacked_keeps_view(tmux_server, run_jv):
    tmux_server("new-window", "-d", "-t", "0:", "-n", "two")   # user stays on 0
    assert run_jv(*voice("split window 1 top and bottom")[1:])[0] == 0
    (a, b) = layout(tmux_server, "1")
    assert a.startswith("0,0 ") and b.startswith("0,") and b.split()[0] != "0,0"   # stacked
    assert tmux_server("display", "-p", "-t", "0", "#I").strip() == "0"


def test_split_runs_claude_with_prompt(fake_claude, tmux_server, run_jv):
    code, out, _ = run_jv("tmux", "split", "vertical", "--run", "claude", "--prompt", "hi there", "--yes", "--json")
    assert code == 0
    wait_for(fake_claude.exists)
    assert fake_claude.read_text() == "--\nhi there\n"
    assert tmux_server("display", "-p", "-t", out["target"], "#{@jv_started}").strip()


@pytest.mark.parametrize("argv", [["diagonal"], ["vertical", "--prompt", "x"]])
def test_split_rejects(tmux_server, run_jv, argv):
    assert run_jv("tmux", "split", *argv)[0] == 2
    assert len(layout(tmux_server)) == 1


def test_split_claude_outside_work_dirs_reads_back(fake_claude, tmux_server, run_jv, tmp_path, monkeypatch):
    """Task 6: same safety as tmux.new: claude in a pane outside claude_dirs reads back; shell never does."""
    monkeypatch.setenv("HOME", str(tmp_path)); monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    for i, d in enumerate(("work/p", "Downloads"), 1):
        (tmp_path / d).mkdir(parents=True)
        tmux_server("new-window", "-d", "-t", f"0:{i}", "-c", str(tmp_path / d))   # 1 = work/p, 2 = Downloads
    out_ = voice("split window 2 vertically and open claude")
    code, out, _ = run_jv(*out_[1:])
    assert code == 3 and out["confirm"] == "Open Claude in Downloads? It's outside your work folders."
    assert len(layout(tmux_server, "2")) == 1   # nothing split before the yes
    assert run_jv(*voice("split window 2 vertically")[1:])[0] == 0   # shell: no read-back
    code, out, _ = run_jv(*voice("split window 1 side by side and open claude")[1:])
    assert code == 0 and out["say"] in {"split window 1; Claude is starting in the new pane",
                                        "split window 1; Claude's readiness isn't confirmed in the new pane"}
    wait_for(fake_claude.exists)
    code, out, _ = run_jv(*yes(out_[1:]))
    assert code == 0 and len(layout(tmux_server, "2")) == 3


# --- task 2: tmux.focus --------------------------------------------------------------------------
F = ["tmux", "focus", "--json", "--"]


@pytest.mark.parametrize("said,argv", [
    ("go to the left pane", [*F, "left"]),
    ("focus the right pane", [*F, "right"]),
    ("switch to the top pane", [*F, "up"]),
    ("Bottom pane.", [*F, "down"]),
    ("focus on the upper pane", [*F, "up"]),
    ("go to the left pane in window 2", ["tmux", "focus", "--window", "2", "--json", "--", "left"]),
])
def test_focus_phrases(said, argv):
    assert voice(said) == ["jv", *argv]


@pytest.mark.parametrize("said", ["focus zen", "focus kitty"])
def test_focus_app_stays_hyprland(said):
    assert router.Router(CAT).route(said).verb == "hypr.focus"


def active(tmux_server, w="0") -> str:
    return tmux_server("display", "-p", "-t", f"0:{w}", "#{pane_left},#{pane_top}").strip()


def test_focus_moves_and_stops_at_edges(tmux_server, run_jv):
    tmux_server("split-window", "-h", "-t", "0:0")   # left | right (active)
    tmux_server("split-window", "-v", "-t", "0:0")   # right splits into top / bottom (active)
    right_bottom = active(tmux_server)
    code, out, _ = run_jv(*voice("go to the top pane")[1:])
    assert code == 0 and out["say"] == "top pane" and active(tmux_server) != right_bottom
    code, _, err = run_jv(*voice("top pane")[1:])
    assert code == 1 and "no pane above in window 0" in err   # no wrap round to the bottom
    code, _, err = run_jv(*voice("right pane")[1:])
    assert code == 1 and "no pane to the right" in err
    assert run_jv(*voice("left pane")[1:])[0] == 0 and active(tmux_server) == "0,0"
    code, _, err = run_jv(*voice("left pane")[1:])
    assert code == 1 and "There's no pane to the left in window 0." in err


def test_focus_other_window(tmux_server, run_jv):
    tmux_server("new-window", "-d", "-t", "0:", "-n", "two")
    tmux_server("split-window", "-h", "-d", "-t", "0:1")   # 1: left (active) | right
    assert run_jv(*voice("go to the right pane in window 1")[1:])[0] == 0
    assert active(tmux_server, "1") != "0,0"
    assert tmux_server("display", "-p", "-t", "0", "#I").strip() == "0"   # view unchanged


# --- task 3: pane targets in resolve() -------------------------------------------------------------
@pytest.fixture
def quad(tmux_server):
    """Window 0: left | right-top / right-bottom; window 1: top / bottom. Returns {name: pane id}; user on 0's left."""
    t = tmux_server
    ids = {"left": t("display", "-p", "-t", "0:0", "#{pane_id}").strip()}
    ids["rtop"] = t("split-window", "-h", "-P", "-F", "#{pane_id}", "-t", ids["left"]).strip()
    ids["rbot"] = t("split-window", "-v", "-P", "-F", "#{pane_id}", "-t", ids["rtop"]).strip()
    t("select-pane", "-t", ids["left"])
    ids["top"] = t("new-window", "-d", "-P", "-F", "#{pane_id}", "-t", "0:").strip()
    ids["bot"] = t("split-window", "-v", "-d", "-P", "-F", "#{pane_id}", "-t", ids["top"]).strip()
    return ids


@pytest.mark.parametrize("target,want", [
    ("this pane", "left"), ("current pane", "left"), ("the left pane", "left"),
    ("right pane", "rtop"),                       # two on the right, none active: the top one
    ("bottom pane", "rbot"), ("top pane of window 0", "rtop"),   # the full-height left one isn't "top"
    ("bottom pane of window 1", "bot"), ("the top pane in window 1", "top"), ("Lower pane of window one", "bot"),
    ("left pane of window 1", "top"),             # stacked: both on the left, the active one
])
def test_resolve_pane_targets(quad, target, want):
    from jv import tmux
    assert tmux.resolve(target)["id"] == quad[want]


def test_resolve_right_pane_prefers_active(quad, tmux_server):
    from jv import tmux
    tmux_server("select-pane", "-t", quad["rbot"])
    assert tmux.resolve("right pane")["id"] == quad["rbot"]


@pytest.mark.parametrize("target", ["0", "1", "current", "first", "0:0", "window one"])
def test_window_targets_unchanged(quad, target):
    from jv import tmux
    want = {"0": "left", "current": "left", "0:0": "left", "1": "top", "first": "left", "window one": "top"}[target]
    assert tmux.resolve(target)["id"] == quad[want]


def test_verbs_work_on_a_pane(quad, tmux_server, run_jv):
    assert run_jv("tmux", "type", "bottom pane of window 1", "echo hi-bot", "--enter", "--yes")[0] == 0
    wait_for(lambda: "hi-bot" in tmux_server("capture-pane", "-p", "-t", quad["bot"]))
    assert "hi-bot" not in tmux_server("capture-pane", "-p", "-t", quad["top"])
    code, out, _ = run_jv("tmux", "read", "bottom pane of window 1", "--json")
    assert code == 0 and "hi-bot" in out["text"]
    run_jv("tmux", "type", "right pane", "half typed")
    wait_for(lambda: "half typed" in tmux_server("capture-pane", "-p", "-t", quad["rtop"]))
    assert run_jv("tmux", "clear", "right pane")[0] == 0
    wait_for(lambda: "half typed" not in tmux_server("capture-pane", "-p", "-t", quad["rtop"]))
    code, out, _ = run_jv("tmux", "prompt", "top pane of window 1", "--json")
    assert code == 0 and out["say"] == "Window 1 isn't asking anything."


def test_pane_of_missing_window(quad, run_jv):
    code, _, err = run_jv("tmux", "read", "left pane of window 7")
    assert code == 1 and "no window 7" in err


# --- task 4: tmux.close_pane ---------------------------------------------------------------------
C = ["tmux", "close_pane"]


@pytest.mark.parametrize("said,argv", [
    ("close this pane", [*C, "--json", "--", "this pane"]),
    ("close the pane", [*C, "--json", "--", "this pane"]),
    ("kill the left pane", [*C, "--json", "--", "left pane"]),
    ("Close the bottom pane in window 2.", [*C, "--window", "2", "--json", "--", "bottom pane"]),
    ("close the top pane of window two", [*C, "--window", "2", "--json", "--", "top pane"]),
])
def test_close_pane_phrases(said, argv):
    assert voice(said) == ["jv", *argv]


def test_close_window_still_closes_windows():
    assert voice("close window 2") == ["jv", "tmux", "close", "--json", "--", "2"]


def yes(argv: list[str]) -> list[str]:
    i = argv.index("--")
    return [*argv[:i], "--yes", *argv[i:]]


def ids(tmux_server, w) -> set[str]:
    return set(tmux_server("list-panes", "-t", f"0:{w}", "-F", "#{pane_id}").split())


def test_close_pane_reads_back_then_kills(quad, tmux_server, run_jv):
    argv = voice("close the bottom pane in window 1")[1:]
    code, out, _ = run_jv(*argv)
    assert code == 3 and out["confirm"] == "Close the bottom pane of window 1?" and ids(tmux_server, 1) == {quad["top"], quad["bot"]}
    code, out, _ = run_jv(*yes(argv))
    assert code == 0 and out["say"] == "closed the bottom pane of window 1" and ids(tmux_server, 1) == {quad["top"]}


def test_close_pane_names_this_pane_and_claude(quad, tmux_server, state_dir, run_jv, monkeypatch):
    from jv import tmux
    from heardback.status import store
    monkeypatch.setattr(store, "alive", lambda pid: True)
    monkeypatch.setattr(tmux, "_tmux_foreground", lambda: True)
    store.write_session({"session_id": "s1", "state": "idle", "claude_pid": 4242, "tmux_pane": quad["left"],
                         "tmux_target": "0:0.0", "updated_at": store.now_iso()})
    code, out, _ = run_jv(*voice("close this pane")[1:])
    assert code == 3 and out["confirm"] == "Close this pane in window 0? It's a Claude session."
    code, out, _ = run_jv("tmux", "close_pane", "0", "--json")   # a window target = its active pane
    assert out["confirm"] == "Close the active pane of window 0? It's a Claude session."


def test_close_last_pane_says_window_closes(quad, tmux_server, run_jv):
    tmux_server("kill-pane", "-t", quad["bot"])
    argv = voice("close the top pane of window 1")[1:]
    code, out, _ = run_jv(*argv)
    assert code == 3 and out["confirm"] == "Close window 1 " + tmux_server(
        "display", "-p", "-t", "0:1", "#W").strip() + "? It's the only pane, so the window closes."
    code, out, _ = run_jv(*yes(argv))
    assert code == 0 and out["say"] == "closed window 1"
    assert tmux_server("list-windows", "-t", "0", "-F", "#I").split() == ["0"]


@pytest.mark.parametrize("said", ["type in the left pane of window 2 git status", "what does the bottom pane say",
                                  "clear the line in the right pane"])
def test_pane_targets_go_via_claude(said):
    assert router.Router(CAT).route(said) is None   # Claude passes the pane as target
