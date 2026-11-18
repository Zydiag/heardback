from jv.verb import fuzzy, JvError, validate, Verb
import pytest

def test_fuzzy_exact_case_insensitive():
    assert fuzzy("WEB", ["claude", "web", "zsh"]) == "web"

def test_fuzzy_unique_substring_wins():
    assert fuzzy("log", ["claude", "logtime", "web"]) == "logtime"

def test_fuzzy_substring_tie():
    with pytest.raises(JvError) as e:
        fuzzy("cla", ["claude-a", "claude-b"])
    assert e.value.code == 2 and set(e.value.extra["candidates"]) == {"claude-a", "claude-b"}

def test_fuzzy_whole_word_beats_substring():
    assert fuzzy("obs", ["Obsidian", "OBS Studio"]) == "OBS Studio"


def test_fuzzy_typo():
    assert fuzzy("lgotime", ["claude", "logtime", "web"]) == "logtime"

def test_fuzzy_no_match_gives_closest_three():
    with pytest.raises(JvError) as e:
        fuzzy("qqqq", ["a", "b", "c", "d"])
    assert e.value.code == 1 and len(e.value.extra["closest"]) <= 3

def _v(**props):
    return Verb(name="t.x", fn=lambda a: {}, description="", argv=list(props),
                params={"type": "object", "properties": props, "required": list(props)})

def test_validate_coerces_int_and_range():
    v = _v(n={"type": "integer", "minimum": 1, "maximum": 10})
    assert validate(v, {"n": "3"}) == {"n": 3}
    with pytest.raises(JvError) as e: validate(v, {"n": "11"})
    assert e.value.code == 2

def test_validate_text_cap():
    v = _v(text={"type": "string", "maxLength": 4000})
    with pytest.raises(JvError) as e: validate(v, {"text": "x" * 4001})
    assert e.value.code == 2

def test_validate_closed_enum():
    v = _v(run={"type": "string", "enum": ["shell", "claude"]})
    with pytest.raises(JvError): validate(v, {"run": "bash -c x"})

def test_gated_verb_needs_check():
    with pytest.raises(ValueError):
        Verb(name="a.b", fn=lambda a: {}, description="d", params={}, argv=[], confirm=True)


def test_confirm_auto_needs_check():
    with pytest.raises(ValueError):
        Verb(name="t.x", fn=lambda a: {}, description="", argv=[], params={}, confirm="auto")

def test_runtime_path_falls_back_when_unset_or_empty(monkeypatch):
    import os
    from jv.verb import runtime_path
    monkeypatch.setenv("XDG_RUNTIME_DIR", "")
    assert runtime_path("x.sock") == f"/run/user/{os.getuid()}/x.sock"
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert runtime_path("x.sock") == f"/run/user/{os.getuid()}/x.sock"
