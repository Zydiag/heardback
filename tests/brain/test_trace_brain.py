"""trace task 3: a fake-claude turn with a denied tool → route, tool_use, permission, tool_result, reply, result."""
import json

from brainutil import FAKE, run_brain, script


def trace(env):
    return [json.loads(l) for l in (env / "state/heardback/trace.jsonl").read_text().splitlines()]


def test_local_context_body_is_omitted_from_durable_stream_records():
    from heardback.brain.brain import Turn, _stream_record

    turn = Turn(None, "")
    turn.sensitive_context = True
    turn.tool_ids["ctx-1"] = "ctx_ctx"
    event = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "ctx-1",
                                                           "content": '{"text":"private words"}'}]}}
    assert "private words" not in json.dumps(_stream_record(turn, event))


def test_denied_tool_turn_is_one_traced_turn(env):
    script(env, {"tmux type --dry-run": {"rc": 4, "stderr": "pane is showing a prompt"}})
    args = {"target": "2", "text": "voice typing is working now", "enter": True}
    msg = {"type": "utterance", "text": "cue:tool mcp__jv__tmux_type " + json.dumps(args), "speaker_ok": True,
           "t": 0, "turn": "20261031-145833-5"}
    run_brain(env, [msg], claude_cmd=FAKE)
    evs = [e for e in trace(env) if e["turn"] == "20261031-145833-5"]
    kinds = [e["ev"] for e in evs]
    for k in ("route", "jv", "tool_use", "permission", "tool_result", "reply", "result"):
        assert k in kinds, (k, kinds)
    assert kinds.index("route") < kinds.index("tool_use") < kinds.index("permission") < kinds.index("reply")
    by = {e["ev"]: e for e in evs}
    assert by["route"]["route"] == "claude" and by["route"]["model"] == "haiku"
    assert by["tool_use"]["name"] == "tmux_type" and by["tool_use"]["args"] == args
    p = by["permission"]
    assert p["decision"] == "deny" and p["dry_run_rc"] == 4 and p["reason"] == "pane is showing a prompt"
    assert p["tool"] == "tmux_type" and p["args"] == args
    assert by["jv"]["rc"] == 4 and "--dry-run" in by["jv"]["argv"]
    assert by["tool_result"]["is_error"] is True and by["tool_result"]["name"] == "tmux_type"
    assert by["reply"]["text"].startswith("denied: pane is showing a prompt")
    assert by["result"]["subtype"] == "success" and by["result"]["tools"] == ["tmux_type"]
    assert all(e["src"] == "brain" for e in evs)


def test_fast_path_route_and_reply(env):
    script(env, {"tmux select": {"rc": 1, "stderr": "no window 9"}})
    run_brain(env, [{"type": "utterance", "text": "switch to tmux window two", "speaker_ok": True, "turn": "T"}])
    evs = {e["ev"]: e for e in trace(env) if e["turn"] == "T"}
    assert evs["route"]["route"] == "fast" and evs["route"]["verb"] == "tmux.select" and evs["route"]["args"]
    assert evs["jv"]["rc"] == 1 and evs["jv"]["stderr"] == "no window 9"
    assert evs["reply"]["text"] == "No window 9."


def test_trace_command_renders_the_denied_turn(env, capsys):
    """Task 4: `heardback-voice trace` over the brain's real trace of the denied turn, plus the voice side."""
    from heardback.voice.cli import main
    tid = "20261031-145833-5"
    script(env, {"tmux type --dry-run": {"rc": 4, "stderr": "pane is showing a prompt"}})
    args = {"target": "2", "text": "voice typing is working now", "enter": True}
    run_brain(env, [{"type": "utterance", "text": "cue:tool mcp__jv__tmux_type " + json.dumps(args),
                     "speaker_ok": True, "t": 0, "turn": tid}], claude_cmd=FAKE)
    f = env / "state/heardback/trace.jsonl"
    voice = [{"ts": "2000-01-01T14:58:33.000+05:30", "ev": "heard", "text": "voice typing is working now",
              "stt_ms": 420},
             {"ts": "2026-10-31T23:59:58.000+05:30", "ev": "say", "text": "Which window?", "expect_reply": True},
             {"ts": "2026-10-31T23:59:59.000+05:30", "ev": "turn_end", "outcome": "done", "total_s": 17.2}]
    with f.open("a") as w:
        w.writelines(json.dumps({"src": "voice", "turn": tid, **e}) + "\n" for e in voice)
        w.write(json.dumps({"ts": "2000-01-01T14:00:00.000+05:30", "src": "voice", "ev": "turn_end",
                            "turn": "ok-1", "outcome": "done", "total_s": 1.0}) + "\n")
    assert main(["trace", "--file", str(f), "--errors"]) == 0
    out = capsys.readouterr().out
    head, acts, ends = out.splitlines()
    assert head == f'14:58:33 #{tid} heard "voice typing is working now" (stt 0.42s)'
    assert acts.startswith("  → claude haiku") and 'tool tmux_type DENIED rc=4 "pane is showing a prompt"' in acts
    assert "tool tmux_type ok" not in acts and "--dry-run" not in acts
    assert ends == '  ← said "Which window?" (asked) · end done 17.2s'
    assert "ok-1" not in out
    assert main(["trace", "--file", str(f), "--turn", "ok-1"]) == 0
    assert capsys.readouterr().out.startswith("14:00:00 #ok-1 turn_end")
