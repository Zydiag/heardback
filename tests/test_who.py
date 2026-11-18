import pytest

from heardback.status import who


def s(sid, project, target=None, state="idle", pane="%1", window=None):
    return {"session_id": sid, "project": project, "tmux_target": target, "state": state,
            "tmux_pane": pane if target else None, "tmux_window": window}


SESS = [s("aaaa1111", "career", "0:1.0"), s("bbbb2222", "webapp", "0:2.0"),
        s("cccc3333", "heardback", "work:12.1"), s("dddd4444", "old", "0:2.0", state="ended")]


def test_window_number():
    assert who.resolve("2", SESS)["session_id"] == "bbbb2222"  # live beats the ended one in window 2
    assert who.resolve("12", SESS)["session_id"] == "cccc3333"


def test_target_prefix():
    assert who.resolve("work:12", SESS)["session_id"] == "cccc3333"
    assert who.resolve("0:1.0", SESS)["session_id"] == "aaaa1111"
    with pytest.raises(who.NotFound):
        who.resolve("0:9", SESS)


def test_project_case_insensitive():
    assert who.resolve("Career", SESS)["session_id"] == "aaaa1111"
    assert who.resolve("old", SESS)["session_id"] == "dddd4444"  # falls back to ended sessions


def test_id_prefix():
    assert who.resolve("cccc", SESS)["session_id"] == "cccc3333"


def test_fuzzy():
    assert who.resolve("web", SESS)["session_id"] == "bbbb2222"


def test_window_name():
    ss = [s("aaaa1111", "heardback", "0:3.0", window="build-03"), s("bbbb2222", "career", "0:4.0", window="heardback")]
    assert who.resolve("build", ss)["session_id"] == "aaaa1111"  # fuzzy on the window name
    assert who.resolve("BUILD-03", ss)["session_id"] == "aaaa1111"
    assert who.resolve("heardback", ss)["session_id"] == "aaaa1111"  # exact project beats exact window name


def test_ambiguous_project():
    two = SESS + [s("eeee5555", "career", "0:7.0")]
    with pytest.raises(who.Ambiguous) as e:
        who.resolve("career", two)
    assert len(e.value.matches) == 2


def test_not_found():
    with pytest.raises(who.NotFound):
        who.resolve("zzzzzz", SESS)
    with pytest.raises(who.NotFound):
        who.resolve("career", [])


def test_name_duplicates():
    two = SESS + [s("eeee5555", "career", "0:7.0")]
    assert who.name(two[0], two) == "career in window 1"
    out = s("ffff6666", "career")
    assert who.name(out, two + [out]) == "the career session outside tmux"


def test_name_unique():
    assert who.name(SESS[0], SESS) == "career"
    assert who.name(SESS[1], SESS) == "webapp"  # the ended "old" in window 2 doesn't count


def test_rules_fall_through():
    sess = [s("12345678", "web", "0:3.0"), s("aaaa", "web21", "0:21.0")]
    assert who.resolve("1234", sess)["session_id"] == "12345678"  # no window 1234 → id prefix
    with pytest.raises(who.NotFound):
        who.resolve("0:2", sess)  # not a prefix of 0:21


def test_name_pane_without_target_not_outside_tmux():
    two = SESS + [s("eeee5555", "career", "0:7.0"), {**s("ffff6666", "career"), "tmux_pane": "%9"}]
    assert "outside tmux" not in who.name(two[-1], two)


def test_name_collision_gets_short_id():
    a, b = s("aaaa9999", "career"), s("bbbb9999", "career")
    assert who.name(a, [a, b]) == "the career session outside tmux, id aaaa"
    assert who.name(b, [a, b]) == "the career session outside tmux, id bbbb"


NUMERIC_NAME_SESSION = {"session_id": "3333abcd", "project": "build-03", "tmux_window": "build-03",
                        "tmux_pane": "%2", "tmux_target": "0:2.0", "state": "idle"}


@pytest.mark.parametrize("query", ["3", "window 3", "windows three", "0:3.0", "%3"])
def test_unmatched_stable_window_targets_do_not_fuzzy_match_project_names(query):
    with pytest.raises(who.NotFound):
        who.resolve(query, [NUMERIC_NAME_SESSION])


@pytest.mark.parametrize("query", ["window two", "windows 2", "second window", "0:2.0", "%2"])
def test_spoken_and_stable_window_targets_resolve_exactly(query):
    assert who.resolve(query, [NUMERIC_NAME_SESSION]) == NUMERIC_NAME_SESSION


def test_numeric_window_phrase_does_not_fall_back_to_numeric_id_prefix():
    numeric_id = {**NUMERIC_NAME_SESSION, "session_id": "3000abcd"}
    assert who.resolve("3000", [numeric_id]) == numeric_id  # bare ID-prefix fallback remains available
    with pytest.raises(who.NotFound):
        who.resolve("window 3000", [numeric_id])


def test_project_lookup_still_uses_fuzzy_matching():
    assert who.resolve("build", [NUMERIC_NAME_SESSION]) == NUMERIC_NAME_SESSION
