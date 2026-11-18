"""Source-only regressions for retained music intent and terminal confirmation attempts.

All boundaries are fake; run only when media/confirmation verification is authorized.
"""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from heardback.brain import catalogue, router
from heardback.brain.brain import Awaiting, Brain, Turn, answer_for, correction
from jv import media, sysctl, zen
from jv.cli import catalogue as jv_catalogue


async def _until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


def _rig(monkeypatch):
    b = Brain(CONFIRM_TIMEOUT_S=0.05, CONFIRM_GRACE_S=0.05)
    b.router = router.Router(catalogue._index(jv_catalogue(media.VERBS + sysctl.VERBS + zen.VERBS)))
    messages = []
    async def send(conn, message):
        messages.append(message)
    monkeypatch.setattr(b, "_send", send)
    monkeypatch.setattr(catalogue, "get", lambda: b.router.cat)
    class Conn:
        def is_closing(self): return False
    monkeypatch.setenv("HEARDBACK_EXECUTION_OWNER", "music-test")
    b.voice = Conn()
    return b, b.voice, messages


def test_normal_result_revokes_attempt_before_reply_and_allows_next_attempt(env, monkeypatch):
    async def scenario():
        b, conn, messages = _rig(monkeypatch)
        turn = Turn(conn, "first", tid="result", sent=True)
        turn.execution_attempt = catalogue.set_current_turn(turn.tid, owner="music-test")
        b.turns.append(turn)
        await b._on_result(turn, {"subtype": "success", "result": "Done."})
        assert turn.finished and catalogue.execution_snapshot() == ("", "")
        assert sum(m["type"] == "done" for m in messages) == 1
        fresh = catalogue.set_current_turn("next", owner="music-test")
        catalogue.block_turn(turn.tid, turn.execution_attempt)
        assert catalogue.execution_snapshot() == ("next", fresh)
    asyncio.run(scenario())


def test_interrupt_revocation_failure_retires_captured_process_and_drains_once(env, monkeypatch):
    async def scenario():
        b, conn, messages = _rig(monkeypatch)
        killed, resets = [], []
        async def kill(): killed.append(True)
        proc = SimpleNamespace(kill=kill)
        b.claude = proc
        head, queued = Turn(conn, "old", tid="old", sent=True), Turn(conn, "queued")
        b.turns.extend((head, queued))
        monkeypatch.setattr(b, "_revoke_execution", lambda turn: False)
        async def reset(reason, old=None): resets.append((reason, old))
        monkeypatch.setattr(b, "reset", reset)
        await b.interrupt()
        assert head.interrupted and head.finished and not b.turns and b.claude is None
        assert killed == [True] and resets == [("execution-gate", proc)]
        assert messages == [{"type": "done"}, {"type": "done"}]
    asyncio.run(scenario())


def test_terminal_revocation_failure_preserves_clarification_and_correction(env, monkeypatch):
    async def scenario():
        b, conn, messages = _rig(monkeypatch)
        turn = Turn(conn, "old", tid="old", sent=True, confirm_reason="other",
                    acts=[dict(state="denied", reason="other", readback="Close?", answer="new request")])
        b.turns.append(turn)
        b._media_question(conn, {"clarification": "music_source", "say": "Spotify or YouTube?"}, owner=turn)
        b.awaiting[conn].expires = 0
        resets, corrections = [], []
        async def kill(): pass
        proc = SimpleNamespace(kill=kill)
        b.claude = proc
        monkeypatch.setattr(b, "_revoke_execution", lambda turn: False)
        async def reset(reason, old=None): resets.append((reason, old))
        async def to_claude(*args, **kwargs): corrections.append((args, kwargs))
        monkeypatch.setattr(b, "reset", reset)
        monkeypatch.setattr(b, "to_claude", to_claude)
        b._terminal(turn, "Spotify or YouTube?", expect_reply=True)
        assert b.claude is None
        await _until(lambda: bool(resets) and bool(corrections))
        assert turn.finished and not b.turns and b.awaiting[conn].expires > time.monotonic()
        assert messages == [{"type": "say", "text": "Spotify or YouTube?", "final": True,
                             "expect_reply": True, "turn": "old"}]
        # The mock follow-up owns completion; retirement sends no earlier done.
        assert resets == [("execution-gate", proc)]
        assert corrections[0][0][0] == 'heardback asked: "Close?". The user answered: "new request". Original request: "old".'
        assert corrections[0][1] == {"followup": True, "parent": "old"}
    asyncio.run(scenario())


def test_retired_process_drain_cannot_remove_later_turn_or_revoke_its_gate(env, monkeypatch):
    async def scenario():
        b, conn, messages = _rig(monkeypatch)
        async def kill(): pass
        old_proc, replacement = SimpleNamespace(kill=kill, pid=1), SimpleNamespace()
        old = Turn(conn, "old", tid="old", sent=True)
        later = Turn(conn, "later", tid="later", sent=True)
        later.execution_attempt = catalogue.set_current_turn(later.tid, owner="replacement")
        b.turns.append(later)
        b.claude = replacement
        await b._retire_execution(old, old_proc)
        assert b.claude is replacement and list(b.turns) == [later] and not later.finished
        assert catalogue.execution_snapshot() == ("later", later.execution_attempt)
        assert not messages
    asyncio.run(scenario())


def test_complete_play_music_spotify_repeated_confirmation_opened_only(env, monkeypatch):
    async def scenario():
        b, conn, messages = _rig(monkeypatch)
        revision = "media:site:" + "a" * 64 + ":" + "b" * 64
        calls = []
        async def run(argv, **kwargs):
            calls.append(argv)
            if "--intent" in argv:
                return 2, json.dumps({"clarification": "music_source", "say": "Where should I play music—Spotify or YouTube?"}), ""
            if "--yes" not in argv:
                return 3, json.dumps({"confirm": "Spotify isn't visible. Open a new tab?", "source_revision": revision}), ""
            return 0, json.dumps({"opened": True, "say": "Opened Spotify; pick something to play."}), ""
        monkeypatch.setattr(catalogue, "run_jv", run)
        await b.utterance("Play music", True, conn)
        assert len(calls) == 1 and messages[-2]["expect_reply"]
        await b.utterance("Spotify", True, conn, parent="question")
        await _until(lambda: conn in b.awaiting)
        question = b.awaiting[conn]
        original_expiry = question.expires
        for words in ("repeat please", "say that again"):
            previous = sum(m["type"] == "confirm" for m in messages)
            await b.handle({"type": "confirm_reply", "confirmation_id": question.id,
                            "ok": False, "text": words, "speaker_ok": True}, conn)
            await _until(lambda: sum(m["type"] == "confirm" for m in messages) > previous)
            assert b.awaiting[conn] is question and question.expires == original_expiry
        questions = [m for m in messages if m["type"] == "confirm"]
        assert len(questions) == 3 and len({m["confirmation_id"] for m in questions}) == 1
        assert len({m["text"] for m in questions}) == 1
        await b.handle({"type": "confirm_reply", "confirmation_id": "stale", "ok": True,
                        "text": "yes", "speaker_ok": True}, conn)
        assert len(calls) == 2 and conn in b.awaiting
        await b.handle({"type": "confirm_reply", "confirmation_id": question.id, "ok": True,
                        "text": "yes", "speaker_ok": True}, conn)
        await _until(lambda: len(calls) == 3)
        await _until(lambda: any(m.get("text") == "Opened Spotify; pick something to play." for m in messages))
        assert calls[-1].count("--yes") == 1 and revision in calls[-1]
        assert "--target" in calls[-1] and "spotify" in calls[-1]
        assert not any(m.get("text", "").lower().startswith("playing") for m in messages)
        assert conn not in b.awaiting
    asyncio.run(scenario())


def test_terminal_model_turn_suppresses_reask_and_preapproved_mcp_launch(env, monkeypatch):
    from heardback.brain import mcp_jv
    from brainutil import jv_calls
    async def scenario():
        b, conn, messages = _rig(monkeypatch)
        turn = Turn(conn, "open Spotify", tid="terminal", sent=True)
        b.turns.append(turn)
        catalogue.set_current_turn(turn.tid, owner="music-test")
        provenance = catalogue.execution_snapshot()
        permission = []
        async def respond(rid, result): permission.append(result)
        async def dry_run(*args): return 3, "Open Spotify in a new tab?", "zen:" + "b" * 64
        monkeypatch.setattr(b, "dry_run", dry_run)
        request = {"tool_name": "mcp__jv__zen_open", "input": {"query": "spotify"}}
        task = asyncio.create_task(b._can_use_tool(SimpleNamespace(respond=respond), turn, "p1", request))
        await _until(lambda: conn in b.awaiting)
        q = b.awaiting[conn]
        await b.handle({"type": "confirm_reply", "confirmation_id": q.id, "ok": False,
                        "text": "", "speaker_ok": True}, conn)
        await task
        assert turn.terminal and permission[-1]["behavior"] == "deny"
        monkeypatch.setenv("HEARDBACK_EXECUTION_GATE", "1")
        result = mcp_jv.call(b.router.cat, "zen_search", {"query": "spotify"},
                             expected_turn=provenance[0], expected_attempt=provenance[1])
        assert result["isError"] and not jv_calls(env)
        await b._on_event({"type": "stream_event", "event": {"type": "content_block_delta",
                          "delta": {"type": "text_delta", "text": "Should I open it again?"}}})
        await b._on_result(turn, {"subtype": "success", "result": "Should I open it again?"})
        spoken = [m for m in messages if m["type"] == "say"]
        assert [m["text"] for m in spoken] == ["I didn't hear an answer, so I didn't do it."]
        assert not any(m.get("expect_reply") for m in spoken)
        assert not b.awaiting and not b.turns
    asyncio.run(scenario())


def test_claude_music_clarification_is_retained_for_source_answer(env, monkeypatch):
    async def scenario():
        b, conn, messages = _rig(monkeypatch)
        turn = Turn(conn, "play some music please", tid="music-model", sent=True)
        turn.tool_ids["tool"] = "media_media"
        b.turns.append(turn)
        catalogue.set_current_turn(turn.tid, owner="music-test")
        await b._on_event({"type": "user", "message": {"content": [{"type": "tool_result",
                           "tool_use_id": "tool", "is_error": True, "content": json.dumps({
                           "clarification": "music_source", "say": "Spotify or YouTube?"})}]}})
        await b._on_result(turn, {"subtype": "success", "result": "I can just play a video?"})
        assert messages[-2]["text"] == "Spotify or YouTube?" and messages[-2]["expect_reply"]
        calls = []
        async def run(argv, **kwargs):
            calls.append(argv)
            return 0, '{"say":"Opened Spotify; pick something to play."}', ""
        monkeypatch.setattr(catalogue, "run_jv", run)
        await b.utterance("Spotify", True, conn, parent="question")
        assert calls[0] == ["jv", "media", "--target", "spotify", "--json", "--", "play"]
    asyncio.run(scenario())


@pytest.mark.parametrize("submission", ["unknown", "unverified", "not_attempted"])
def test_claude_web_ask_outcome_retires_model_retry_and_false_speech(env, monkeypatch, submission):
    from heardback.brain import mcp_jv
    from brainutil import jv_calls
    async def scenario():
        b, conn, messages = _rig(monkeypatch)
        turn = Turn(conn, "ask Claude.ai a question", tid="web-ask", sent=True)
        turn.tool_ids["ask"] = "zen_ask"
        b.turns.append(turn)
        catalogue.set_current_turn(turn.tid, owner="music-test")
        provenance = catalogue.execution_snapshot()
        sentence = "Typed in Claude.ai, but sending isn't confirmed. Check the chat before trying again."
        await b._on_event({"type": "user", "message": {"content": [{"type": "tool_result",
                           "tool_use_id": "ask", "content": json.dumps({"submission": submission, "say": sentence})}]}})
        assert turn.terminal == sentence
        monkeypatch.setenv("HEARDBACK_EXECUTION_GATE", "1")
        result = mcp_jv.call(b.router.cat, "zen_ask", {"text": "duplicate"},
                             expected_turn=provenance[0], expected_attempt=provenance[1])
        assert result["isError"] and not jv_calls(env)
        await b._on_result(turn, {"subtype": "success", "result": "Sent it. I'll send it again."})
        assert [m["text"] for m in messages if m["type"] == "say"] == [sentence]
    asyncio.run(scenario())


def test_repeat_does_not_extend_expiry_or_accept_late_yes(env, monkeypatch):
    async def scenario():
        b, _, messages = _rig(monkeypatch)
        class Conn:
            def is_closing(self): return False
        conn = Conn()
        task = asyncio.create_task(b.ask(conn, "Same question?", True))
        await _until(lambda: conn in b.awaiting)
        q = b.awaiting[conn]
        expiry = q.expires
        b._answer(conn, False, "repeat please")
        await _until(lambda: len(messages) == 2)
        assert q.expires == expiry
        q.expires = time.monotonic() - 1
        b._answer(conn, True, "yes")
        assert await task == (None, "") and conn not in b.awaiting
    asyncio.run(scenario())


@pytest.mark.parametrize("via_model", [False, True])
@pytest.mark.parametrize("answer,player", [("Zen", "firefox.instance_1"), ("MPD", "mpd"), ("use firefox.instance_1", "firefox.instance_1")])
def test_player_answer_retains_exact_candidate_and_original_action(env, monkeypatch, via_model, answer, player):
    async def scenario():
        b, conn, messages = _rig(monkeypatch)
        question = {"ok": False, "clarification": "media_player", "action": "previous", "target": None,
                    "candidates": ["firefox.instance_1", "mpd"],
                    "player_labels": {"firefox.instance_1": "Zen", "mpd": "mpd"}, "say": "Which player—Zen or mpd?"}
        calls = []
        async def run(argv, **kwargs):
            calls.append(argv)
            return 0, '{"say":"previous: Selected song"}', ""
        monkeypatch.setattr(catalogue, "run_jv", run)
        if via_model:
            turn = Turn(conn, "previous song please", tid="choose-player", sent=True)
            turn.tool_ids["skip"] = "media_media"
            b.turns.append(turn)
            catalogue.set_current_turn(turn.tid, owner="music-test")
            await b._on_event({"type": "user", "message": {"content": [{"type": "tool_result",
                              "tool_use_id": "skip", "is_error": True, "content": json.dumps(question)}]}})
            await b._on_result(turn, {"subtype": "success", "result": "I cannot skip in Zen."})
        else:
            await b._speak_jv(router.Route("media", {"action": "previous"}, 1), 2, json.dumps(question), "", conn, "previous song")
        assert messages[-2]["text"] == question["say"] and messages[-2]["expect_reply"]
        await b.utterance(answer, True, conn, parent="question")
        assert calls == [["jv", "media", "--player", player, "--json", "--", "previous"]]
        assert conn not in b.awaiting and messages[-2]["text"] == "previous: Selected song"
    asyncio.run(scenario())


def test_ambiguous_zen_answer_never_selects_first_browser(env, monkeypatch):
    async def scenario():
        b, conn, messages = _rig(monkeypatch)
        candidates = ["firefox.instance_1", "firefox.instance_2"]
        question = {"clarification": "media_player", "action": "next", "target": "zen", "candidates": candidates,
                    "player_labels": dict.fromkeys(candidates, "Zen"), "say": "Which browser player?"}
        b._media_question(conn, question)
        assert "zen" not in b.awaiting[conn].options
        b.to_claude = AsyncMock()
        calls = []
        async def run(argv, **kwargs):
            calls.append(argv)
            return 0, '{"say":"next: Selected song"}', ""
        monkeypatch.setattr(catalogue, "run_jv", run)
        await b.utterance("Zen", True, conn, "ambiguous", "question")
        assert not calls and conn not in b.awaiting
        b.to_claude.assert_awaited_once_with("Zen", conn, "ambiguous", followup=False, parent="question")
        b._media_question(conn, question)
        await b.utterance(candidates[1], True, conn, parent="question")
        assert calls == [["jv", "media", "--target", "zen", "--player", candidates[1], "--json", "--", "next"]]
    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["expired", "untrusted", "interrupt", "cancel", "new topic"])
def test_ended_player_question_cannot_dispatch_a_late_answer(env, monkeypatch, ending):
    async def scenario():
        b, conn, _ = _rig(monkeypatch)
        b._media_question(conn, {"clarification": "media_player", "action": "next", "candidates": ["mpd"],
                                "player_labels": {"mpd": "mpd"}, "say": "Which player?"})
        calls = []
        async def run(argv, **kwargs):
            calls.append(argv)
            return 0, '{"say":"Unexpected dispatch"}', ""
        monkeypatch.setattr(catalogue, "run_jv", run)
        if ending == "expired":
            b.awaiting[conn].expires = time.monotonic() - 1
        elif ending == "untrusted":
            await b.utterance("MPD", False, conn, parent="question")
        elif ending == "interrupt":
            await b.interrupt()
        else:
            await b.utterance(ending, True, conn, parent="question")
        await b.utterance("MPD", True, conn, parent="question")
        assert not calls and conn not in b.awaiting
    asyncio.run(scenario())


def test_failed_model_media_attempt_blocks_retry_but_fresh_play_next_dispatches(env, monkeypatch):
    from heardback.brain import mcp_jv
    from brainutil import jv_calls
    async def scenario():
        b, conn, messages = _rig(monkeypatch)
        turn = Turn(conn, "play Spotify", tid="failed-play", sent=True)
        turn.tool_ids["play"] = "media_media"
        b.turns.append(turn)
        catalogue.set_current_turn(turn.tid, owner="music-test")
        provenance = catalogue.execution_snapshot()
        sentence = "Spotify accepted the command, but I couldn't verify playback."
        await b._on_event({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "play",
                          "is_error": True, "content": json.dumps({"ok": False, "dispatched": True, "say": sentence})}]}})
        monkeypatch.setenv("HEARDBACK_EXECUTION_GATE", "1")
        blocked = mcp_jv.call(b.router.cat, "media_media", {"action": "next"},
                              expected_turn=provenance[0], expected_attempt=provenance[1])
        assert blocked["isError"] and not jv_calls(env)
        await b._on_result(turn, {"subtype": "success", "result": "I won't retry next."})
        assert [m["text"] for m in messages if m["type"] == "say"] == [sentence]
        calls = []
        async def run(argv, **kwargs):
            calls.append(argv)
            return 0, '{"say":"next: Ban Ja Rani"}', ""
        monkeypatch.setattr(catalogue, "run_jv", run)
        await b.utterance("Play next", True, conn)
        assert calls == [["jv", "media", "--json", "--", "next"]]
        assert messages[-2]["text"] == "next: Ban Ja Rani" and not b.awaiting
    asyncio.run(scenario())


@pytest.mark.parametrize("via_model", [False, True])
@pytest.mark.parametrize("source", ["desktop", "site"])
def test_player_followup_retains_explicit_spotify_source_and_intent(env, monkeypatch, via_model, source):
    async def scenario():
        b, conn, _ = _rig(monkeypatch)
        question = {"clarification": "media_player", "action": "previous", "target": "spotify", "source": source,
                    "intent": "resume", "candidates": ["spotify.instance123", "spotify.instance456"],
                    "player_labels": {"spotify.instance123": "spotify", "spotify.instance456": "spotify"}}
        if via_model:
            turn = Turn(conn, "previous on Spotify", tid="source-player", sent=True)
            turn.tool_ids["choose"] = "media_media"
            b.turns.append(turn)
            await b._on_event({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "choose",
                               "is_error": True, "content": json.dumps(question)}]}})
        else:
            b._media_question(conn, question)
        calls = []
        async def run(argv, **kwargs):
            calls.append(argv)
            return 0, '{"say":"previous: Selected song"}', ""
        monkeypatch.setattr(catalogue, "run_jv", run)
        await b.utterance("spotify.instance456", True, conn, parent="question")
        assert calls == [["jv", "media", "--target", "spotify", "--source", source, "--player", "spotify.instance456",
                          "--intent", "resume", "--json", "--", "previous"]]
    asyncio.run(scenario())


@pytest.mark.parametrize("answer,source", [("Spotify app", "desktop"), ("desktop Spotify", "desktop"),
                                          ("Spotify in Zen", "site"), ("Spotify website", "site"),
                                          ("Spotify site", "site")])
def test_source_answer_retains_app_site_choice(env, monkeypatch, answer, source):
    async def scenario():
        b, conn, _ = _rig(monkeypatch)
        b._media_question(conn, {"clarification": "music_source", "action": "play", "say": "Which source?"})
        calls = []
        async def run(argv, **kwargs):
            calls.append(argv)
            return 0, '{"say":"Selected source"}', ""
        monkeypatch.setattr(catalogue, "run_jv", run)
        await b.utterance(answer, True, conn, parent="question")
        assert calls == [["jv", "media", "--target", "spotify", "--source", source, "--json", "--", "play"]]
    asyncio.run(scenario())


def test_model_media_preflight_retains_terminal_player_question(env, monkeypatch):
    async def scenario():
        b, conn, _ = _rig(monkeypatch)
        turn = Turn(conn, "previous on Spotify app", tid="preflight-choice", sent=True)
        b.turns.append(turn)
        question = {"clarification": "media_player", "action": "previous", "target": "spotify", "source": "desktop",
                    "candidates": ["spotify.instance123", "spotify.instance456"],
                    "player_labels": {"spotify.instance123": "spotify", "spotify.instance456": "spotify"},
                    "say": "Which Spotify player?"}
        async def run(argv, **kwargs):
            assert "--dry-run" in argv
            return 2, json.dumps(question), ""
        monkeypatch.setattr(catalogue, "run_jv", run)
        responses = []
        async def respond(rid, response):
            responses.append(response)
        proc = SimpleNamespace(respond=respond)
        b.claude = proc
        media.duck_path().write_text("spotify.instance123\n")
        await b._can_use_tool(proc, turn, "choice", {"tool_name": "mcp__jv__media_media",
                              "input": {"action": "previous", "target": "spotify", "source": "desktop"}})
        assert responses[0]["behavior"] == "deny"
        assert turn.terminal == question["say"] and turn.terminal_expect_reply
        aw = b.awaiting[conn]
        assert aw.owner is turn and aw.original == turn.text
        assert aw.options["spotify instance456"].args["source"] == "desktop"
        assert not media.duck_path().exists()
        b.run_route = AsyncMock()
        await b.utterance("spotify.instance456", True, conn, "answer", turn.tid)
        b.run_route.assert_awaited_once_with(aw.options["spotify instance456"], conn, "spotify.instance456", "answer")
        assert list(b.turns) == [turn] and not b.awaiting
    asyncio.run(scenario())


@pytest.mark.parametrize("text,expected", [("firefox", "route"), ("use Firefox!", "route"),
                                          ("pick option firefox", "new"), ("volume up", "new"),
                                          ("YouTube", "model"), ("stop", "new"), ("start over", "new")] +
                         [(f"{lead} firefox", "route") for lead in
                          ("on", "in", "choose", "pick", "option", "answer", "press", "say")])
def test_media_answer_matching_is_pure(text, expected):
    route = router.Route("media", {"action": "next", "player": "firefox"}, 1.0)
    aw = Awaiting(None, True, "id", "Which player?", 123, kind="choice", source="media",
                  options={router.normalise(k): v for k, v in
                           {"firefox": route, "youtube": None, "stop": route, "start over": route}.items()})
    before = vars(aw).copy()
    assert answer_for(aw, text) == (route if expected == "route" else expected)
    assert vars(aw) == before
    aw.kind = "confirm"
    assert answer_for(aw, text) == "new"


def test_exact_player_name_wins_over_another_players_label(env, monkeypatch):
    async def scenario():
        b, conn, _ = _rig(monkeypatch)
        b._media_question(conn, {"clarification": "media_player", "action": "next",
                                "candidates": ["firefox", "mpd"],
                                "player_labels": {"firefox": "Zen", "mpd": "firefox"}})
        aw = b.awaiting[conn]
        assert aw.kind == "choice" and aw.source == "media"
        assert answer_for(aw, "use firefox").args["player"] == "firefox"
        assert answer_for(aw, "Zen").args["player"] == "firefox"
        assert answer_for(aw, "mpd").args["player"] == "mpd"
    asyncio.run(scenario())


def test_player_names_that_normalise_the_same_do_not_select_one(env, monkeypatch):
    async def scenario():
        b, conn, _ = _rig(monkeypatch)
        b._media_question(conn, {"clarification": "media_player", "action": "next",
                                "candidates": ["firefox.instance_1", "firefox_instance_1"],
                                "player_labels": {"firefox.instance_1": "First", "firefox_instance_1": "Second"}})
        aw = b.awaiting[conn]
        assert answer_for(aw, "firefox.instance_1") == "new"
        assert answer_for(aw, "First").args["player"] == "firefox.instance_1"
    asyncio.run(scenario())


@pytest.mark.parametrize("via_model", [False, True])
def test_youtube_sends_one_model_followup_with_question_and_original(env, monkeypatch, via_model):
    async def scenario():
        b, conn, _ = _rig(monkeypatch)
        original, question = "play some music please", "Spotify or YouTube?"
        data = {"clarification": "music_source", "say": question}
        if via_model:
            turn = Turn(conn, original, tid="question", sent=True)
            turn.tool_ids["tool"] = "media_media"
            b.turns.append(turn)
            await b._on_event({"type": "user", "message": {"content": [{"type": "tool_result",
                              "tool_use_id": "tool", "is_error": True, "content": json.dumps(data)}]}})
            await b._on_result(turn, {"subtype": "success", "result": ""})
        else:
            await b._speak_jv(router.Route("media", {"action": "play", "intent": "music"}, 1),
                              2, json.dumps(data), "", conn, original, "question")
        aw = b.awaiting[conn]
        assert aw.options["youtube"] is None and aw.original == original
        aw.expires = 0
        await b.finish(aw.owner)
        assert time.monotonic() + b.AWAIT_S - 1 < aw.expires <= time.monotonic() + b.AWAIT_S
        b.to_claude, b.run_route = AsyncMock(), AsyncMock()
        monkeypatch.setattr(b.router, "route", lambda text: pytest.fail("YouTube answer must go to the model"))
        await b.utterance("on YouTube", True, conn, "answer", "question")
        b.to_claude.assert_awaited_once_with(correction(question, "on YouTube", original), conn, "answer",
                                           followup=True, parent="question")
        b.run_route.assert_not_awaited()
        assert not b.awaiting
    asyncio.run(scenario())


@pytest.mark.parametrize("ending", ["miss", "expired", "new wake"])
def test_media_miss_expiry_and_new_wake_use_normal_routing(env, monkeypatch, ending):
    async def scenario():
        b, conn, _ = _rig(monkeypatch)
        b._media_question(conn, {"clarification": "music_source"})
        if ending == "expired":
            b.awaiting[conn].expires = time.monotonic() - 1
        b.run_route, b.to_claude = AsyncMock(), AsyncMock()
        route = router.Route("volume", {"action": "up"}, 1.0)
        text = "volume up" if ending == "miss" else "Spotify website"
        parent = None if ending == "new wake" else "question"
        await b.utterance(text, True, conn, "answer", parent)
        assert not b.awaiting
        if ending == "miss":
            b.run_route.assert_awaited_once_with(route, conn, text, "answer")
            b.to_claude.assert_not_awaited()
        else:
            b.to_claude.assert_awaited_once_with(text, conn, "answer", followup=False, parent=parent)
            b.run_route.assert_not_awaited()
    asyncio.run(scenario())
