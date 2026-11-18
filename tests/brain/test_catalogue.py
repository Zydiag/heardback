import asyncio
import json

from brainutil import script
from heardback.brain import catalogue


def test_drops_voice_false_and_caches(env):
    cat = catalogue.get(force=True)
    assert "zen.install" not in cat and "say" not in cat and "tmux.select" in cat
    assert (env / "state/heardback/catalogue.json").exists()


def test_falls_back_to_cache(env, monkeypatch):
    catalogue.get(force=True)
    monkeypatch.setenv("PATH", "/nonexistent")
    assert "tmux.select" in catalogue.get(force=True)


def test_no_cache_no_jv_is_empty(env, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    assert catalogue.get(force=True) == {}


def test_bad_json_falls_back_to_cache(env, monkeypatch):
    catalogue.get(force=True)
    bad = env / "bad.json"
    bad.write_text("not json")
    monkeypatch.setenv("FAKE_JV_CATALOGUE", str(bad))
    assert "tmux.select" in catalogue.get(force=True)


def test_legacy_say_cache_entry_is_never_voice_catalogued(env, monkeypatch):
    from jv.cli import catalogue as cli_catalogue
    from jv.say import VERBS as say_verbs

    catalogue.get(force=True)
    path = env / "state/heardback/catalogue.json"
    data = json.loads(path.read_text())
    legacy_say = cli_catalogue(say_verbs)["verbs"][0]
    legacy_say["voice"] = True
    data["verbs"].append(legacy_say)
    path.write_text(json.dumps(data))
    monkeypatch.setenv("PATH", "/nonexistent")
    cat = catalogue.get(force=True)
    assert "say" not in cat and "tmux.select" in cat


def test_memory_copy_reused_within_max_age(env, monkeypatch):
    first = catalogue.get(force=True)
    monkeypatch.setenv("PATH", "/nonexistent")
    assert catalogue.get() is first


def test_cache_write_failure_keeps_fetch(env):   # M1 review minor 3
    (env / "state/heardback/catalogue.json").mkdir(parents=True)   # unwritable as a file
    assert "tmux.select" in catalogue.get(force=True)


def test_build_argv():   # R1: flags first, then --, then positionals
    e = {"domain": "tmux", "verb": "type", "argv": ["target", "text", "--enter"]}
    assert catalogue.build_argv(e, {"target": "2", "text": "run the tests", "enter": True}) == \
        ["jv", "tmux", "type", "--enter", "--", "2", "run the tests"]
    assert catalogue.build_argv(e, {"target": "2", "text": "x", "enter": False}) == ["jv", "tmux", "type", "--", "2", "x"]
    h = {"domain": "zen", "verb": "close-unused", "argv": ["--hours"]}
    assert catalogue.build_argv(h, {"hours": 24}) == ["jv", "zen", "close-unused", "--hours", "24"]
    assert catalogue.build_argv(h, {}, "--json", "--yes") == ["jv", "zen", "close-unused", "--json", "--yes"]
    # live 2026-11-01: dotless verbs ran as `jv brightness brightness -- get` (rc 2); media had never run by voice
    b = {"name": "brightness", "domain": "brightness", "verb": "brightness", "argv": ["action", "n"]}
    assert catalogue.build_argv(b, {"action": "get"}, "--json") == ["jv", "brightness", "--json", "--", "get"]
    s = {"domain": "tmux", "verb": "select", "argv": ["window"]}
    assert catalogue.build_argv(s, {"window": "-x"}, "--json") == ["jv", "tmux", "select", "--json", "--", "-x"]


def test_tool_name():
    assert catalogue.tool_name({"domain": "zen", "verb": "close-unused"}) == "zen_close-unused"


def test_choices_and_slot_type():
    e = {"args": {"window": {"type": "enum", "choices": ["1", "web"]}}}
    assert catalogue.choices(e, "window") == ["1", "web"] and catalogue.choices(e, "x") == []
    assert catalogue.slot_type(e, "window") == "enum" and catalogue.slot_type(e, "x") is None


def test_run_jv_ok(env):
    script(env, {"wall next": {"rc": 0, "stdout": '{"ok":true}'}})
    assert asyncio.run(catalogue.run_jv(["jv", "wall", "next"])) == (0, '{"ok":true}', "")


def test_run_jv_timeout(env):
    script(env, {"wall next": {"rc": 0, "stdout": "{}", "sleep": 5}})
    assert asyncio.run(catalogue.run_jv(["jv", "wall", "next"], timeout=0.2)) == (1, "", "timed out")


def test_run_jv_missing(env, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    assert asyncio.run(catalogue.run_jv(["jv", "wall", "next"])) == (1, "", "jv not found")


def test_concurrent_cache_writes_unique_tmp(env):   # M3 review: brain + MCP server may write at once
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(4) as ex:
        assert all(ex.map(lambda _: "tmux.select" in catalogue.get(force=True), range(8)))
    d = env / "state/heardback"
    assert (d / "catalogue.json").exists() and not list(d.glob("*.tmp"))


def test_do_verbs_get_the_user_action_timeout():   # plan 05: foreground actions may run up to 60 s
    assert catalogue.jv_timeout(["jv", "do", "find-movie", "--json"], 10) == catalogue.DO_TIMEOUT_S > 60
    assert catalogue.jv_timeout(["jv", "tmux", "select"], 10) == 10
    assert catalogue.jv_timeout(["jv", "shot", "area", "--json"], 30) == catalogue.SHOT_TIMEOUT_S > 30
