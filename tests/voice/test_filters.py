from types import SimpleNamespace

import pytest

from heardback.voice.text import clean, clean_dictation, fix_mishear, hotwords, match_confirm, split_sentences

S = lambda text, nsp=0.0, lp=-0.2, cr=1.2: SimpleNamespace(text=text, no_speech_prob=nsp, avg_logprob=lp, compression_ratio=cr)


def test_clean_drops_ghosts_but_keeps_confirm_words():
    assert clean([S(" Thank you.")]) == ""
    assert clean([S(" Thanks for watching!")]) == ""
    for w in ["yes", "Yeah.", "no", "OK", "sure"]:
        assert clean([S(w)]) != ""


def test_clean_drops_silence_and_loops_and_markers():
    assert clean([S(" hello there", nsp=0.7, lp=-1.2)]) == ""
    assert clean([S(" go go go go go", cr=2.6)]) == ""
    assert clean([S(" [Music] (laughs)")]) == ""
    assert clean([S(" a")]) == ""                       # < 2 alphanumerics
    assert clean([S(" [Music] switch to window 2")]) == "switch to window 2"


def test_clean_joins_segments_and_keeps_good_ones():
    assert clean([S(" switch to"), S(" uh uh uh uh", cr=2.5), S(" window 2.")]) == "switch to window 2."


def test_clean_strips_leading_wake_phrase():
    assert clean([S(" Hey Claude, switch to tmux window 2.")]) == "switch to tmux window 2."
    assert clean([S(" hey claude")]) == ""


def test_dictation_cleanup_preserves_literal_short_words_and_wake_phrase():
    assert clean_dictation([S("  You  uh "), S("hey Claude, press enter  ")]) == "You uh hey Claude, press enter"
    assert clean_dictation([S(" thanks ")]) == "thanks"
    assert clean_dictation([S(" "), S("\t")]) == ""


def test_fix_mishear_longest_whole_phrase_and_single_pass():
    fixes = {" cloud ": "Claude", "CLOUD CODE": "Claude Code", "alpha": "beta", "beta": "gamma"}
    assert fix_mishear("cloud code, cloudy cloud!", fixes) == "Claude Code, cloudy Claude!"
    assert fix_mishear("alpha", fixes) == "beta"
    assert fix_mishear("quotation mark") == "quotation mark"
    assert fix_mishear("Cloud\t code", {"cloud  code": "Claude Code"}) == "Claude Code"


def test_match_confirm():
    assert match_confirm("Yeah, do it.") is True
    assert match_confirm("go ahead") is True
    assert match_confirm("No.") is False
    assert match_confirm("cancel") is False
    assert match_confirm("maybe later") is None
    assert match_confirm("yes and also close zen") is None    # whole transcript, not a prefix


@pytest.mark.parametrize("said,want", [
    # yesfix live 2026-11-01: the four read-back answers
    ("Yes, close it.", True), ("Close it", True), ("Leave it.", False), ("Kill windows webapp", None),
    ("yes close it", True), ("yeah kill it", True), ("yes please", True), ("ok do it", True),
    ("yes close the window", True), ("sure send it", True), ("Yes, please close it.", True),
    ("kill it", True), ("run it", True), ("send it", True), ("do that", True), ("press it", True),
    ("open it", True), ("delete it", True), ("please close it", True), ("close it please", True),
    ("go ahead and", None), ("close it go ahead", True), ("yeah, go ahead", True), ("okay", True),
    ("no", False), ("no leave it", False), ("no don't", False), ("Leave it", False), ("keep it", False),
    ("don't close it", False), ("not now", False), ("never mind", False), ("Nope.", False),
    ("yes but window 2", None), ("close window 2 instead", None), ("no, close window 2 instead", None),
    ("yes close window 2", None), ("open it in Claude folder", None), ("only the old ones", None), ("", None),
])
def test_match_confirm_table(said, want):
    assert match_confirm(said) is want


def test_split_sentences():
    assert split_sentences("Done. Window 2 is up! Next?") == ["Done.", "Window 2 is up!", "Next?"]
    assert split_sentences("  ") == []


def test_hotwords_from_catalogue_and_static():
    cat = {"version": 1, "verbs": [{"name": "tmux switch"}, {"name": "wall set"}]}
    h = hotwords(cat).split()
    assert {"tmux", "switch", "wall", "set", "Hyprland", "Zen", "Claude", "wallpaper", "workspace"} <= set(h)
    assert len(h) == len(set(h))
    assert "tmux" in hotwords(None)


def test_hotwords_order_dedup_and_cap():
    h = hotwords({"verbs": [{"name": "tmux.switch"}]}, user=["Mornye", "zen"], local=["heardback", "Obsidian"]).split()
    assert h[:2] == ["Mornye", "zen"] and "Zen" not in h          # user words first, case-insensitive dedup
    assert h.index("chess.com") < h.index("heardback") < h.index("switch")   # sites, then projects/apps, then verbs
    long = hotwords(None, user=["mine"], local=[f"app{i}" for i in range(500)])
    assert len(long) <= 500 and long.startswith("mine tmux") and "chess.com" in long


def test_dangling_command_heads():
    from heardback.voice.text import dangling
    for t in ["Type", "typing", "Type in this window.", "type in window 1", "in window 1", "in window one",
              "write", "type into the current pane", "send to window two", "type this", "Type in this session"]:
        assert dangling(t), t
    for t in ["type hello", "type in this window hello world", "typing is working", "in window 1 type hello",
              "press enter", "send it", "type it", "window 1", "switch to window 1", ""]:
        assert not dangling(t), t


def test_hotwords_clipboard_words():   # live 18:57: "selected text" heard as "Slector Test"
    h = hotwords(None)
    assert all(w in h for w in ("selected text", "clipboard", "copied", "selection", "paste"))


def test_clean_drops_whisper_credit_and_repeat_ghosts():   # fix2 P5 (friction2 #2, #20)
    for g in ["Copyright Australian Broadcasting Corporation", "Subtitles by the Amara.org community",
              "Transcribed by ESO", "Amara.org", "Watch it Watch it Watch it", "Watch it. Watch it. Watch it.",
              "ha ha ha ha"]:
        assert clean([S(g)]) == "", g
    for ok in ["copy that to window 2", "watch it watch it", "play it again play it again later",
               "type ha ha ha in window 2", "no no"]:
        assert clean([S(ok)]) != "", ok
