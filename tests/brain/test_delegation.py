"""Task 23: delegation ("tell the X claude to …") and "ask in claude" flows, pinned against the fixture catalogue."""
from brainutil import FAKE, jv_calls, load_fixture, run_brain, says, script, user_messages, utt
from heardback.brain.brain import Brain


def test_ask_in_claude_fast_path(env):
    script(env, {"zen ask": {"rc": 0, "stdout": '{"ok":true,"say":"asked claude"}'}})
    out = run_brain(env, [utt("ask claude why is the sky blue")], claude_cmd=FAKE)
    assert says(out) == ["asked claude"]
    assert jv_calls(env)[-1] == ["zen", "ask", "--json", "--", "why is the sky blue"]   # R1 argv order
    assert not user_messages(env)   # no Claude turn spent


def test_ask_named_session_goes_to_claude(env):
    run_brain(env, [utt("ask the webapp claude to run the tests")], claude_cmd=FAKE)
    assert user_messages(env)[-1].endswith("ask the webapp claude to run the tests")


def test_delegation_confirm_readback(env):
    script(env, {"tmux type --dry-run": {"rc": 3, "stdout": '{"ok":false,"confirm":"Send \'run the tests\' to webapp?"}'}})
    out = run_brain(env, [utt('cue:tool mcp__jv__tmux_type {"target":"%12","text":"run the tests","enter":true}')],
                    claude_cmd=FAKE, replies={"confirm": {"type": "confirm_reply", "ok": True, "text": "yes", "speaker_ok": True}})
    question, = [m for m in out if m["type"] == "confirm"]
    assert question["text"] == "Send 'run the tests' to webapp?" and question["timeout_s"] == 8
    assert question["confirmation_id"] and question["remaining_s"] > 0
    assert "allowed." in says(out)


def test_delegation_no_types_nothing(env):
    script(env, {"tmux type --dry-run": {"rc": 3, "stdout": '{"ok":false,"confirm":"Send \'hi\' to scratch?"}'}})
    out = run_brain(env, [utt('cue:tool mcp__jv__tmux_type {"target":"%12","text":"hi","enter":true}')],
                    claude_cmd=FAKE, replies={"confirm": {"type": "confirm_reply", "ok": False, "text": "no", "speaker_ok": True}})
    assert says(out) == ["Okay, not doing it."]
    assert [c for c in jv_calls(env) if c[:2] == ["tmux", "type"]] == \
        [["tmux", "type", "--enter", "--dry-run", "--json", "--", "%12", "hi"]]   # only the dry run


def test_every_tool_uses_the_callback():
    assert Brain._allowed(load_fixture()) == []
