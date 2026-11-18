"""trace task 2: one turn id voice → brain → every jv the brain (or its MCP server) runs."""
import json
import os
import subprocess
import sys

from brainutil import run_brain, script, utt
from heardback.brain import catalogue


def turn_log(env):
    p = env / "turns.log"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def trace(env):
    p = env / "state/heardback/trace.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_fast_path_jv_gets_the_turn(env, monkeypatch):
    monkeypatch.setenv("FAKE_JV_TURN_LOG", str(env / "turns.log"))
    script(env, {"tmux select": {"rc": 0, "stdout": '{"ok":true,"say":"Window 2."}'}})
    run_brain(env, [{**utt("switch to tmux window two"), "turn": "20261031-145833-5"}])
    assert [["tmux", "select"], "20261031-145833-5"] in turn_log(env)
    (jv,) = [e for e in trace(env) if e["ev"] == "jv"]
    assert jv["turn"] == "20261031-145833-5" and jv["rc"] == 0 and jv["argv"][:3] == ["jv", "tmux", "select"]


def test_utterance_without_turn_gets_a_brain_id(env, monkeypatch):
    monkeypatch.setenv("FAKE_JV_TURN_LOG", str(env / "turns.log"))
    run_brain(env, [utt("next wallpaper")])
    (tid,) = [t for a, t in turn_log(env) if a == ["wall", "next"]]
    assert tid and "-b" in tid


def test_mcp_jv_reads_the_current_turn(env, monkeypatch):
    monkeypatch.setenv("FAKE_JV_TURN_LOG", str(env / "turns.log"))
    catalogue.set_current_turn("20261031-150000-7")
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "wall_next", "arguments": {}}}
    subprocess.run([sys.executable, "-m", "heardback.brain.mcp_jv"], input=json.dumps(msg) + "\n", capture_output=True,
                   text=True, timeout=20, env=os.environ.copy(), check=True)
    assert [["wall", "next"], "20261031-150000-7"] in turn_log(env)


def test_jv_main_writes_a_run_event(env, tmp_path):
    env_ = {**os.environ, "HEARDBACK_TURN": "t-42", "PATH": os.environ["PATH"]}
    code = "import sys; sys.argv=['jv','tmux','select','--json','--','nope-window']; from jv.cli import main; main()"
    p = subprocess.run([sys.executable, "-c", code], env=env_, capture_output=True, text=True, timeout=20)
    run = [e for e in trace(env) if e["ev"] == "run"]
    assert len(run) == 1, p.stderr
    r = run[0]
    assert r["src"] == "jv" and r["turn"] == "t-42" and r["rc"] == p.returncode and r["argv"][:2] == ["tmux", "select"]
    assert r["dry_run"] is False and r["yes"] is False and isinstance(r["ms"], int)
    assert (r["rc"] == 0) == (r["error"] is None)
