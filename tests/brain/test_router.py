import pytest

from heardback.brain import router


@pytest.mark.parametrize("raw,norm", [
    ("Hey Claude, switch to tmux window two, please.", "switch to tmux window 2"),
    ("Go to the third window", "switch to 3 window"),
    ("Open Firefox", "open zen"),
    ("What's my wallpaper?", "whats my wall"),
    ("Kill the browser", "close zen"),
    ("ask claude what is two plus two", "ask claude what is 2 plus 2"),
    ("Jarvis, can you show window number twenty for me", "switch to window 20"),
    ("ask the webapp claude", "ask webapp claude"),
    ("Hey Claude: next {wallpaper}", "next wall"),
])
def test_normalise(raw, norm):
    assert router.normalise(raw) == norm


def test_normalise_template_keeps_slots():
    assert router.normalise("switch to {window:enum}", template=True) == "switch to {window:enum}"


@pytest.mark.parametrize("said,target", [
    ("take a screenshot of the screen", "screen"), ("screenshot the screen", "screen"),
    ("capture the screen", "screen"), ("take a screenshot of this window", "window"),
    ("screenshot this window", "window"), ("capture this window", "window"),
    ("take a screenshot of an area", "area"), ("screenshot an area", "area"),
    ("capture an area", "area"),
])
def test_screenshot_capture_phrases(said, target):
    route = _live().route(said)
    assert (route.verb, route.args) == ("shot", {"target": target})


@pytest.mark.parametrize("said", ["send a screenshot to claude", "pass a screenshot to claude",
                                   "give a screenshot to claude"])
def test_screenshot_send_phrases_start_new_claude(said):
    route = _live().route(said)
    assert (route.verb, route.args) == ("tmux.new", {"run": "claude", "attach": "screenshot"})


@pytest.mark.parametrize("said", ["screen brightness", "share my screen", "take a note", "window status",
                                   "send a screenshot to window 3"])
def test_screenshot_near_misses_stay_unrouted(said):
    assert _live().route(said) is None


@pytest.mark.parametrize("said,norm", [
    ("Switch2 window4", "switch to window 4"),
    ("Switch to Window1", "switch to window 1"),
    ("Switch2 workspace4", "switch to workspace 4"),
    ("Switch to Workspace1", "switch to workspace 1"),
])
def test_complete_glued_numeric_navigation(said, norm):
    assert router.normalise(said) == norm


def test_strip_wake_only_at_start():
    assert router.strip_wake(["hey", "claude", "ask", "claude"]) == ["ask", "claude"]
    assert router.strip_wake(["Hey", "Jarvis,", "hi"]) == ["hi"]
    assert router.strip_wake(["open", "claude"]) == ["open", "claude"]


from brainutil import load_fixture  # noqa: E402
from heardback.brain.router import Route  # noqa: E402

T = router.compile_catalogue(load_fixture())


def test_exact_enum_digit_slot():
    assert router.match_exact("switch to tmux window two", T) == [Route("tmux.select", {"window": "2"}, 1.0)]


def test_int_slot():
    assert router.match_exact("switch to workspace three", T) == [Route("hypr.workspace", {"n": 3}, 1.0)]


def test_homophone_only_in_numeric_position():
    assert router.match_exact("window to", T)[0].args == {"window": "2"}
    assert router.match_exact("switch to tmux window", T) == []


def test_enum_slot_returns_original_choice():
    assert router.match_exact("open visual studio code", T)[0].args == {"app": "visual studio code"}


def test_int_range():
    assert router.match_exact("switch to workspace 11", T) == []


def test_free_text_keeps_raw():   # Review Focus 5
    r = router.match_exact("Ask Claude, what's the weather in Pune?", T)
    assert r == [Route("zen.ask", {"text": "what's the weather in Pune?"}, 1.0)]


def test_free_text_drops_leading_fillers():
    assert router.match_exact("ask claude the capital of France", T)[0].args == {"text": "capital of France"}


def test_optional_group():
    assert router.match_exact("ask in claude hi there", T)[0].args == {"text": "hi there"}


def test_colon_after_wake_word():   # M1 review minor 1
    assert router.match_exact("Ask Claude: what is up", T)[0].args == {"text": "what is up"}


def test_free_text_may_start_with_wake_word():   # M1 review minor 2
    assert router.match_exact("ask claude claude code tips", T)[0].args == {"text": "claude code tips"}
    assert router.match_exact("ask claude, Jarvis is cool", T)[0].args == {"text": "Jarvis is cool"}


def test_ask_named_session_is_not_zen():
    assert router.match_exact("ask the webapp claude to run the tests", T) == []


def test_bad_slot_name_skipped(caplog):
    cat = {"x.y": {"domain": "x", "verb": "y", "params": {"properties": {"n": {"type": "integer"}}}, "args": {},
                   "argv": ["n"], "confirm": False, "phrases": ["do {n:int}"]}}
    assert router.compile_catalogue(cat) == []
    assert "do {n:int}" in caplog.text


def test_slot_type_mismatch_skipped():
    cat = {"x.y": {"domain": "x", "verb": "y", "params": {}, "args": {"n": {"type": "str"}},
                   "argv": ["n"], "confirm": False, "phrases": ["do {n:int}"]}}
    assert router.compile_catalogue(cat) == []


def test_template_fields():
    t = next(t for t in T if t.text == "whats my wall")
    assert t.verb == "wall.current" and t.tokens == ["whats", "my", "wall"] and t.is_question and not t.has_text
    s = next(t for t in T if t.verb == "tmux.select")
    assert s.slots == {"window": "enum"} and s.tokens[-1] == "{window}"


import json  # noqa: E402
import time  # noqa: E402

from brainutil import FIX  # noqa: E402

R = router.Router(load_fixture())


@pytest.mark.parametrize("utt,verb,args", json.loads((FIX / "router_table.json").read_text()))
def test_table(utt, verb, args):
    t0 = time.perf_counter()
    r = R.route(utt)
    assert time.perf_counter() - t0 < 0.1
    assert (r is None) if verb == "CLAUDE" else (r is not None and (r.verb, r.args) == (verb, args))


def fixture_with_n_phrases(n: int) -> dict:
    cat = load_fixture()
    for i in range(n):
        cat[f"x.v{i}"] = {"domain": "x", "verb": f"v{i}", "confirm": False, "params": {}, "args": {},
                          "argv": [], "phrases": [f"do thing number {i} now"]}
    return cat


def test_200_templates_under_100ms():
    big = router.Router(fixture_with_n_phrases(200))
    assert len(big.templates) >= 200
    t0 = time.perf_counter()
    big.route("switch to window 7")
    assert time.perf_counter() - t0 < 0.1


def _two(phrase_a: str, phrase_b: str, args_b: dict | None = None) -> router.Router:
    return router.Router({
        "a.x": {"domain": "a", "verb": "x", "confirm": False, "params": {}, "args": {}, "argv": [], "phrases": [phrase_a]},
        "b.y": {"domain": "b", "verb": "y", "confirm": False, "params": {}, "args": args_b or {}, "argv": [],
                "phrases": [phrase_b]}})


def test_exact_tie_more_literals_wins():   # R2
    r = _two("next wallpaper", "next {thing:enum}", {"thing": {"type": "enum", "choices": ["wallpaper"]}})
    assert r.route("next wallpaper") == Route("a.x", {}, 1.0)


def test_exact_tie_equal_literals_goes_to_claude():   # R2
    assert _two("do it now", "do it now").route("do it now") is None


def test_non_exact_phrases_go_to_claude():
    assert _two("open pod bay doors", "close it").route("open pod bay doors now") is None


def test_confirm_verbs_still_require_exact_phrases():
    assert R.route("close unused tabs now") is None


def test_enum_choice_named_like_wake_word():
    assert R.route("window claude") == Route("tmux.select", {"window": "claude"}, 1.0)


def test_tie_more_literals_wins():
    T = router.compile_catalogue(load_fixture())
    assert {t.text: t.literals for t in T if t.verb in ("do.open-zen", "hypr.open")}["open zen"] == 2
    assert router.Router(load_fixture()).route("open zen").verb == "do.open-zen"


def test_equal_literals_still_claude():
    cat = load_fixture() | {"do.zen2": {**load_fixture()["do.open-zen"], "name": "do.zen2", "verb": "zen2"}}
    assert router.Router(cat).route("open zen") is None


def test_empty_enum_template_skipped():   # `add {name:enum}` with no drafts
    assert not [t for t in router.compile_catalogue(load_fixture()) if t.verb == "do.accept"]


def test_enum_choices_do_not_collapse_through_synonyms():   # live bug 2026-10-30: "open zen" launched Firefox
    r = _two("start up", "open {app:enum}", {"app": {"type": "enum", "choices": ["Firefox", "Zen Browser"]}})
    for said in ("open zen", "open zen browser", "open browser", "open firefox"):
        assert r.route(said) == Route("b.y", {"app": "Zen Browser"}, 1.0), said


@pytest.mark.parametrize("choices,said,want", [
    (["wallpaper", "wall"], "next wall", "wall"),          # a synonym-only match never shadows a literal one
    (["wall", "wallpaper"], "next wall", "wall"),
    (["kill", "close"], "next close", "close"),
    (["shut", "close"], "next close", "close"),
    (["show", "switch to"], "next switch to", "switch to"),
    (["background", "wall"], "next background", "wall"),
    (["Zen Browser"], "next zen", "Zen Browser"),
])
def test_enum_synonym_never_shadows(choices, said, want):
    r = _two("start up", "next {x:enum}", {"x": {"type": "enum", "choices": choices}})
    assert r.route(said) == Route("b.y", {"x": want}, 1.0)


def test_search_for_site_name_routes_to_search():   # live verbs: "search for whatsapp" must not hit zen.open
    from jv.cli import all_verbs, catalogue
    cat = {v["name"]: v for v in catalogue(all_verbs())["verbs"] if v.get("voice", True)}
    r = router.Router(cat)
    assert r.route("search for whatsapp").verb == "zen.search"
    assert r.route("search whatsapp").verb == "zen.search"
    assert r.route("open site whatsapp").verb == "zen.open"
    assert r.route("search shiv thunder on youtube").verb == "zen.search"   # ytsearch: not zen.play / zen.open


# --- frictionfix 2: fast-path phrases the user actually says (friction.md §A/§E #2) ----------
def _live():
    from jv.cli import all_verbs, catalogue
    return router.Router({v["name"]: v for v in catalogue(all_verbs())["verbs"] if v.get("voice", True)})


@pytest.mark.parametrize("said,verb,args", [
    ("Type in this window. Hello world", "tmux.type", {"target": "current", "text": "Hello world"}),   # #1/#2
    ("type in this window go with your next order", "tmux.type",
     {"target": "current", "text": "go with your next order"}),                                         # #13
    ("type in this session slash rename manager", "tmux.type",
     {"target": "current", "text": "slash rename manager"}),                                            # #7
    ("type hello", "tmux.type", {"target": "current", "text": "hello"}),
    ("type in window 3 git status", "tmux.type", {"target": 3, "text": "git status"}),
    ("type in window two hello", "tmux.type", {"target": 2, "text": "hello"}),
    ("press enter", "tmux.type", {"target": "current", "text": "", "enter": True}),                    # #9
    ("press enter in this window", "tmux.type", {"target": "current", "text": "", "enter": True}),     # #14
    ("press enter in window 4", "tmux.type", {"target": 4, "text": "", "enter": True}),
    ("change my wallpaper", "wall.next", {}),                                                           # #26
    ("change wallpaper", "wall.next", {}),
    ("next wallpaper", "wall.next", {}),
    ("switch to next wallpaper", "wall.next", {}),                                                      # #28
    ("show wallpapers", "wall.show", {}),
    ("show walls", "wall.show", {}),
    ("Open Wallpaper Picker", "wall.show", {}),                                                         # #41
    ("open dashboard in zen", "zen.open", {"query": "dashboard"}),                                      # #12
    ("open chess.com on zen", "zen.open", {"query": "chess.com"}),
    ("open youtube in firefox", "zen.open", {"query": "youtube"}),
    ("open site youtube", "zen.open", {"query": "youtube"}),
    # context: the clipboard / selection never passes through Claude
    ("paste my clipboard into window 3", "tmux.type", {"target": 3, "clipboard": True}),
    ("Paste my clipboard into window 3 and press enter.", "tmux.type", {"target": 3, "clipboard": True, "enter": True}),
    ("paste the copied text in window two", "tmux.type", {"target": 2, "clipboard": True}),
    ("type what I copied into window 3", "tmux.type", {"target": 3, "clipboard": True}),
    ("paste the selected text from the clipboard into window 4", "tmux.type",
     {"target": 4, "clipboard": True}),
    ("paste the selected text into window 3 and then press enter", "tmux.type",
     {"target": 3, "selection": True, "enter": True}),
    ("paste the selection into window 1", "tmux.type", {"target": 1, "selection": True}),
])
def test_live_fast_phrases(said, verb, args):
    assert _live().route(said) == Route(verb, args, 1.0)


@pytest.mark.parametrize("said", [
    "type hello and then press enter",                                    # #11: chain → Claude
    "type in this window voice typing is working I'm testing it right now",   # #1: > 10 words
    "type it and also type it after that",                                 # #37
    "type into window 3 hello",                                           # a target the templates don't know
    "yes type in window one who opened top process in another terminal",   # #22
    "open youtube and whatsapp in zen",
    "change wallpaper to second wallpaper",                               # #27
    "typing",
    "type",
    "paste my clipboard into window 3 and close zen",                     # context: a chain after the phrase
    "send the selected text to third window",                             # live 18:57: Claude, with ctx tools
    "Send the Slector Test to third window",                              # turn 20261031-185728-5, as heard
    "Selected Text Text I have Copied and Press Enter",                   # turn 20261031-185742-6: never press enter alone
])
def test_live_still_claude(said):
    assert _live().route(said) is None


def test_glued_navigation_targets_the_right_domain():
    from copy import deepcopy

    from jv.cli import all_verbs, catalogue

    verbs = deepcopy(all_verbs())
    next(v for v in verbs if v.name == "tmux.select").args["window"]["choices"] = lambda: ["1", "4"]
    cat = {v["name"]: v for v in catalogue(verbs)["verbs"] if v.get("voice", True)}
    r = router.Router(cat)
    assert r.route("Focus Workspace ONE") == Route("hypr.workspace", {"n": 1}, 1.0)
    assert r.route("Switch2 window4") == Route("tmux.select", {"window": "4"}, 1.0)
    assert r.route("Switch to Window1") == Route("tmux.select", {"window": "1"}, 1.0)
    assert r.route("Switch2 workspace4") == Route("hypr.workspace", {"n": 4}, 1.0)
    assert r.route("Switch2 window4 and then close zen") is None
    assert r.route("type in window 4 Switch2 window4").args["text"] == "Switch2 window4"


def test_live_type_and_enter_require_exact_phrases():
    r = _live()
    assert r.route("press entre") is None and r.route("pressed enter now") is None


# --- sessionhelp ---------------------------------------------------------------------------
@pytest.mark.parametrize("said,verb,args", [
    ("what is window 3 asking", "tmux.prompt", {"target": 3}),
    ("What is window three asking?", "tmux.prompt", {"target": 3}),
    ("what does window 2 want", "tmux.prompt", {"target": 2}),
    ("what is this window asking", "tmux.prompt", {"target": "current"}),
    ("Type hello world.", "tmux.type", {"target": "current", "text": "hello world"}),   # 5b: Whisper's stop
    ("type in this window wait...", "tmux.type", {"target": "current", "text": "wait..."}),
    ("catch me up", "catchup", {}),
    ("Jarvis, catch me up.", "catchup", {}),
    ("what did I miss", "catchup", {}),
    ("summarise my sessions", "status.summary", {}),
    ("what did window 4 do", "status.detail", {"who": "4"}),
    ("what did web app do", "status.detail", {"who": "web app"}),
    ("what are my sessions doing", "status.list", {}),
])
def test_sessionhelp_fast(said, verb, args):
    r = _live().route(said)
    assert r and (r.verb, r.args) == (verb, args)


def test_sessionhelp_argv_end_to_end():   # what the brain runs for these routes (no voice turn had used them)
    from heardback.brain import catalogue as cat_mod
    r = _live()
    for said, argv in [("catch me up", ["catchup", "--json"]),
                       ("what did window 4 do", ["status", "detail", "--json", "--", "4"]),
                       ("what is window 3 asking", ["tmux", "prompt", "--json", "--", "3"])]:
        route = r.route(said)
        assert cat_mod.build_argv(r.cat[route.verb], route.args, "--json") == ["jv", *argv]


# --- context: Claude's tools for "this" / clipboard / selection ---------------------------
def test_live_ctx_tools():
    from heardback.brain.brain import Brain
    cat = _live().cat
    assert Brain._allowed(cat) == []   # read-only calls still use the callback
    props = cat["tmux.type"]["params"]["properties"]
    assert props["clipboard"]["type"] == props["selection"]["type"] == "boolean"
    assert cat["tmux.type"]["params"]["required"] == ["target"]


# --- zenplay ---------------------------------------------------------------------------------
@pytest.mark.parametrize("said,verb,args", [
    ("play lofi hip hop on youtube", "zen.play", {"query": "lofi hip hop"}),
    ("Play Kendrick Lamar on YouTube.", "zen.play", {"query": "Kendrick Lamar"}),
    ("play four types of people in youtube", "zen.play", {"query": "four types of people"}),
    ("play the second result", "zen.pick", {"n": 2}),
    ("play the 2nd result", "zen.pick", {"n": 2}),
    ("pick result 3", "zen.pick", {"n": 3}),
    ("pick the second result from your search", "zen.pick", {"n": 2}),   # friction #17, as said
    ("open the third video", "zen.pick", {"n": 3}),
    ("Play second result on YouTube", "zen.pick", {"n": 2}),   # pickfix: live 2026-10-31 21:42:55
    ("play the 3rd video on youtube", "zen.pick", {"n": 3}),
    ("open result 2 in youtube", "zen.pick", {"n": 2}),
    ("pick the first one", "zen.pick", {"n": 1}),
    ("play second hand news on youtube", "zen.play", {"query": "second hand news"}),   # a real title
    ("pause the video", "zen.video", {"action": "pause"}),
    ("resume the video", "zen.video", {"action": "play"}),
    ("play the video", "zen.video", {"action": "play"}),
    ("stop the video", "zen.video", {"action": "pause"}),
])
def test_zenplay_fast(said, verb, args):
    assert _live().route(said) == Route(verb, args, 1.0)


@pytest.mark.parametrize("site", ["on youtube", "in youtube", "on you tube"])
@pytest.mark.parametrize("modifier", ["in background", "in the background"])
@pytest.mark.parametrize("before", [False, True])
def test_zenplay_background_is_explicit(site, modifier, before):
    r = _live()
    tail = f"{modifier} {site}" if before else f"{site} {modifier}"
    result = r.route(f"play lofi {tail}")
    assert result == Route("zen.play", {"query": "lofi", "background": True}, 1.0)
    assert r.route(f"play lofi {site}") == Route("zen.play", {"query": "lofi"}, 1.0)


@pytest.mark.parametrize("said", ["play lofi on youtube in background then close window 3",
                                  "play lofi in background on youtube and open gmail",
                                  "play lofi in background and close window 3 on youtube"])
def test_zenplay_background_does_not_swallow_another_action(said):
    assert _live().route(said) is None


@pytest.mark.parametrize("said", [
    "play jazz on spotify",           # friction #5: Spotify isn't YouTube; a named song goes to Claude
    "play lofi",                      # bare "play X": Claude picks zen.play by the prompt rule
    "read this page",                 # zen.read is Claude-only: page text goes to Claude on request
    "summarise this page",
    "play the 30th result",           # out of range
])
def test_zenplay_still_claude(said):
    assert _live().route(said) is None


def test_zen_read_has_no_fast_phrase():
    assert all(t.verb != "zen.read" for t in _live().templates)


# --- bgplay: media via MPRIS -----------------------------------------------------------------
@pytest.mark.parametrize("said,action", [
    ("stop the music", "pause"), ("Pause the music.", "pause"), ("pause the song", "pause"), ("pause song", "pause"),
    ("resume the music", "play"), ("play the music", "play"), ("next song", "next"), ("skip", "next"),
    ("skip this song", "next"), ("previous song", "previous"), ("what's playing", "status"),
    ("what's playing?", "status"),
])
def test_media_fast(said, action):
    args = {"action": action, **({"intent": "resume" if said == "resume the music" else "music"} if action == "play" else {})}
    assert _live().route(said) == Route("media", args, 1.0)


def test_media_still_routes_videos_and_spotify():
    r = _live()
    assert r.route("pause the video") == Route("zen.video", {"action": "pause"}, 1.0)
    assert r.route("play jazz on spotify") is None and r.route("play music on youtube").verb == "zen.play"


def test_prompt_music_is_media():
    from pathlib import Path

    from heardback.brain.brain import Brain
    from model_text import description
    assert "is media_media, never zen_video" in description("media")
    cat = _live().cat
    assert cat["media"]["confirm"] == "auto"   # missing Spotify may require a new-tab read-back
    assert "mcp__jv__media_media" not in Brain._allowed(cat)


# --- fix2 P2: a spoken "enter" / "send it" at the end stays fast (jv strips it); clear the line --------
@pytest.mark.parametrize("said,verb,args", [
    ("Type Read Handoff You are the manager Enter", "tmux.type",
     {"target": "current", "text": "Read Handoff You are the manager Enter"}),                          # friction2 #39
    ("type in this window git status and press enter", "tmux.type",
     {"target": "current", "text": "git status", "enter": True}),
    ("type in window 3 what's next and send it.", "tmux.type", {"target": 3, "text": "what's next", "enter": True}),
    ("type fix the tests and submit", "tmux.type", {"target": "current", "text": "fix the tests", "enter": True}),
    ("clear the line", "tmux.clear", {}),
    ("delete that", "tmux.clear", {}),
    ("clear that", "tmux.clear", {}),
    ("clear the line in window 2", "tmux.clear", {"target": 2}),
])
def test_fix2_dictation_phrases(said, verb, args):
    r = _live().route(said)
    assert (r.verb, r.args) == (verb, args)


# --- routerfix: a target after the text is never typed into the current pane; it goes to Claude ---------
@pytest.mark.parametrize("said", [
    "type git status in window 3 and press enter",
    "type hello in window 3",
    "type hello in window three.",
    "type hello into window 3",
    "type hello in window heardback",
    "type hello in window my project",
    "type hello in this window",
    "type ls in window 3 enter",
    "type ls in window 3, send it",
    "type ls in window 3 and hit enter",
    "type git status in the left pane of window 2",
    "type in this window hello in window 3",
])
def test_trailing_target_goes_to_claude(said):
    assert _live().route(said) is None


@pytest.mark.parametrize("said,args", [
    ("type in window 3 git status and press enter", {"target": 3, "text": "git status", "enter": True}),
    ("type in window 4 hello in window 3", {"target": 4, "text": "hello in window 3"}),   # the head named the target
    ("type hello", {"target": "current", "text": "hello"}),
    ("type ls enter", {"target": "current", "text": "ls enter"}),   # jv strips the spoken enter
    ("type the window 3 bug is fixed", {"target": "current", "text": "window 3 bug is fixed"}),   # "type the" = head
    ("type a bug in window 3 is fixed for everyone", {"target": "current",
                                                      "text": "bug in window 3 is fixed for everyone"}),
])
def test_text_without_a_trailing_target_stays_fast(said, args):
    r = _live().route(said)
    assert (r.verb, r.args) == ("tmux.type", args)


def test_trailing_target_gate_is_only_for_the_current_pane():
    assert _live().route("play how to clean a window on youtube").verb == "zen.play"


@pytest.mark.parametrize("said,verb,args", [
    ("move this window to workspace 2", "hypr.move", {"query": "active", "workspace": 2}),
    ("move the window to workspace 2", "hypr.move", {"query": "active", "workspace": 2}),
    ("move current window to workspace two", "hypr.move", {"query": "active", "workspace": 2}),
    ("move the active window to workspace 2", "hypr.move", {"query": "active", "workspace": 2}),
    ("move it to workspace 2", "hypr.move", {"query": "active", "workspace": 2}),
    ("move zen to workspace 2", "hypr.move", {"query": "zen", "workspace": 2}),
    ("move window 3 to 1", "tmux.move", {"window": 3, "to": 1}),
])
def test_move_this_window_means_active(said, verb, args):
    r = _live().route(said)
    assert (r.verb, r.args) == (verb, args)


@pytest.mark.parametrize("said,args", [("close window 3", {"windows": "3"}),
                                       ("close windows 2, 3, 4", {"windows": "2, 3, 4"}),
                                       ("kill window web", {"windows": "web"})])
def test_fix2_close_window_phrases(said, args):
    r = _live().route(said)
    assert (r.verb, r.args) == ("tmux.close", args)


# --- fix3 P9: what was actually said, fast ---------------------------------------------------------
@pytest.mark.parametrize("said,verb,args", [
    ("What's the status of third window?", "status.ask", {"who": "3"}),                  # plan 14 Task 6
    ("what is the status of window 2", "status.ask", {"who": "2"}),
    ("status of window three", "status.ask", {"who": "3"}),
    ("what's the status of web app", "status.get", {"who": "web app"}),
    ("what's the status of my sessions", "status.list", {}),                               # not who="my sessions"
    ("Close YouTube on Zen", "zen.close", {"query": "youtube"}),                          # friction2 #38
    ("close youtube in zen", "zen.close", {"query": "youtube"}),
    ("close the YouTube tabs", "zen.close", {"query": "youtube"}),
    ("close the youtube tab", "zen.close", {"query": "youtube"}),
    ("close youtube tabs on zen", "zen.close", {"query": "youtube"}),
    ("close tab youtube", "zen.close", {"query": "youtube"}),
    ("close unused tabs", "zen.close-unused", {}),
    ("close window 3", "tmux.close", {"windows": "3"}),
    ("Stop the music playing on Zen", "media", {"action": "pause", "target": "zen"}),                      # friction2 #29
    ("pause the music in zen", "media", {"action": "pause", "target": "zen"}),
    ("stop the music playing", "media", {"action": "pause"}),
    ("next song on zen", "media", {"action": "next", "target": "zen"}),
    ("resume the music on zen", "media", {"action": "play", "target": "zen", "intent": "resume"}),
    ("Open WhatsApp Web-on Zen", "zen.open", {"query": "WhatsApp Web"}),                  # friction2 #24
    ("open up whatsapp on zen", "zen.open", {"query": "whatsapp"}),
    ("open whatsapp in zen please", "zen.open", {"query": "whatsapp"}),
])
def test_fix3_fast(said, verb, args):
    assert _live().route(said) == Route(verb, args, 1.0)


@pytest.mark.parametrize("said", ["what's the status", "close youtube and spotify on zen",
                                  "play jazz on spotify", "open youtube and whatsapp in zen"])
def test_fix3_still_claude(said):
    assert _live().route(said) is None


# --- sysctl: volume / brightness / bluetooth -------------------------------------------------------
@pytest.mark.parametrize("said,verb,args", [
    ("volume up", "volume", {"action": "up"}), ("Turn it up.", "volume", {"action": "up"}),
    ("turn up the volume", "volume", {"action": "up"}), ("louder", "volume", {"action": "up"}),
    ("quieter", "volume", {"action": "down"}), ("turn it down a bit", "volume", {"action": "down", "n": 5}),
    ("volume 40", "volume", {"action": "set", "n": 40}), ("Set volume to 40%", "volume", {"action": "set", "n": 40}),
    ("set the volume to 40 percent", "volume", {"action": "set", "n": 40}),
    ("mute", "volume", {"action": "mute"}), ("unmute", "volume", {"action": "unmute"}),
    ("what's the volume?", "volume", {"action": "get"}),
    ("brightness up", "brightness", {"action": "up"}), ("brighter", "brightness", {"action": "up"}),
    ("dimmer", "brightness", {"action": "down"}), ("brightness 50", "brightness", {"action": "set", "n": 50}),
    ("Dim the screen.", "brightness", {"action": "set", "n": 20}),
    ("full brightness", "brightness", {"action": "set", "n": 100}),
    ("what's the brightness", "brightness", {"action": "get"}),
    ("bluetooth on", "bt.power", {"state": "on"}), ("turn off bluetooth", "bt.power", {"state": "off"}),
    ("connect my buds", "bt.connect", {"name": "buds"}), ("connect to the speaker", "bt.connect", {"name": "speaker"}),
    ("connect my buds to bluetooth", "bt.connect", {"name": "buds"}),
    ("disconnect my buds", "bt.disconnect", {"name": "buds"}),
    ("is the JBL connected", "bt.status", {"name": "jbl"}), ("are my buds connected", "bt.status", {"name": "buds"}),
    ("what's connected", "bt.status", {}),
])
def test_sysctl_fast(said, verb, args):
    assert _live().route(said) == Route(verb, args, 1.0)


@pytest.mark.parametrize("said,verb", [("stop the music", "media"), ("next song", "media"),
                                       ("what's playing", "media"), ("pause the video", "zen.video")])
def test_sysctl_leaves_media_alone(said, verb):
    assert _live().route(said).verb == verb


def test_sysctl_still_claude():
    r = _live()
    assert r.route("mute the youtube tab") is None and r.route("connect my buds and play music") is None


@pytest.mark.parametrize("said,verb,args", [
    ("do not disturb status", "settings.dnd", {"state": "state"}),
    ("turn on do not disturb", "settings.dnd", {"state": "on"}),
    ("notifications back on", "settings.dnd", {"state": "off"}),
    ("night light on", "settings.night", {"state": "on"}),
    ("night light off", "settings.night", {"state": "off"}),
    ("warmer screen", "settings.night", {"state": "warmer"}),
    ("power saver", "settings.power", {"profile": "power-saver"}),
    ("performance mode", "settings.power", {"profile": "performance"}),
    ("balanced power profile", "settings.power", {"profile": "balanced"}),
    ("current power profile", "settings.power_get", {}),
    ("read my power profile", "settings.power_get", {}),
    ("power profile status", "settings.power_get", {}),
    ("list audio outputs", "settings.outputs", {}),
    ("available audio outputs", "settings.outputs", {}),
    ("list audio sinks", "settings.outputs", {}),
    ("switch audio to headphones", "settings.output", {"name": "headphones"}),
    ("use speakers for audio", "settings.output", {"name": "speakers"}),
    ("switch to headphones speakers", "settings.output", {"name": "headphones"}),
    ("mute my mic", "settings.mic", {"state": "mute"}),
    ("mute the microphone", "settings.mic", {"state": "mute"}),
    ("unmute my mic", "settings.mic", {"state": "unmute"}),
    ("make this fullscreen", "settings.window", {"action": "fullscreen"}),
    ("fullscreen this window", "settings.window", {"action": "fullscreen"}),
    ("float this window", "settings.window", {"action": "float"}),
    ("dark mode", "settings.theme", {"scheme": "dark"}),
    ("switch to dark mode", "settings.theme", {"scheme": "dark"}),
    ("light mode", "settings.theme", {"scheme": "light"}),
    ("how much battery is left", "system.battery", {}),
    ("what's my battery", "system.battery", {}),
    ("battery status", "system.battery", {}),
    ("how full is my disk", "system.disk", {}),
    ("disk space", "system.disk", {}),
    ("root disk usage", "system.disk", {}),
    ("how much memory is used", "system.memory", {}),
    ("memory usage", "system.memory", {}),
    ("how is my memory", "system.memory", {}),
    ("how hot is my computer", "system.temperature", {}),
    ("computer temperature", "system.temperature", {}),
    ("system temperature", "system.temperature", {}),
    ("what's my GPU temperature", "system.gpu", {}),
    ("GPU status", "system.gpu", {}),
    ("how is my GPU", "system.gpu", {}),
    ("are there updates", "system.updates", {}),
    ("any updates available", "system.updates", {}),
    ("check for updates", "system.updates", {}),
    ("is tailscale up", "system.tailscale", {}),
    ("tailscale status", "system.tailscale", {}),
    ("is my tailnet connected", "system.tailscale", {}),
    ("am I online", "system.online", {}),
    ("is the internet working", "system.online", {}),
    ("check my connection", "system.online", {}),
    ("calculate 18 percent of 2400", "calculate", {"expr": "18 percent of 2400"}),
    ("what is 5 miles to kilometers", "calculate", {"expr": "5 miles to kilometers"}),
    ("convert 32 fahrenheit to celsius", "calculate", {"expr": "32 fahrenheit to celsius"}),
])
def test_plan15_fast_phrases(said, verb, args):
    assert _live().route(said) == Route(verb, args, 1.0)


@pytest.mark.parametrize("said", [
    "what is the weather", "what is this error", "what is the time",
    "what is my battery level", "what is my GPU temperature",
    "what is 2 plus 2 and explain how it works", "convert this PDF to text",
    "convert the screenshot to text", "convert this video and send it to Claude",
    "how hot is it", "how hot is it outside", "how hot is it in Delhi",
    "do not disturb Claude", "do not disturb off", "check for updates on my task",
    "mute my music", "do not make this fullscreen", "switch audio to speakers and play music",
])
def test_settings1_keeps_unrelated_requests_for_claude(said):
    assert _live().route(said) is None


@pytest.mark.parametrize("said,verb,args", [
    ("what is my volume", "volume", {"action": "get"}),
    ("what is my brightness", "brightness", {"action": "get"}),
    ("what is window 4 doing", "status.get", {"who": "window 4"}),
    ("what is playing", "media", {"action": "status"}),
    ("calculate 2 + 2", "calculate", {"expr": "2 + 2"}),
    ("convert 5 miles to kilometers", "calculate", {"expr": "5 miles to kilometers"}),
    ("what's 18 percent of 2400", "calculate", {"expr": "18 percent of 2400"}),
])
def test_settings1_preserves_specific_and_math_routes(said, verb, args):
    actual = _live().route(said)
    assert actual is None if said in {"what is my volume", "what is my brightness"} else \
        actual is not None and (actual.verb, actual.args) == (verb, args)


@pytest.mark.parametrize("said,expr", [
    ("Hey Claude, what's 18 percent of 2400.", "18 percent of 2400"),
    ("what is 2 plus 2", "2 plus 2"),
    ("calculate (2 + 3) * 4", "(2 + 3) * 4"),
    ("convert 2 hours in minutes", "2 hours in minutes"),
])
def test_calculator_admits_only_math_with_original_expression(said, expr):
    result = _live().route(said)
    assert result == Route("calculate", {"expr": expr}, 1.0)


@pytest.mark.parametrize("said", [
    "calculate this error", "calculate 2 plus 2 and explain it", "what's the weather",
    "what's 2 plus 2 and explain it", "convert 5 miles to kilometers and send it to Claude",
    "calculate 2; touch a-file", "convert 2 PDFs to text",
])
def test_calculator_hands_non_math_and_extra_requests_to_claude(said):
    assert _live().route(said) is None


@pytest.mark.parametrize("said,verb,args", [
    ("open Spotify", "hypr.open", {"app": "Spotify"}),
    ("open Spotify app", "hypr.open", {"app": "Spotify"}),
    ("open desktop Spotify", "hypr.open", {"app": "Spotify"}),
    ("open Spotify website", "zen.open", {"query": "spotify"}),
    ("open Spotify site", "zen.open", {"query": "spotify"}),
    ("open Spotify in Zen", "zen.open", {"query": "Spotify"}),
])
def test_spotify_open_keeps_app_and_website_separate(monkeypatch, said, verb, args):
    from jv import hypr, zen
    from jv.cli import catalogue as cli_catalogue
    from heardback.brain import catalogue
    monkeypatch.setattr(hypr, "desktop_apps", lambda: {"Spotify": "spotify"})
    cat = catalogue._index(cli_catalogue(hypr.VERBS + zen.VERBS))
    route = router.Router(cat).route(said)
    assert route and route.verb == verb and route.args == args


@pytest.mark.parametrize("said,verb,query", [
    ("open whatsapp in the background", "zen.open", "whatsapp"),
    ("open site gmail in background", "zen.open", "gmail"),
    ("open chess.com, don't switch", "zen.open", "chess.com"),
    ("open site gmail and don't switch", "zen.open", "gmail"),
    ("show gmail without switching", "zen.open", "gmail"),
    ("switch to tab github in the background", "zen.switch", "github"),
    ("search for cats in the background", "zen.search", "cats"),
    ("search cats on youtube, do not switch", "zen.search", "cats on youtube"),
])
def test_background_navigation_precedes_wallpaper_normalization(said, verb, query):
    from jv import zen
    from jv.cli import catalogue as cli_catalogue
    from heardback.brain import catalogue
    r = router.Router(catalogue._index(cli_catalogue(zen.VERBS)))
    result = r.route(said)
    assert result and result.verb == verb and result.args == {"query": query, "background": True}


@pytest.mark.parametrize("said", ["search cats in background and play music",
                                  "open site gmail and search cats in background",
                                  "open site gmail and don't switch then play music"])
def test_background_modifier_does_not_swallow_multiple_actions(said):
    assert _live().route(said) is None


def test_background_wallpaper_phrases_stay_wallpaper_commands():
    assert _live().route("next background").verb == "wall.next"
    assert router.normalise("change background") == "change wall"


def test_status_is_a_catalogue_phrase():
    from jv import cli
    cat = {v["name"]: v for v in cli.catalogue(cli.all_verbs())["verbs"] if v.get("voice", True)}
    assert router.Router(cat).route("status") == Route("status.list", {}, 1.0)
    assert router.Router(cat).route("status unknown") is None
    cat.pop("status.list")
    assert router.Router(cat).route("status") is None


@pytest.mark.parametrize("said,expected", [("status", "status.list"), ("focus window 1", "tmux.select")])
def test_shortcuts_are_exact_phrases(said, expected):
    from jv import cli
    cat = {v["name"]: v for v in cli.catalogue(cli.all_verbs())["verbs"] if v.get("voice", True)}
    cat["tmux.select"]["args"]["window"]["choices"] = ["1"]
    assert router.Router(cat).route(said).verb == expected
    assert (router.Router(cat).route(said + " now").verb == "hypr.focus") if said.startswith("focus") else \
        router.Router(cat).route(said + " now") is None


def test_two_reminder_requests_are_exact_routes():
    r = _live()
    assert r.route("remind me at 8 pm to X").verb == "reminder.add"
    assert r.route("remind me at 9 to Y").verb == "reminder.add"


@pytest.mark.parametrize("text", ["status delete files", "status unknown", "status and close window 2",
                                  "status then shut down", "status erase"])
def test_status_shorthand_does_not_accept_unknown_or_destructive_tails(text):
    assert router.Router(load_fixture()).route(text) is None


def test_unused_tab_phrasings_never_close_a_window():
    # 2026-11-17 live: "close the Zen tabs I haven't used" routed to hypr.close and closed the Zen window.
    from jv import cli
    r = router.Router({v["name"]: v for v in cli.catalogue(cli.all_verbs())["verbs"]})
    for s in ("Close the Zen tabs I haven't used", "close the tabs i have not used", "close old tabs",
              "close all the idle zen tabs", "close unused tabs"):
        assert r.route(s).verb == "zen.close-unused", s
    assert r.route("close youtube tabs").verb == "zen.close"
    assert r.route("close spotify").verb == "hypr.close"
