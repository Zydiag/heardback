"""Playback speaks local jv output while Claude receives no page data."""
import json
from pathlib import Path

import pytest

from brainutil import FAKE, jv_calls, run_brain, says, script, utt
from heardback.brain import catalogue, mcp_jv, play_result
from heardback.brain.brain import Brain, NOT_DONE
from jv import cli, media, zen
from jv.verb import runtime_path
from test_injectgate import READ, responses


TITLE = "Pokemon Journey Episode 8"
URL = "https://www.youtube.com/watch?v=secret"
SAYS = ["playing " + TITLE, zen.BLOCKED_SAY.replace("Opened it", "Opened " + TITLE)]


def test_tainted_conversation_refuses_video_without_playback_fallback(env, monkeypatch):
    cat = env / "play-catalogue.json"
    cat.write_text(json.dumps(cli.catalogue(cli.all_verbs())))
    monkeypatch.setenv("FAKE_JV_CATALOGUE", str(cat))
    out = run_brain(env, [READ, utt('cue:tool mcp__jv__zen_play {"query":"episode 9"}')],
                    claude_cmd=FAKE)
    assert responses(env)[-1] == {"behavior": "deny", "message": NOT_DONE["tainted"]}
    assert says(out)[-1] == NOT_DONE["tainted"]
    assert "I couldn't confirm the video started." not in says(out)
    assert not any(c[:2] == ["zen", "play"] for c in jv_calls(env))


def test_two_fast_background_plays_keep_modifier_and_clear_slots(env, monkeypatch):
    cat = env / "play-catalogue.json"
    cat.write_text(json.dumps(cli.catalogue(zen.VERBS)))
    monkeypatch.setenv("FAKE_JV_CATALOGUE", str(cat))
    script(env, {"zen play": {"stdout": json.dumps({"ok": True, "say": "playing lofi"})}})
    b = Brain()
    out = run_brain(env, [utt("play lofi on youtube in the background"),
                          utt("play lofi in background on youtube")], brain=b)
    assert says(out) == ["playing lofi", "playing lofi"]
    assert [c for c in jv_calls(env) if c[:2] == ["zen", "play"]] == [
        ["zen", "play", "--background", "--json", "--", "lofi"]] * 2
    assert b.claude is None and b._tainted is None and not b.awaiting


@pytest.mark.parametrize("say", SAYS)
@pytest.mark.parametrize("ok", [False, True])
@pytest.mark.parametrize("tool,command,args", [("zen_play", "zen play", {"query": "episode 8"}),
                                              ("media_media", "media --json", {"action": "play"})])
def test_mcp_play_hides_all_page_data_and_keeps_speech_once(env, say, ok, tool, command, args):
    data = {"ok": ok, "say": say, "title": TITLE, "url": URL, "error": TITLE + URL,
            "artist": "Untrusted artist", "before": {"title": TITLE}, "candidates": [{"title": TITLE, "url": URL}]}
    if tool == "media_media":
        data["observed"] = ok
    script(env, {command: {"rc": 0 if ok else 1, "stdout": json.dumps(data)}})
    cat = catalogue._index(cli.catalogue(zen.VERBS + media.VERBS))
    result = mcp_jv.call(cat, tool, args,
                         expected_turn="turn", expected_attempt="attempt")
    text = result["content"][0]["text"]
    assert TITLE not in text and URL not in text and "title" not in text and "url" not in text and "artist" not in text
    visible = json.loads(text)
    assert set(visible) == {"ok", "say", "receipt"} | ({"observed"} if tool == "media_media" else set())
    assert visible["ok"] is ok
    if tool == "media_media":
        assert visible["observed"] is ok
    assert result["isError"] is not ok
    path = Path(runtime_path("heardback-play-" + visible["receipt"]))
    assert path.stat().st_mode & 0o777 == 0o600
    assert play_result.take(visible["receipt"], "turn", "attempt") == say
    assert play_result.take(visible["receipt"], "turn", "attempt") is None
    assert not path.exists()


@pytest.mark.parametrize("raw", [TITLE + URL, "[]", "null", '{"ok":true}'])
def test_malformed_play_output_fails_closed(env, raw):
    script(env, {"zen play": {"rc": 0, "stdout": raw}})
    result = mcp_jv.call(catalogue._index(cli.catalogue(zen.VERBS)), "zen_play", {"query": "episode 8"})
    assert result["isError"] == (raw != '{"ok":true}')
    assert json.loads(result["content"][0]["text"]) == {
        "ok": raw == '{"ok":true}', "say": "opened the video" if raw == '{"ok":true}' else "couldn't start the video"}


def test_receipt_write_failure_never_exposes_page_data(env, monkeypatch):
    script(env, {"zen play": {"stdout": json.dumps({"ok": True, "say": SAYS[0], "title": TITLE, "url": URL})}})
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(play_result, "save", fail)
    result = mcp_jv.call(catalogue._index(cli.catalogue(zen.VERBS)), "zen_play", {"query": "episode 8"})
    assert not result["isError"]
    assert json.loads(result["content"][0]["text"]) == {"ok": True, "say": "opened the video"}


@pytest.mark.parametrize("receipt", ["../escape", None, "a" * 32])
def test_missing_or_invalid_receipt_is_ignored(env, receipt):
    assert play_result.take(receipt, "turn", "attempt") is None


@pytest.mark.parametrize("turn,attempt", [("old", "attempt"), ("turn", "old")])
def test_receipt_cannot_cross_turn_or_execution_attempt(env, turn, attempt):
    receipt = play_result.save(SAYS[0], "turn", "attempt")
    assert play_result.take(receipt, turn, attempt) is None


@pytest.mark.parametrize("say", SAYS)
def test_two_plays_in_one_conversation_are_allowed_and_speak_title_once_each(env, monkeypatch, say):
    cat = env / "play-catalogue.json"
    cat.write_text(json.dumps(cli.catalogue(cli.all_verbs())))
    monkeypatch.setenv("FAKE_JV_CATALOGUE", str(cat))
    monkeypatch.setenv("FAKE_CLAUDE_MCP", "1")
    script(env, {"zen play": {"stdout": json.dumps({"ok": True, "say": say, "title": TITLE, "url": URL})}})
    out = run_brain(env, [utt('cue:tool mcp__jv__zen_play ' + json.dumps({"query": q}))
                          for q in ("episode 8", "episode 9")], claude_cmd=FAKE)
    assert [r["behavior"] for r in responses(env)] == ["allow", "allow"]
    assert says(out) == [say, say]
    executions = [r["execution"] for line in (env / "claude.log").read_text().splitlines()
                  if "execution" in (r := json.loads(line))]
    assert len(executions) == 2 and all(not r["isError"] for r in executions)
    for result in executions:
        text = result["content"][0]["text"]
        assert TITLE not in text and URL not in text
        assert json.loads(text)["say"] == "opened the video"
    assert not list(Path(runtime_path(".")).glob("heardback-play-*"))


def test_media_then_video_in_one_conversation_stays_clean_and_speaks_local_titles(env, monkeypatch):
    cat = env / "play-catalogue.json"
    cat.write_text(json.dumps(cli.catalogue(cli.all_verbs())))
    monkeypatch.setenv("FAKE_JV_CATALOGUE", str(cat))
    monkeypatch.setenv("FAKE_CLAUDE_MCP", "1")
    script(env, {"media --intent --dry-run": {"stdout": json.dumps({"ok": True, "source_revision": "media:desktop:" + "a" * 64})},
                 "media --intent": {"stdout": json.dumps({"ok": True, "observed": True, "say": "playing Sailor Song",
                                                      "title": "Sailor Song", "artist": "Gigi Perez", "url": URL})},
                 "zen play": {"stdout": json.dumps({"ok": True, "say": SAYS[0], "title": TITLE, "url": URL})}})
    out = run_brain(env, [utt('cue:tool mcp__jv__media_media {"action":"play","intent":"music"}'),
                          utt('cue:tool mcp__jv__zen_play {"query":"episode 9"}')], claude_cmd=FAKE)
    assert [r["behavior"] for r in responses(env)] == ["allow", "allow"]
    assert says(out) == ["playing Sailor Song", SAYS[0]]
    executions = [r["execution"] for line in (env / "claude.log").read_text().splitlines()
                  if "execution" in (r := json.loads(line))]
    assert len(executions) == 2 and all(not r["isError"] for r in executions)
    assert all(word not in json.dumps(executions) for word in ("Sailor Song", "Gigi Perez", TITLE, URL))
    assert not list(Path(runtime_path(".")).glob("heardback-play-*"))


@pytest.mark.parametrize("clarification", ["music_source", "media_player"])
def test_mcp_media_preserves_local_clarification_and_private_question(env, clarification):
    question = {"clarification": clarification, "candidates": ["spotify.instance123", "spotify.instance456"],
                "player_labels": {"spotify.instance123": "spotify", "spotify.instance456": "spotify"},
                "source": "desktop", "action": "previous", "target": "spotify", "intent": "resume"}
    script(env, {"media --json": {"rc": 2, "stdout": json.dumps({**question, "ok": False,
                 "say": "Which player should I use?", "title": TITLE, "artist": "Untrusted artist", "url": URL})}})
    result = mcp_jv.call(catalogue._index(cli.catalogue(media.VERBS)), "media_media", {"action": "previous"},
                         expected_turn="turn", expected_attempt="attempt")
    visible = json.loads(result["content"][0]["text"])
    assert result["isError"]
    assert visible == {**question, "ok": False, "say": "couldn't verify the media action", "receipt": visible["receipt"]}
    assert play_result.take(visible["receipt"], "turn", "attempt") == "Which player should I use?"


@pytest.mark.parametrize("raw", [TITLE + URL, "[]", "null", '{"ok":true}'])
def test_malformed_media_output_hides_metadata(env, raw):
    script(env, {"media --json": {"stdout": raw}})
    result = mcp_jv.call(catalogue._index(cli.catalogue(media.VERBS)), "media_media", {"action": "status"})
    ok = raw == '{"ok":true}'
    assert result["isError"] is not ok
    visible = json.loads(result["content"][0]["text"])
    if ok:
        assert play_result.take(visible.pop("receipt"), "", "") == "I couldn't confirm that."
    assert visible == {"ok": ok, "say": "I couldn't confirm that." if ok else "couldn't verify the media action"}
