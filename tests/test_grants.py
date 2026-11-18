import concurrent.futures
import json
import os
from pathlib import Path

import pytest

from jv import grants
from jv.cli import run
from jv.verb import Verb, runtime_path


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setenv("HEARDBACK_REQUIRE_GRANT", "1")
    monkeypatch.setenv("HEARDBACK_TURN", "turn")
    monkeypatch.delenv("HEARDBACK_GRANT", raising=False)
    calls = []
    verb = Verb("test.act", lambda args: calls.append(args) or {"say": "done"}, "test",
                {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}, ["x"],
                confirm=True, check=lambda args: "Act?")
    return verb, calls


def authorize(monkeypatch, **overrides):
    token = grants.mint(overrides.get("verb", "test.act"), overrides.get("args", {"x": "a"}),
                        overrides.get("turn", "turn"), "attempt")
    monkeypatch.setenv("HEARDBACK_GRANT", token)
    return Path(runtime_path("heardback-grant-" + token))


@pytest.mark.parametrize("token", ["", "forged", "0" * 64 + "-" + "0" * 32, "../heardback-execution.json"])
def test_missing_or_forged_grant_refused_even_with_yes(gated, monkeypatch, capsys, token):
    verb, calls = gated
    monkeypatch.setenv("HEARDBACK_GRANT", token)
    assert run(["test", "act", "a", "--yes", "--json"], [verb]) == 4
    assert "grant" in json.loads(capsys.readouterr().out)["error"]
    assert calls == []


def test_grant_is_private_and_runs_once_without_yes(gated, monkeypatch):
    verb, calls = gated
    path = authorize(monkeypatch)
    assert path.stat().st_mode & 0o777 == 0o600
    assert run(["test", "act", "a"], [verb]) == 0
    assert not path.exists()
    assert run(["test", "act", "a", "--yes"], [verb]) == 4
    assert calls == [{"x": "a"}]


@pytest.mark.parametrize("override", [{"args": {"x": "changed"}}, {"verb": "test.other"}, {"turn": "other"}])
def test_changed_args_verb_or_turn_refused(gated, monkeypatch, override):
    verb, calls = gated
    path = authorize(monkeypatch, **override)
    assert run(["test", "act", "a"], [verb]) == 4
    assert not path.exists() and calls == []


def test_expired_grant_refused(gated, monkeypatch):
    verb, calls = gated
    now = grants.time.time()
    path = authorize(monkeypatch)
    monkeypatch.setattr(grants.time, "time", lambda: now + 31)
    assert run(["test", "act", "a"], [verb]) == 4
    assert not path.exists() and calls == []


@pytest.mark.parametrize("data", ["[]", "null", "{}", '{"expires":"bad"}', "broken"])
def test_malformed_grant_refused(gated, monkeypatch, data):
    verb, calls = gated
    path = authorize(monkeypatch)
    path.write_text(data)
    assert run(["test", "act", "a"], [verb]) == 4
    assert calls == []


def test_consumption_is_atomic(gated, monkeypatch):
    authorize(monkeypatch)
    def consume(_):
        try:
            grants.consume(os.environ["HEARDBACK_GRANT"], "test.act", {"x": "a"}, "turn")
            return True
        except grants.JvError:
            return False
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(consume, range(16))) == 1


def test_read_runs_without_grant_and_terminal_stays_unchanged(gated, monkeypatch):
    verb, calls = gated
    verb.name = "status.list"
    verb.domain, verb.verb = "status", "list"
    verb.check = None
    assert run(["status", "list", "a"], [verb]) == 0
    monkeypatch.delenv("HEARDBACK_REQUIRE_GRANT")
    verb.name = "test.act"
    verb.domain, verb.verb = "test", "act"
    verb.check = lambda args: "Act?"
    assert run(["test", "act", "a"], [verb]) == 3
    assert run(["test", "act", "a", "--yes"], [verb]) == 0
    assert len(calls) == 2


def test_lookup_binds_attempt_and_prunes_expired(monkeypatch):
    token = grants.mint("test.act", {"x": "a"}, "turn", "attempt")
    assert grants.find("test.act", {"x": "a"}, "turn", "other-attempt") == ""
    assert grants.find("test.act", {"x": "a"}, "turn", "attempt") == token
    now = grants.time.time()
    monkeypatch.setattr(grants.time, "time", lambda: now + 31)
    assert grants.find("test.act", {"x": "a"}, "turn", "attempt") == ""
    assert not Path(runtime_path("heardback-grant-" + token)).exists()


def test_hash_is_canonical_and_matches_argv_omissions():
    assert grants.args_hash({"z": "é", "a": 1, "flag": False, "optional": None}) == \
        grants.args_hash({"a": 1, "z": "é"})


def test_grant_survives_argv_typing(monkeypatch, capsys):
    # Claude may send an integer as "5" or a string as 5; jv sees argv text and validate() coerces it.
    monkeypatch.setenv("HEARDBACK_REQUIRE_GRANT", "1")
    monkeypatch.setenv("HEARDBACK_TURN", "turn")
    calls = []
    verb = Verb("test.set", lambda args: calls.append(args) or {"say": "done"}, "test",
                {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}, ["n"], confirm=False)
    for sent in ("5", 5):
        monkeypatch.setenv("HEARDBACK_GRANT", grants.mint("test.set", {"n": sent}, "turn", "attempt"))
        assert run(["test", "set", "5", "--json"], [verb]) == 0
    assert calls == [{"n": 5}, {"n": 5}]
