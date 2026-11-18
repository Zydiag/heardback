from jv.cli import all_verbs, catalogue
from jv.selftest import problems


def test_real_catalogue_clean(tmux_server):
    assert problems(catalogue(all_verbs())) == []


def test_problems_catches_bad_argv():
    cat = {"version": 1, "verbs": [{"name": "a.b", "domain": "a", "verb": "b", "confirm": "maybe",
           "voice": True, "params": {"type": "object", "properties": {}}, "argv": ["x"], "args": {}, "phrases": []}]}
    assert len(problems(cat)) >= 2


def _verb(**kw):
    v = {"name": "say", "domain": "say", "verb": "say", "confirm": False, "voice": True,
         "params": {"type": "object", "properties": {"text": {"type": "string"}, "n": {"type": "integer"}},
                    "required": ["text"]},
         "argv": ["n", "text"], "args": {"text": {"type": "text"}, "n": {"type": "int"}},
         "phrases": ["say {text:text}", "(in )?{n:int}"]}
    v.update(kw)
    return {"version": 1, "verbs": [v]}


def test_dotless_name_ok():
    assert problems(_verb()) == []


def test_text_not_last_and_bad_slot():
    p = problems(_verb(argv=["text", "n"], phrases=["say {text:str}", "{zz:int}"]))
    assert len(p) == 3, p


def test_duplicate_phrase_across_verbs():
    a = _verb()["verbs"][0]
    b = {**a, "name": "talk", "domain": "talk", "verb": "talk"}
    p = problems({"version": 1, "verbs": [a, b]})
    assert len(p) == 2 and all("already used by say" in x for x in p), p
