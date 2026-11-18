import json
from pathlib import Path

from heardback.status import transcript

FIX = str(Path(__file__).parent / "fixtures" / "transcript.ndjson")


def test_text_parts_only():
    assert transcript.last_turns(FIX) == [
        {"role": "user", "text": "paint the fence blue"},
        {"role": "assistant", "text": "The fence is now blue."},
        {"role": "user", "text": "also the gate"},
        {"role": "assistant", "text": "Gate done too."},
    ]


def test_last_n():
    assert [t["text"] for t in transcript.last_turns(FIX, n=1)] == ["also the gate", "Gate done too."]


def test_tool_results_skipped():
    texts = [t["text"] for t in transcript.last_turns(FIX)]
    assert "fence.txt" not in texts and "BLOB should be ignored" not in texts


def test_sidechain_skipped():
    texts = [t["text"] for t in transcript.last_turns(FIX)]
    assert "sidechain chatter" not in texts and not any("/clear" in t for t in texts)


def test_caps(tmp_path):
    p = tmp_path / "t.jsonl"
    lines = [{"type": "assistant", "message": {"content": [{"type": "text", "text": c * 1000}]}} for c in "abcde"]
    p.write_text("".join(json.dumps(l) + "\n" for l in lines))
    out = transcript.last_turns(str(p), n=5)
    assert out and all(len(t["text"]) <= transcript.DETAIL_PART_MAX for t in out)
    assert sum(len(t["text"]) for t in out) <= transcript.DETAIL_TOTAL_MAX
    assert out[-1]["text"].startswith("e")  # the newest survive


def test_tail_drops_partial_line(tmp_path, monkeypatch):
    p = tmp_path / "t.jsonl"
    big = json.dumps({"type": "user", "message": {"content": "x" * 5000}})
    p.write_text(big + "\n" + json.dumps({"type": "user", "message": {"content": "last"}}) + "\n")
    monkeypatch.setattr(transcript, "TAIL_BYTES", 100)
    assert transcript.last_turns(str(p)) == [{"role": "user", "text": "last"}]


def test_missing_file(tmp_path):
    assert transcript.last_turns(None) == []
    assert transcript.last_turns(str(tmp_path / "nope.jsonl")) == []


def test_odd_lines(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text('not json\n[1]\n{"type": "user", "message": "str"}\n{"type": "user", "message": {"content": "hi"}}\n')
    assert transcript.last_turns(str(p)) == [{"role": "user", "text": "hi"}]
    assert transcript.last_turns(str(p), n=0) == []


def test_wrappers_and_compact_summary_skipped(tmp_path):
    p = tmp_path / "t.jsonl"
    lines = [{"type": "user", "message": {"content": "real prompt"}},
             {"type": "user", "message": {"content": "<task-notification>done</task-notification>"}},
             {"type": "user", "isCompactSummary": True, "message": {"content": "This session is being continued"}},
             {"type": "assistant", "message": {"content": [{"type": "text", "text": "<b>ok</b>"}]}}]
    p.write_text("".join(json.dumps(l) + "\n" for l in lines))
    assert transcript.last_turns(str(p)) == [{"role": "user", "text": "real prompt"},
                                             {"role": "assistant", "text": "<b>ok</b>"}]


def test_wrapper_tags_only(tmp_path):
    p = tmp_path / "t.jsonl"
    texts = ["<command-name>/clear</command-name>", "<local-command-stdout>x</local-command-stdout>",
             "<task-notification>t</task-notification>", "<system-reminder>r</system-reminder>",
             "<bash-input>ls</bash-input>", "<div> why is this centered?"]
    p.write_text("".join(json.dumps({"type": "user", "message": {"content": t}}) + "\n" for t in texts))
    assert transcript.last_turns(str(p), n=10) == [{"role": "user", "text": "<div> why is this centered?"}]
