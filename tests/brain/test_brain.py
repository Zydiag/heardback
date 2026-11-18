import asyncio
import json
import socket

import pytest

from brainutil import FAKE, HELLO, jv_calls, run_brain, script, serving, talk, utt
from heardback.brain import brain

PLACEHOLDER = "Claude is off; simple commands still work."   # no claude_cmd


def said(out):
    return [m["text"] for m in out if m["type"] == "say"]


def test_fast_path_speaks_say(env):
    script(env, {"tmux select": {"rc": 0, "stdout": '{"ok":true,"say":"Window 2."}'}})
    out = run_brain(env, [utt("switch to tmux window two")])
    assert out[0].pop("turn")
    assert out == [{"type": "say", "text": "Window 2.", "final": True}, {"type": "done"}]
    assert jv_calls(env)[-1] == ["tmux", "select", "--json", "--", "2"]   # R1 order
    log = (env / "state/heardback/brain.log").read_text()
    assert "route=fast verb=tmux.select" in log and "text='switch to tmux window two'" in log


def test_missing_say_is_done(env):
    script(env, {"wall next": {"rc": 0, "stdout": '{"ok":true}'}})
    assert said(run_brain(env, [utt("next wallpaper")])) == ["Done."]


def test_failure_speaks_reason(env):
    script(env, {"tmux select": {"rc": 1, "stdout": "", "stderr": "no window 9\nmore"}})
    assert said(run_brain(env, [utt("window two")])) == ["No window 9."]


def test_miss_speaks_closest(env):
    script(env, {"tmux select": {"rc": 1, "stderr": "x",
                                 "stdout": '{"ok":false,"error":"no match for \'wbe\'","closest":["web","notes"]}'}})
    assert said(run_brain(env, [utt("window two")])) == ["No match for 'wbe'. Closest, web, notes."]


def test_failure_speaks_jv_say_not_the_error(env):   # playfix: technical detail stays in `error`
    script(env, {"tmux select": {"rc": 1, "stdout": '{"ok":false,"error":"opened the YouTube results for lofi but '
                                 'couldn\'t start a video: no search results on this page","diag":"watch_links=0",'
                                 '"say":"I opened the YouTube results for lofi, but couldn\'t start a video."}'}})
    assert said(run_brain(env, [utt("window two")])) == [
        "I opened the YouTube results for lofi, but couldn't start a video."]


@pytest.mark.parametrize("data,err", [
    ({}, "no window 9"),
    ({"error": "no match for 'wbe'", "closest": ["web", "notes"]}, ""),
    ({"error": "opened the YouTube results for X but couldn't start a video: no search results on this page"}, ""),
    ({"error": "couldn't type into claude: composer not found"}, ""),
    ({"error": "zen bridge not connected (is Zen open? run jv zen ping)"}, ""),
    ({"error": "x: y", "say": "I couldn't find result 3 on this page."}, ""),
])
def test_spoken_failure_is_one_plain_sentence(data, err):   # playfix: TTS pauses at colons; one "couldn't"
    line = brain.spoken(1, data, err)
    assert ":" not in line and line.lower().count("couldn't") <= 1 and line.endswith((".", "?"))


@pytest.mark.parametrize("reason,want", [
    ("no Claude Code session in window 3", "No Claude Code session in window 3."),
    ("couldn't type into Claude: composer not found", "Couldn't type into Claude, composer not found."),
    ("window 3 is unavailable", "Couldn't window 3 is unavailable."),
])
def test_spoken_failure_prefix(reason, want):
    assert brain.spoken(1, {"error": reason}, "") == want


def test_ambiguous_goes_to_claude(env):
    script(env, {"tmux select": {"rc": 2, "stdout": '{"ok":false,"error":"ambiguous","candidates":["a","b"]}'}})
    assert said(run_brain(env, [utt("window two")])) == [PLACEHOLDER]


def test_ambiguous_browser_candidates_use_labels_without_urls():
    data = {"candidates": [{"label": "YouTube, tab 4", "title": "A long title", "tabId": 4,
                            "url": "https://youtube.com/watch?v=secret"},
                           {"label": "YouTube, tab 9", "url": "https://youtube.com/results?search_query=secret"}]}
    assert brain._reason(2, data, "") == "which one: YouTube, tab 4, YouTube, tab 9?"


def test_needs_confirm_unanswered(env):   # Task 15 replaced the M1 placeholder
    script(env, {"tmux select": {"rc": 3, "stdout": '{"ok":false,"confirm":"Really?"}'}})
    out = run_brain(env, [utt("window two")], CONFIRM_TIMEOUT_S=0.05, CONFIRM_GRACE_S=0)
    question, = [m for m in out if m["type"] == "confirm"]
    assert question["text"] == "Really?" and question["timeout_s"] == 0.05
    assert question["confirmation_id"] and 0 < question["remaining_s"] <= 0.1
    assert said(out) == ["I didn't hear an answer, so I didn't do it."]


def test_unrouted_goes_to_claude(env):
    out = run_brain(env, [utt("tell the webapp claude to run the tests")])
    assert said(out) == [PLACEHOLDER] and out[-1] == {"type": "done"}
    assert jv_calls(env) == [["list", "--json"]]
    assert "route=claude verb=- model=haiku" in (env / "state/heardback/brain.log").read_text()


def test_jv_hang_times_out(env):   # Review Focus 3
    script(env, {"wall next": {"rc": 0, "stdout": "{}", "sleep": 5}})
    out = run_brain(env, [utt("next wallpaper")], JV_TIMEOUT_S=0.3)
    assert out[0]["text"] == "Couldn't timed out."


def test_recent_records_calls(env):
    script(env, {"tmux select": {"rc": 1, "stderr": "nope"}})
    b = brain.Brain()
    run_brain(env, [utt("next wallpaper")], brain=b)
    run_brain(env, [utt("window two")], brain=b)
    assert [(v, a, ok) for _, v, a, ok in b.recent] == [("wall.next", {}, True), ("tmux.select", {"window": "2"}, False)]


def test_unknown_override_rejected():
    with pytest.raises(TypeError):
        brain.Brain(NOT_A_CONSTANT=1)


def test_speaker_not_ok_dropped(env):
    out = run_brain(env, [utt("next wallpaper", speaker_ok=False)], timeout=0.3)
    assert out == [] and jv_calls(env) == []
    assert "route=drop" in (env / "state/heardback/brain.log").read_text()


@pytest.mark.parametrize("text", ["you", "Thank you.", "a", "  "])
def test_hallucination_ignored(env, text):
    assert run_brain(env, [utt(text)]) == [{"type": "done"}]
    assert jv_calls(env) == []


@pytest.mark.parametrize("text", ["stop", "never mind", "shut up"])
def test_builtins_only_done(env, text):
    assert run_brain(env, [utt(text)]) == [{"type": "done"}]
    assert jv_calls(env) == []


def test_bad_json_line_ignored(env):
    async def go():
        path = brain.sock_path()
        async with serving(brain.Brain(), path):
            r, w = await asyncio.open_unix_connection(str(path))
            w.write((json.dumps(HELLO) + "\n").encode())
            w.write(b"not json\n[1]\n" + b'{"type":"utterance","text":"you","speaker_ok":true}\n')
            await w.drain()
            line = await asyncio.wait_for(r.readline(), 3)
            w.close()
            return line
    assert asyncio.run(go()) == b'{"type": "done"}\n'


def test_say_forwarded_to_voice(env):
    async def go():
        path = brain.sock_path()
        async with serving(brain.Brain(), path):
            ra, wa = await asyncio.open_unix_connection(str(path))
            wa.write((json.dumps(HELLO) + '\n{"type":"utterance","text":"you","speaker_ok":true}\n').encode())
            await wa.drain()
            assert await asyncio.wait_for(ra.readline(), 3) == b'{"type": "done"}\n'   # hello processed
            await talk(path, [{"type": "say", "text": "Build finished."}], timeout=0.2)
            line = await asyncio.wait_for(ra.readline(), 3)
            wa.close()
            return line
    assert json.loads(asyncio.run(go())) == {"type": "say", "text": "Build finished.", "final": True, "announce": True}


def test_announce_without_voice_held(env):
    b = brain.Brain()
    asyncio.run(b.announce("Build finished."))
    assert b.events.rows == [{"kind": "done", "text": "Build finished."}]


def test_state_reaches_brain(env):
    b = brain.Brain()
    run_brain(env, [{"type": "state", "busy": True}, utt("you")], brain=b)
    assert b.voice_busy


def test_socket_is_0600(env):
    async def go():
        path = brain.sock_path()
        async with serving(brain.Brain(), path):
            return path.stat().st_mode & 0o777
    assert asyncio.run(go()) == 0o600


def test_stale_socket_replaced(env):
    path = brain.sock_path()
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(path))
    s.close()   # leaves a dead socket file behind
    assert path.exists()
    assert run_brain(env, [utt("you")]) == [{"type": "done"}]


def test_second_instance_refuses(env):
    async def go():
        path = brain.sock_path()
        async with serving(brain.Brain(), path):
            with pytest.raises(SystemExit, match="already running"):
                await brain.Brain().serve(path)
    asyncio.run(go())


def test_refuses_with_api_key(env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    with pytest.raises(SystemExit):
        asyncio.run(brain.serve(env / "s.sock"))
    assert not (env / "s.sock").exists()


def test_serve_wires_announcer_to_announce(env, monkeypatch):   # R3: announcer says go out as announce:true
    from heardback.status import announce
    seen = {}

    async def fake_run(ann, say):
        seen["ann"], seen["say"] = ann, say
        await say("Build finished.")

    async def fake_serve(self, path):
        await asyncio.sleep(0)
        seen["brain"] = self
        raise SystemExit("stop")
    monkeypatch.setattr(announce, "run", fake_run)
    monkeypatch.setattr(brain.Brain, "serve", fake_serve)
    with pytest.raises(SystemExit):
        asyncio.run(brain.serve(env / "s.sock"))
    b = seen["brain"]
    assert isinstance(seen["ann"], announce.Announcer) and b.announcer is seen["ann"]
    assert seen["say"] == b.announce


def test_log_tools_per_turn(env):
    run_brain(env, [utt("cue:tools zen_open,hypr_focus"), utt("hello")], claude_cmd=FAKE, gap=0.3)
    results = [l for l in (env / "state/heardback/brain.log").read_text().splitlines() if " result " in l]
    assert results[0].endswith(" tools=zen_open,hypr_focus")
    assert "tools=" not in results[1]
