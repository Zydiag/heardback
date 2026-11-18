"""Tasks 20-21: reset phrases, recent-actions prefix, follow-up window."""
import json
import re

import pytest

from brainutil import (FAKE, control_requests, jv_calls, last_user_message, pids, run_brain, says, script, user_messages, utt)


def test_recent_actions_prefix(env):
    run_brain(env, [utt("next wallpaper"), utt("undo that")], claude_cmd=FAKE)
    line = last_user_message(env)
    assert re.fullmatch(r"\[recent actions: \d\d:\d\d wall next → ok\]\nundo that", line)


def test_prefix_args_and_failure(env):
    script(env, {"tmux select": {"rc": 1, "stderr": "nope"}})
    run_brain(env, [utt("next wallpaper"), utt("window two"), utt("why")], claude_cmd=FAKE)
    assert re.fullmatch(r"\[recent actions: \d\d:\d\d wall next → ok; \d\d:\d\d tmux select 2 → failed\]\nwhy",
                        last_user_message(env))


def test_prefix_not_repeated(env):
    run_brain(env, [utt("next wallpaper"), utt("undo that"), utt("and again")], claude_cmd=FAKE)
    assert user_messages(env)[-1] == "and again"


def test_reset_phrase(env):
    out = run_brain(env, [utt("hello"), utt("start over"), utt("again")], claude_cmd=FAKE, gap=0.3)
    assert says(out) == ["You said: hello.", "Okay, fresh start.", "You said: again."]
    p = pids(env)
    assert len(p) == 2 and p[0] != p[1]
    assert "reset reason=user" in (env / "state/heardback/brain.log").read_text()


def test_reset_phrase_without_claude(env):
    assert run_brain(env, [utt("forget that")]) == [{"type": "say", "text": "Okay, fresh start.", "final": True},
                                                    {"type": "done"}]


def test_followup_window(env):
    out = run_brain(env, [utt("cue:ask"), {**utt("switch tmux window 3"), "parent": "question"}, utt("next wallpaper")], claude_cmd=FAKE,
                    gap=0.3)   # the reply comes after the question was heard
    assert says(out) == ["Which one?", "You said: switch tmux window 3.", "Done."]
    assert [c[:2] for c in jv_calls(env) if c[0] != "list"] == [["wall", "next"]]   # fuzzy went to Claude


def test_question_says_expect_reply(env):
    out = run_brain(env, [utt("cue:ask"), utt("next wallpaper")], claude_cmd=FAKE, gap=0.3)
    sayings = [m for m in out if m["type"] == "say"]
    assert sayings[0].pop("turn").endswith("-b1")
    assert sayings[0] == {"type": "say", "text": "Which one?", "final": True, "expect_reply": True}
    assert "expect_reply" not in sayings[1]          # fast path never asks for a follow-up


def test_streamed_question_still_expects_reply(env):
    sayings = [m for m in run_brain(env, [utt("cue:asknl")], claude_cmd=FAKE) if m["type"] == "say"]
    assert len({s.pop("turn") for s in sayings}) == 1
    assert sayings == [{"type": "say", "text": "Which one?", "final": False},
                       {"type": "say", "text": "", "final": True, "expect_reply": True}]


def test_question_before_last_sentence_expects_reply(env):
    sayings = [m for m in run_brain(env, [utt("cue:asklate")], claude_cmd=FAKE) if m["type"] == "say"]
    assert sayings[-1]["expect_reply"] is True and sayings[-1]["text"] == "Tell me the name and I'll send it."


def test_fuzzy_phrase_goes_to_claude(env):
    out = run_brain(env, [utt("hello"), utt("switch tmux window 3")], claude_cmd=FAKE)
    assert says(out)[-1] == "You said: switch tmux window 3."
    assert [c[:2] for c in jv_calls(env) if c[0] != "list"] == []


def test_all_phrases_use_haiku(env):
    phrases = ["hello", "think about my week", "use opus for this", "think hard about it",
               "compare tea and coffee", "explain how this works in detail", "walk me through a plan"]
    out = run_brain(env, [utt(text) for text in phrases], claude_cmd=FAKE)
    assert says(out) == [f"You said: {text}." for text in phrases]
    assert control_requests(env) == []
    events = [json.loads(line) for line in (env / "state/heardback/trace.jsonl").read_text().splitlines()]
    assert [e["model"] for e in events if e["ev"] == "result"] == ["haiku"] * len(phrases)
    assert json.loads((env / "state/heardback/usage.json").read_text())["by_model"] == {"haiku": len(phrases)}


@pytest.mark.parametrize("reported,expected", [("claude-sonnet-5-5", "sonnet"), ("none", None)])
def test_reported_model_is_logged_without_gating_reply(env, monkeypatch, reported, expected):
    monkeypatch.setenv("FAKE_CLAUDE_ASSISTANT_MODEL", reported)
    out = run_brain(env, [utt("hello")], claude_cmd=FAKE)
    assert says(out) == ["You said: hello."]
    assert control_requests(env) == []
    events = [json.loads(line) for line in (env / "state/heardback/trace.jsonl").read_text().splitlines()]
    assert [e["model"] for e in events if e["ev"] == "result"] == [expected]
