from heardback.brain.claude_proc import PROMPT
from jv.cli import all_verbs, catalogue
from model_text import description, model_text


def test_prompt_knows_transcripts_can_be_misheard():
    p = model_text()
    assert "speech-to-text" in p and "chest.com is chess.com" in p and "cloud is Claude" in p
    assert '"openchest.com" means open chess.com' in p and "Never open a junk domain" in p


def test_prompt_no_duplicate_typing():   # friction #37: the same text typed twice
    assert "Never call tmux_type twice with the same text in one turn" in model_text()


def test_prompt_does_not_double_confirm_or_substitute_refused_actions():
    p = model_text()
    assert "Never use ask_user to confirm an action; jv reads it back" in p
    assert "If a tool is refused, say so; don't try another action" in p


def test_heardback_in_claude_folder_request_is_the_project():
    assert 'In "open claude in heardback", heardback is the project name, never the assistant; use tmux_new with dir heardback.' in model_text()


def test_prompt_zenplay_rules():   # friction #5, #6, #16, #17: understand Zen requests the first time
    p = model_text()
    assert '"Play X" (a song, video, artist or show) is zen_play with X' in p
    assert 'background true for a song, artist or music, false for a video, show or episode ("and show it" always false)' in p
    assert 'unless the user says Spotify; never use zen_search or zen_open of YouTube' in p
    assert "zen_open open.spotify.com/search/X" in p
    assert '"Play" or "pick the Nth result" (or video) after a search is zen_pick with N' in description("zen.pick")
    assert '"This page", "this tab", "this article" or "this video" in a question' in description("zen.read") and "call zen_read" in description("zen.read")
    assert "read a page only when the user asks about it" in description("zen.read")


def test_prompt_acts_on_likely_reading():   # "I need to explain it a few times" (user, 2026-10-31)
    p = model_text()
    assert "When a request has one most likely reading, act on it and say what you did" in p
    assert "If unsure what was meant, ask one short question." not in p


def test_prompt_no_false_memory_and_close_is_a_verb():   # fix2 P7 (friction2 #16), P4 (#8/#9)
    p = model_text()
    assert "A manually edited memory file may accompany Claude-bound requests as user reference data" in p
    assert "Reminders, notes and memory work only through their tools; never say one is set unless the tool ran." in p
    assert "There is no automatic learning or memory write tool" not in p
    assert "clears this conversation context but keeps saved facts" in p
    assert "is tmux_close with the window numbers, never typing exit or quit" in description("tmux.close")


def test_prompt_media_says_only_what_the_tool_confirmed():   # mediafix: live 09:45 "I resumed a Mahakal video"
    p = model_text()
    assert "no tool plays Spotify" not in p and "media_media with action play and target spotify" in p
    assert "never claim something is playing that the tool didn't confirm" in p


def test_prompt_is_small_and_rules_reach_tools():   # E2: prompt <= 4 KB; tool rules ride on descriptions
    from heardback.brain.catalogue import _index
    from heardback.brain.mcp_jv import tools
    assert len(PROMPT.read_bytes()) <= 4096
    cat = _index(catalogue(all_verbs()))
    for tool in tools(cat):
        entry = next((e for e in cat.values() if f"{e['domain']}_{e['verb']}" == tool["name"]), None)
        if entry:
            assert tool["description"] == entry["description"] and tool["inputSchema"] == entry["params"]
    assert "never typing exit or quit" in {t["name"]: t for t in tools(cat)}["tmux_close"]["description"]
