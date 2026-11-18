"""Task 15: spoken confirm for Claude's jv tools (can_use_tool) and fast-path confirm verbs."""
import asyncio
import json

import pytest

from brainutil import FAKE, _inputs, jv_calls, run_brain, says, script, serving, talk, utt, voice_connection
from heardback.brain.brain import Awaiting, Brain, sock_path

CLOSE_UNUSED = 'cue:tool mcp__jv__zen_close-unused {"hours":24}'
YES = {"type": "confirm_reply", "ok": True, "text": "yes", "speaker_ok": True}
QUICK = {"CONFIRM_TIMEOUT_S": 0.2, "CONFIRM_GRACE_S": 0}


def _confirm_close_unused(env, sentence="Close 3 tabs: A, B, C?"):
    script(env, {"zen close-unused --dry-run": {"rc": 3, "stdout": json.dumps({"ok": False, "confirm": sentence})}})


def _responses(env) -> list[dict]:
    return [m["response"]["response"] for _, m in _inputs(env) if m.get("type") == "control_response"]


def test_claude_confirm_yes(env):
    _confirm_close_unused(env)
    out = run_brain(env, [utt(CLOSE_UNUSED)], claude_cmd=FAKE, replies={"confirm": YES})
    assert any(m.get("type") == "confirm" and m["text"] == "Close 3 tabs: A, B, C?" and m["timeout_s"] == 8 and m["confirmation_id"] for m in out)
    assert "allowed." in says(out)
    assert jv_calls(env)[-1] == ["zen", "close-unused", "--hours", "24", "--dry-run", "--json"]   # R1
    assert _responses(env) == [{"behavior": "allow", "updatedInput": {"hours": 24}}]


def test_claude_clipboard_permission_carries_source_revision(env):
    revision = "clipboard:" + "a" * 64
    script(env, {"tmux type --dry-run": {"rc": 3, "stdout": json.dumps({
        "confirm": "Paste 2 lines into window 2?", "source_revision": revision})}})
    args = {"target": "2", "clipboard": True}
    out = run_brain(env, [utt("cue:tool mcp__jv__tmux_type " + json.dumps(args))], claude_cmd=FAKE,
                    replies={"confirm": YES})
    assert _responses(env) == [{"behavior": "allow", "updatedInput": {**args, "source_revision": revision}}]


def test_claude_unprompted_clipboard_dispatch_carries_source_revision(env):
    revision = "clipboard:" + "d" * 64
    script(env, {"tmux type --dry-run": {"rc": 0, "stdout": json.dumps({"source_revision": revision})}})
    args = {"target": "2", "clipboard": True}
    out = run_brain(env, [utt("cue:tool mcp__jv__tmux_type " + json.dumps(args))], claude_cmd=FAKE)
    assert _responses(env) == [{"behavior": "allow", "updatedInput": {**args, "source_revision": revision}}]


def test_fast_clipboard_confirmation_carries_source_revision(env):
    revision = "clipboard:" + "b" * 64
    script(env, {"tmux type": {"rc": 3, "stdout": json.dumps({
        "confirm": "Paste text into window 2?", "source_revision": revision})},
        "tmux type --yes": {"rc": 0, "stdout": '{"ok":true,"say":"Pasted."}'}})
    out = run_brain(env, [utt("paste my clipboard into window 2")], replies={"confirm": YES})
    assert "Pasted." in says(out)
    assert jv_calls(env)[-1] == ["tmux", "type", "--clipboard", "--source-revision", revision,
                                 "--json", "--yes", "--", "2"]


def _browser_catalogue(env, monkeypatch):
    from brainutil import FIX
    from jv import media, zen
    from jv.cli import catalogue
    data = json.loads((FIX / "catalogue.json").read_text())
    data["verbs"] += catalogue([v for v in zen.VERBS + media.VERBS if v.name in ("zen.open", "media")])["verbs"]
    path = env / "browser-catalogue.json"
    path.write_text(json.dumps(data))
    monkeypatch.setenv("FAKE_JV_CATALOGUE", str(path))


def test_fast_zen_open_confirmation_carries_target_revision(env, monkeypatch):
    _browser_catalogue(env, monkeypatch)
    revision = "zen:" + "a" * 64
    script(env, {"zen open": {"rc": 3, "stdout": json.dumps({
        "confirm": "Other Zen workspaces may be unseen. Open Gmail in a new tab anyway?", "source_revision": revision})},
        "zen open --yes": {"rc": 0, "stdout": '{"ok":true,"say":"Opened Gmail."}'}})
    out = run_brain(env, [utt("open site gmail")], replies={"confirm": YES})
    assert "Opened Gmail." in says(out)
    assert jv_calls(env)[-1] == ["zen", "open", "--source-revision", revision, "--json", "--yes", "--", "gmail"]


def test_claude_zen_permission_preserves_target_revision(env, monkeypatch):
    _browser_catalogue(env, monkeypatch)
    revision = "zen:" + "b" * 64
    script(env, {"zen open --dry-run": {"rc": 3, "stdout": json.dumps({
        "confirm": "Open Gmail in a new tab anyway?", "source_revision": revision})}})
    run_brain(env, [utt('cue:tool mcp__jv__zen_open {"query":"gmail"}')], claude_cmd=FAKE, replies={"confirm": YES})
    assert _responses(env) == [{"behavior": "allow", "updatedInput": {"query": "gmail", "source_revision": revision}}]


def test_fast_background_confirmation_preserves_mode_and_revision(env, monkeypatch):
    _browser_catalogue(env, monkeypatch)
    revision = "zen:" + "d" * 64
    script(env, {"zen open": {"rc": 3, "stdout": json.dumps({
        "confirm": "Gmail isn't visible. Open a new tab?", "source_revision": revision})},
        "zen open --yes": {"rc": 0, "stdout": '{"ok":true,"say":"Opened Gmail in the background."}'}})
    out = run_brain(env, [utt("open site gmail in the background")], replies={"confirm": YES})
    assert "Opened Gmail in the background." in says(out)
    assert jv_calls(env)[-1] == ["zen", "open", "--source-revision", revision, "--background",
                                "--json", "--yes", "--", "gmail"]


def test_model_background_confirmation_preserves_mode_and_revision(env, monkeypatch):
    _browser_catalogue(env, monkeypatch)
    revision = "zen:" + "e" * 64
    script(env, {"zen open --dry-run": {"rc": 3, "stdout": json.dumps({
        "confirm": "Open Gmail in a new tab?", "source_revision": revision})}})
    run_brain(env, [utt('cue:tool mcp__jv__zen_open {"query":"gmail","background":true}')],
              claude_cmd=FAKE, replies={"confirm": YES})
    assert _responses(env) == [{"behavior": "allow", "updatedInput": {
        "query": "gmail", "background": True, "source_revision": revision}}]


def test_claude_zen_permission_without_bound_target_is_denied(env, monkeypatch):
    _browser_catalogue(env, monkeypatch)
    script(env, {"zen open --dry-run": {"rc": 3, "stdout": '{"confirm":"Open anyway?"}'}})
    out = run_brain(env, [utt('cue:tool mcp__jv__zen_open {"query":"gmail"}')], claude_cmd=FAKE, replies={"confirm": YES})
    assert _responses(env)[0]["behavior"] == "deny" and not any(m.get("type") == "confirm" for m in out)


def test_missing_spotify_confirmation_reaches_fast_voice_flow(env, monkeypatch):
    _browser_catalogue(env, monkeypatch)
    revision = "media:site:" + "c" * 64 + ":" + "d" * 64
    script(env, {"media --target": {"rc": 3, "stdout": json.dumps({
        "confirm": "Open Spotify in a new tab anyway?", "source_revision": revision})},
        "media --target --yes": {"rc": 0, "stdout": '{"ok":true,"observed":false,"say":"Opened Spotify; pick something to play."}'}})
    out = run_brain(env, [utt("play spotify")], replies={"confirm": YES})
    assert says(out) == ["I couldn't confirm that."]
    assert "--source-revision" in jv_calls(env)[-1] and revision in jv_calls(env)[-1]


def test_confirm_reply_without_speaker_denied(env):
    _confirm_close_unused(env)
    out = run_brain(env, [utt(CLOSE_UNUSED)], claude_cmd=FAKE, replies={"confirm": {**YES, "speaker_ok": False}})
    assert "Okay, not doing it." in says(out)


def test_confirm_reply_missing_speaker_uses_turn(env):   # speaker check off: falls back to the turn's value
    _confirm_close_unused(env)
    reply = {k: v for k, v in YES.items() if k != "speaker_ok"}
    assert "allowed." in says(run_brain(env, [utt(CLOSE_UNUSED)], claude_cmd=FAKE, replies={"confirm": reply}))


def test_confirm_reply_no_denied(env):
    _confirm_close_unused(env)
    out = run_brain(env, [utt(CLOSE_UNUSED)], claude_cmd=FAKE, replies={"confirm": {**YES, "ok": False}})
    assert "Okay, not doing it." in says(out)


def test_confirm_timeout_is_no_answer(env):   # live 2026-10-31: a timeout reached Claude as "User said no."
    _confirm_close_unused(env)
    out = run_brain(env, [utt(CLOSE_UNUSED)], claude_cmd=FAKE, **QUICK)
    assert "I didn't hear an answer, so I didn't do it." in says(out)


def test_confirm_nothing_heard_is_no_answer(env):   # voice timed out / STT empty: ok false, no text
    _confirm_close_unused(env)
    out = run_brain(env, [utt(CLOSE_UNUSED)], claude_cmd=FAKE, replies={"confirm": {**YES, "ok": False, "text": ""}})
    assert "I didn't hear an answer, so I didn't do it." in says(out)


def test_confirm_spoken_no_is_said_no(env):
    _confirm_close_unused(env)
    out = run_brain(env, [utt(CLOSE_UNUSED)], claude_cmd=FAKE, replies={"confirm": {**YES, "ok": False, "text": "no"}})
    assert "Okay, not doing it." in says(out)


def test_confirm_other_answer_goes_to_claude(env):   # friction #33: "open it in Claude folder" was counted as no
    _confirm_close_unused(env)
    reply = {**YES, "ok": False, "text": "Open it in Claude folder"}
    out = run_brain(env, [utt(CLOSE_UNUSED)], claude_cmd=FAKE, replies={"confirm": reply})
    assert "I didn't do that. I'll take your answer as a new request." in says(out)
    assert _denied(env) == ["denied tool=zen_close-unused rc=3 reason='answered' args={\"hours\": 24}"]
    assert _responses(env) == [{"behavior": "deny", "message":
                               "The user answered with other words; their answer comes next."}]


def test_confirm_other_answer_untrusted_is_no(env):   # words from a failed speaker check never reach Claude
    _confirm_close_unused(env)
    reply = {**YES, "ok": False, "text": "open it in claude folder", "speaker_ok": False}
    assert "Okay, not doing it." in says(run_brain(env, [utt(CLOSE_UNUSED)], claude_cmd=FAKE,
                                                         replies={"confirm": reply}))


def test_typed_yes_answers_confirm(env):
    _confirm_close_unused(env)
    out = run_brain(env, [utt(CLOSE_UNUSED)], claude_cmd=FAKE, replies={"confirm": utt("yeah")})
    assert "allowed." in says(out) and [m["type"] for m in out].count("done") == 1


def test_typed_other_text_denies(env):
    _confirm_close_unused(env)
    out = run_brain(env, [utt(CLOSE_UNUSED)], claude_cmd=FAKE, replies={"confirm": utt("nope")})
    assert "Okay, not doing it." in says(out)


def test_typed_other_text_is_answered(env):
    _confirm_close_unused(env)
    out = run_brain(env, [utt(CLOSE_UNUSED)], claude_cmd=FAKE, replies={"confirm": utt("only the old ones")})
    assert "I didn't do that. I'll take your answer as a new request." in says(out)


def test_other_connection_cannot_answer(env):   # Review Focus 1
    _confirm_close_unused(env)

    brain = Brain(claude_cmd=FAKE, CONFIRM_TIMEOUT_S=0.4, CONFIRM_GRACE_S=0)

    def b_pending():
        return bool(brain.awaiting)

    async def go():
        async with serving(brain, sock_path()):
            a = asyncio.create_task(talk(sock_path(), [utt(CLOSE_UNUSED)]))
            async with asyncio.timeout(3):   # until A's confirm is pending
                while not b_pending():
                    await asyncio.sleep(0.01)
            b = await talk(sock_path(), [utt("yes")], voice=False)
            return await a, b
    a, b = asyncio.run(go())
    assert [m for m in a if m["type"] == "confirm"] and "I didn't hear an answer, so I didn't do it." in says(a)
    assert says(b) == ["Actions need an authenticated voice request."] and not [m for m in b if m["type"] == "confirm"]


def test_disconnect_denies(env):   # Review Focus 2
    _confirm_close_unused(env)

    async def go():
        async with serving(Brain(claude_cmd=FAKE), sock_path()):
            r, w = await voice_connection(sock_path())
            w.write((json.dumps(utt(CLOSE_UNUSED)) + "\n").encode())
            await w.drain()
            async with asyncio.timeout(3):
                while json.loads(await r.readline())["type"] != "confirm":
                    pass
            w.close()
            return await talk(sock_path(), [utt("hello")], timeout=3)   # well before the 10 s confirm timeout
    out = asyncio.run(go())
    assert says(out) == ["You said: hello."]
    assert _responses(env) == [{"behavior": "deny", "message": "User said no."}]


def test_dry_run_ok_allows_without_asking(env):   # auto verb with nothing to confirm
    script(env, {"zen close --dry-run": {"rc": 0, "stdout": '{"ok":true}'}})
    out = run_brain(env, [utt('cue:tool mcp__jv__zen_close {"query":"foo"}')], claude_cmd=FAKE)
    assert not [m for m in out if m["type"] == "confirm"] and "allowed." in says(out)
    assert jv_calls(env)[-1] == ["zen", "close", "--dry-run", "--json", "--", "foo"]


def test_dry_run_failure_denies_with_reason(env):   # Review Focus 4
    script(env, {"zen close --dry-run": {"rc": 1, "stderr": "no tab matches 'foo'"}})
    out = run_brain(env, [utt('cue:tool mcp__jv__zen_close {"query":"foo"}')], claude_cmd=FAKE)
    assert not [m for m in out if m["type"] == "confirm"] and "denied: no tab matches 'foo'." in says(out)


def test_dry_run_ambiguous_denies_with_candidates(env):
    script(env, {"zen close --dry-run": {"rc": 2, "stdout": '{"ok":false,"candidates":["Docs","Mail"]}'}})
    out = run_brain(env, [utt('cue:tool mcp__jv__zen_close {"query":"foo"}')], claude_cmd=FAKE)
    assert not [m for m in out if m["type"] == "confirm"] and "denied: which one: Docs, Mail?." in says(out)


def test_unknown_tool_denied(env):
    out = run_brain(env, [utt("cue:tool mcp__jv__nope {}")], claude_cmd=FAKE)
    assert "denied: heardback refused that tool call.." in says(out) and all(c[0] == "list" for c in jv_calls(env))


def test_hidden_verb_denied(env):   # voice:false verbs are not in the brain's catalogue
    out = run_brain(env, [utt("cue:tool mcp__jv__zen_install {}")], claude_cmd=FAKE)
    assert "denied: heardback refused that tool call.." in says(out)


def test_long_read_back_capped(env):   # R6
    _confirm_close_unused(env, "Close these tabs: " + "Some Long Tab Title, " * 20 + "and more?")
    out = run_brain(env, [utt(CLOSE_UNUSED)], claude_cmd=FAKE, replies={"confirm": YES})
    (c,) = [m for m in out if m["type"] == "confirm"]
    assert len(c["text"]) <= 200 and c["text"].startswith("Close these tabs: Some Long Tab Title,")


def test_fast_path_confirm(env):
    script(env, {"zen close-unused": {"rc": 3, "stdout": '{"confirm":"Close 7 tabs unused for a day?"}'},
                 "zen close-unused --yes": {"rc": 0, "stdout": '{"ok":true,"say":"Closed 7 tabs."}'}})
    out = run_brain(env, [utt("close unused tabs")], replies={"confirm": utt("yes")})
    assert any(m.get("type") == "confirm" and m["text"] == "Close 7 tabs unused for a day?" for m in out)
    assert says(out) == ["Closed 7 tabs."] and out[-1] == {"type": "done"}
    assert jv_calls(env)[-1] == ["zen", "close-unused", "--json", "--yes"]


@pytest.mark.parametrize("answer", ["Yes, close it.", "no, window 4"])
def test_fast_path_close_other_is_a_correction(env, monkeypatch, answer):
    from heardback.brain import catalogue
    from jv.cli import all_verbs, catalogue as live
    cat = {v["name"]: v for v in live(all_verbs())["verbs"] if v.get("voice", True)}   # the fixture has no tmux.close
    monkeypatch.setattr(catalogue, "get", lambda *a, **k: cat)
    script(env, {"tmux close": {"rc": 3, "stdout": '{"confirm":"Close window 3 webapp?"}'},
                 "tmux close --yes": {"rc": 0, "stdout": '{"ok":true,"say":"Closed window 3."}'}})
    out = run_brain(env, [utt("close window 3")], claude_cmd=FAKE, replies={"confirm": utt(answer)})
    assert not any("--yes" in c for c in jv_calls(env))
    from brainutil import user_messages
    (message,) = user_messages(env)
    assert 'heardback asked: "Close window 3 webapp?"' in message
    assert f'The user answered: "{answer}"' in message
    assert 'Original request: "close window 3"' in message
    assert says(out)[0] == "I didn't do that. I'll take your answer as a new request."
    assert [m["type"] for m in out].count("done") == 1 and out[-1] == {"type": "done"}
    assert [c[:2] for c in jv_calls(env)].count(["tmux", "close"]) == 1


def test_fast_path_confirm_reply_no(env):
    script(env, {"zen close-unused": {"rc": 3, "stdout": '{"confirm":"Close 7 tabs?"}'}})
    out = run_brain(env, [utt("close unused tabs")], replies={"confirm": {**YES, "ok": False, "text": "no"}})
    assert says(out) == ["Okay, not doing it."]
    assert not any("--yes" in c for c in jv_calls(env))


def test_fast_path_confirm_other_words_are_a_new_request(env):   # sessionhelp 5a, like Claude's path (#33)
    script(env, {"zen close-unused": {"rc": 3, "stdout": '{"confirm":"Close 7 tabs?"}'}})
    out = run_brain(env, [utt("close unused tabs")], claude_cmd=FAKE, replies={"confirm": {**YES, "ok": False, "text": "next wallpaper"}})
    assert "Okay, not doing it." not in says(out) and out[-1] == {"type": "done"}
    from brainutil import user_messages
    assert 'The user answered: "next wallpaper"' in user_messages(env)[-1]
    assert not any("--yes" in c for c in jv_calls(env))


def test_readback_other_when_claude_off(env):
    from heardback.brain.brain import OFF
    script(env, {"zen close-unused": {"rc": 3, "stdout": '{"confirm":"Close 7 tabs?"}'}})
    out = run_brain(env, [utt("close unused tabs")], claude_cmd=None,
                    replies={"confirm": {**YES, "ok": False, "text": "no, window 4"}})
    assert says(out) == ["I didn't do that. I'll take your answer as a new request.", OFF]
    assert [m["type"] for m in out] == ["confirm", "say", "say", "done"]


def test_fast_path_confirm_no_answer(env):
    script(env, {"zen close-unused": {"rc": 3, "stdout": '{"confirm":"Close 7 tabs?"}'}})
    out = run_brain(env, [utt("close unused tabs")], replies={"confirm": {**YES, "ok": False, "text": ""}})
    assert says(out) == ["I didn't hear an answer, so I didn't do it."]
    assert not any("--yes" in c for c in jv_calls(env))


def test_yes_without_pending_goes_to_claude(env):
    assert says(run_brain(env, [utt("yes")], claude_cmd=FAKE)) == ["You said: yes."]


def _denied(env) -> list[str]:
    return [l.split(" ", 1)[1] for l in (env / "state/heardback/brain.log").read_text().splitlines() if " denied " in l]


def test_denied_logged_with_dry_run_reason(env):   # tmuxtype: jv's reason used to reach Claude only
    script(env, {"tmux type --dry-run": {"rc": 1, "stderr": "no window first window"}})
    text = "voice typing is working now and this is a long sentence"
    run_brain(env, [utt('cue:tool mcp__jv__tmux_type ' + json.dumps({"target": "first window", "text": text}))],
              claude_cmd=FAKE)
    assert _denied(env) == ["denied tool=tmux_type rc=1 reason='no window first window' "
                            f'args={{"target": "first window", "text": "{text[:40]}"}}']


def test_denied_logged_said_no_and_timeout(env):
    _confirm_close_unused(env)
    run_brain(env, [utt(CLOSE_UNUSED)], claude_cmd=FAKE, replies={"confirm": {**YES, "ok": False}})
    run_brain(env, [utt(CLOSE_UNUSED)], claude_cmd=FAKE, **QUICK)
    run_brain(env, [utt("cue:tool mcp__jv__nope {}")], claude_cmd=FAKE)
    assert _denied(env) == ["denied tool=zen_close-unused rc=3 reason='said-no' args={\"hours\": 24}",
                            "denied tool=zen_close-unused rc=3 reason='timeout' args={\"hours\": 24}",
                            "denied tool=nope rc=None reason='not-allowed' args={}"]


def test_allowed_not_logged_as_denied(env):
    _confirm_close_unused(env)
    run_brain(env, [utt(CLOSE_UNUSED)], claude_cmd=FAKE, replies={"confirm": YES})
    assert _denied(env) == []


def test_brain_outwaits_readback_plus_listen():   # live 2026-10-31: brain denied at 10 s while voice still listened
    from heardback.brain import brain as b
    assert b.CONFIRM_TIMEOUT_S + b.CONFIRM_GRACE_S >= 30


def test_parallel_questions_wait_for_separate_yeses(env, monkeypatch):
    class Connection:
        def is_closing(self):
            return False

    async def go():
        b, conn, sent = Brain(), Connection(), []
        async def send(conn, msg):
            sent.append(msg)
        monkeypatch.setattr(b, "_send", send)
        first = asyncio.create_task(b.ask(conn, "First action?", True))
        await asyncio.sleep(0)
        question = b.awaiting[conn]
        second = asyncio.create_task(b.ask(conn, "Second action?", True))
        await asyncio.sleep(0)
        assert b.awaiting[conn] is question and not first.done() and not second.done()
        assert [m["text"] for m in sent] == ["First action?"]
        b._answer(conn, True, "yes")
        assert await first == (True, "yes")
        await asyncio.sleep(0)
        assert b.awaiting[conn].id != question.id and not second.done()
        assert [m["text"] for m in sent] == ["First action?", "Second action?"]
        b._answer(conn, True, "yes")
        assert await second == (True, "yes")
        assert not b.awaiting
    asyncio.run(go())


@pytest.mark.parametrize("unasked", [False, True])
def test_confirm_reply_ignores_choice_slot(env, monkeypatch, unasked):
    from heardback.brain import brain
    events = []
    monkeypatch.setattr(brain.trace, "emit", lambda *args, **kw: events.append((args, kw)))

    async def go():
        b, conn = Brain(), object()
        b.voice = conn
        future = asyncio.get_running_loop().create_future()
        question = Awaiting(future, True, "choice-id", "Which one?",
                            asyncio.get_running_loop().time() + 60, kind="choice", source="model")
        b.awaiting[conn] = question
        await b.handle({**YES, "confirmation_id": question.id, "unasked": unasked}, conn)
        assert b.awaiting[conn] is question and not future.done() and not question.replay.is_set()
        (args, event), = events
        assert args[:2] == ("brain", "confirm_reply")
        assert event["waiting"] and event["awaiting_kind"] == "choice"
    asyncio.run(go())


@pytest.mark.parametrize("confirmation_id", [None, "stale"])
def test_wrong_confirmation_id_cannot_answer(env, monkeypatch, confirmation_id):
    from unittest.mock import AsyncMock, Mock

    async def go():
        b, conn = Brain(), Mock()
        conn.is_closing.return_value = False
        b.voice = conn
        monkeypatch.setattr(b, "_send", AsyncMock())
        task = asyncio.create_task(b.ask(conn, "Close tabs?", True))
        await asyncio.sleep(0)
        question = b.awaiting[conn]
        assert question.kind == "confirm" and question.source == "readback"
        await b.handle({**YES, "confirmation_id": confirmation_id}, conn)
        assert b.awaiting[conn] is question and not question.future.done() and not task.done()
        await b.handle({**YES, "confirmation_id": question.id}, conn)
        assert await asyncio.wait_for(task, 0.2) == (True, "yes")
        assert not b.awaiting
    asyncio.run(go())


def test_slow_client_yes_after_timeout_s_still_allowed(env):   # voice spends ~3 s on the read-back before listening
    _confirm_close_unused(env)

    async def go():
        async with serving(Brain(claude_cmd=FAKE, CONFIRM_TIMEOUT_S=0.2, CONFIRM_GRACE_S=2), sock_path()):
            r, w = await voice_connection(sock_path())
            try:
                w.write((json.dumps(utt(CLOSE_UNUSED)) + "\n").encode())
                await w.drain()
                out = []
                async with asyncio.timeout(5):
                    while line := await r.readline():
                        out.append(m := json.loads(line))
                        if m["type"] == "confirm":
                            await asyncio.sleep(0.8)   # past timeout_s, inside the grace
                            w.write((json.dumps({**YES, "confirmation_id": m["confirmation_id"]}) + "\n").encode())
                            await w.drain()
                        if m["type"] == "done":
                            return out
            finally:
                w.close()
                await w.wait_closed()
    assert "allowed." in says(asyncio.run(go()))


# --- sessionhelp 2: a reply to a waiting alert answers the dialog, through tmux.answer's read-back ----------
@pytest.mark.parametrize("said, choice, no", [
    ("2", "2", "3"), ("yes", "1", "3"), ("no", "3", "3"), ("yeah", "1", "3"),
    ("option 1", "1", "3"), ("answer 2", "2", "3"), ("press 2", "2", "3"),
    ("say 2", "2", "3"), ("choose 2", "2", "3"), ("pick 2", "2", "3"),
    ("no", "esc", "esc"),
])
@pytest.mark.parametrize("parent", [None, "alert"])
def test_alert_reply_answers_with_readback(env, monkeypatch, said, choice, no, parent):
    from jv.cli import all_verbs, catalogue
    cat = {"version": 1, "verbs": [v for v in catalogue(all_verbs())["verbs"] if v["name"] == "tmux.answer"]}
    (env / "cat.json").write_text(json.dumps(cat))
    monkeypatch.setenv("FAKE_JV_CATALOGUE", str(env / "cat.json"))
    script(env, {"tmux answer": {"rc": 3, "stdout": json.dumps({"ok": False, "confirm": "Answer 'Yes' in window 3?"})},
                 "tmux answer --yes": {"rc": 0, "stdout": json.dumps({"ok": True, "say": "answered"})}})
    b = Brain()
    handle = b.handle

    async def announce_on_connect(msg, conn):
        await handle(msg, conn)
        if msg.get("type") == "hello":
            await b.announce("Permission?", ask={"pane": "%3", "no": no, "choices": ["1", "2", "3"]})
            question = b.awaiting[conn]
            assert question.kind == "choice" and question.source == "alert"

    monkeypatch.setattr(b, "handle", announce_on_connect)
    out = run_brain(env, [{**utt(said), "parent": parent}], brain=b, replies={"confirm": YES})
    assert any(m.get("expect_reply") for m in out)
    assert any(m.get("type") == "confirm" and m["text"] == "Answer 'Yes' in window 3?" for m in out)
    last = jv_calls(env)[-1]
    assert last[:2] == ["tmux", "answer"] and {"%3", choice, "--yes"} <= set(last)


def test_two_alert_replies_without_parent_still_need_readback(env, monkeypatch):
    from jv.cli import all_verbs, catalogue
    cat = {"verbs": [v for v in catalogue(all_verbs())["verbs"] if v["name"] == "tmux.answer"]}
    path = env / "cat.json"
    path.write_text(json.dumps(cat))
    monkeypatch.setenv("FAKE_JV_CATALOGUE", str(path))
    script(env, {"tmux answer": {"rc": 3, "stdout": json.dumps({"confirm": "Answer yes in window 3?"})}})
    async def go():
        b = Brain()
        async with serving(b, sock_path()):
            r, w = await voice_connection(sock_path())
            try:
                while b.voice is None:
                    await asyncio.sleep(.01)
                for turn in ("first", "second"):
                    await b.announce("Permission?", ask={"pane": "%3", "no": "2", "choices": ["1", "2"]})
                    alert = json.loads(await r.readline())
                    assert alert["expect_reply"] and b.awaiting[b.voice].source == "alert"
                    w.write((json.dumps({**utt("yes"), "turn": turn}) + "\n").encode())
                    await w.drain()
                    confirm = json.loads(await r.readline())
                    assert confirm["type"] == "confirm" and confirm["text"] == "Answer yes in window 3?"
                    assert b.awaiting[b.voice].kind == "confirm"
                    w.write((json.dumps({"type": "confirm_reply", "ok": False, "text": "no",
                                         "speaker_ok": True, "confirmation_id": confirm["confirmation_id"]}) + "\n").encode())
                    await w.drain()
                    reply = json.loads(await r.readline())
                    assert reply["type"] == "say" and reply["text"] == "Okay, not doing it."
                    assert json.loads(await r.readline())["type"] == "done"
                    assert b.voice not in b.awaiting
                actions = [c for c in jv_calls(env) if c[:2] == ["tmux", "answer"]]
                assert len(actions) == 2 and all("--yes" not in c for c in actions)
                assert b.claude is None
            finally:
                w.close()
                await w.wait_closed()
    asyncio.run(asyncio.wait_for(go(), 5))


@pytest.mark.parametrize("kind", ["choice", "confirm"])
def test_alert_preserves_open_slot(env, monkeypatch, kind):
    from unittest.mock import AsyncMock
    from event_voice import Voice
    from heardback.brain import catalogue, router

    async def go():
        b, conn = Brain(), Voice()
        b.voice = conn
        b.router = router.Router(catalogue.get())
        monkeypatch.setattr(b, "_send", AsyncMock())
        monkeypatch.setattr(b, "to_claude", AsyncMock())
        monkeypatch.setattr(b, "_kick", AsyncMock())
        question = Awaiting(asyncio.get_running_loop().create_future(), True, "question", "Which one?",
                            asyncio.get_running_loop().time() + 30, kind=kind, source="model")
        b._set_awaiting(conn, question)
        await b.announce("Permission?", ask={"pane": "%3", "no": "2", "choices": ["1", "2"]})
        assert b.awaiting[conn] is question
        b._send.assert_not_awaited()
        assert b.events.rows == [{"kind": "done", "text": "Permission?",
                                  "ask": {"pane": "%3", "no": "2", "choices": ["1", "2"]}}]
        if kind == "choice":
            await b.utterance("explain that bit", True, conn, parent="question-turn")
            b.to_claude.assert_awaited_once_with("explain that bit", conn, b.to_claude.call_args.args[2],
                                               followup=True, parent="question-turn")
    asyncio.run(go())


@pytest.mark.parametrize("reply, parent, expired", [
    ("press 3", "alert", False), ("press 3", None, False), ("2", None, True), ("2", "alert", True),
])
def test_alert_miss_clears_slot_and_routes_normally(env, monkeypatch, reply, parent, expired):
    from unittest.mock import AsyncMock
    from event_voice import Voice
    from heardback.brain import catalogue, router
    from heardback.brain.brain import answer_for

    async def go():
        b, conn = Brain(), Voice()
        b.voice = conn
        b.router = router.Router(catalogue.get())
        monkeypatch.setattr(b, "_send", AsyncMock())
        monkeypatch.setattr(b, "to_claude", AsyncMock())
        monkeypatch.setattr(b, "_kick", AsyncMock())
        monkeypatch.setattr(b, "run_route", AsyncMock())
        await b.announce("Permission?", ask={"pane": "%3", "no": "2", "choices": ["1", "2"]})
        question = b.awaiting[conn]
        assert answer_for(question, "press 3") == "new"
        if expired:
            question.expires = 0
        await b.utterance(reply, True, conn, parent=parent)
        assert conn not in b.awaiting
        b.run_route.assert_not_awaited()
        # A one-digit new request is ignored; other words take the normal model path.
        assert b.to_claude.await_count == (1 if reply == "press 3" else 0)
    asyncio.run(go())


def test_claude_native_media_permission_carries_rc0_target_revision(env, monkeypatch):
    _browser_catalogue(env, monkeypatch)
    revision = "media:desktop:" + "a" * 64
    script(env, {"media --target --dry-run": {"rc": 0, "stdout": json.dumps({"source_revision": revision})}})
    args = {"action": "play", "target": "spotify", "source": "desktop"}
    out = run_brain(env, [utt("cue:tool mcp__jv__media_media " + json.dumps(args))], claude_cmd=FAKE)
    assert _responses(env) == [{"behavior": "allow", "updatedInput": {**args, "source_revision": revision}}]
    assert not any(m.get("type") == "confirm" for m in out)


@pytest.mark.parametrize("revision", [None, "zen:" + "a" * 64, "media:desktop:bad"])
def test_claude_media_permission_requires_valid_rc0_binding(env, monkeypatch, revision):
    _browser_catalogue(env, monkeypatch)
    script(env, {"media --target --dry-run": {"rc": 0, "stdout": json.dumps({"source_revision": revision})}})
    run_brain(env, [utt('cue:tool mcp__jv__media_media {"action":"play","target":"spotify"}')], claude_cmd=FAKE)
    assert _responses(env)[0]["behavior"] == "deny"
