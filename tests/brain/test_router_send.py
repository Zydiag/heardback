import pytest

from heardback.brain import router
from heardback.brain.router import Route
from jv.cli import all_verbs, catalogue


@pytest.fixture(scope="module")
def route():
    cat = {v["name"]: v for v in catalogue(all_verbs())["verbs"] if v.get("voice", True)}
    return router.Router(cat).route


@pytest.mark.parametrize("said,want", [
    ("send this page to claude", Route("tmux.new", {"run": "claude", "attach": "tab"}, 1.0)),
    ("send this tab to claude code", Route("tmux.new", {"run": "claude", "attach": "tab"}, 1.0)),
    ("pass this link to claude", Route("tmux.new", {"run": "claude", "attach": "tab"}, 1.0)),
    ("give the page to claude", Route("tmux.new", {"run": "claude", "attach": "tab"}, 1.0)),
    ("send my clipboard to claude", Route("tmux.new", {"run": "claude", "attach": "clipboard"}, 1.0)),
    ("send what i copied to claude", Route("tmux.new", {"run": "claude", "attach": "clipboard"}, 1.0)),
    ("send my selection to claude", Route("tmux.new", {"run": "claude", "attach": "selection"}, 1.0)),
    ("send the selected text to claude", Route("tmux.new", {"run": "claude", "attach": "selection"}, 1.0)),
    ("send my selection to window 3", Route("tmux.type", {"attach": "selection", "target": 3}, 1.0)),
    ("send the selected text to window 3", Route("tmux.type", {"attach": "selection", "target": 3}, 1.0)),
    ("send what i just said to claude", Route("tmux.new", {"run": "claude", "attach": "last_heard"}, 1.0)),
    ("send what i said to claude", Route("tmux.new", {"run": "claude", "attach": "last_heard"}, 1.0)),
    ("send what i just said to window 3", Route("tmux.type", {"attach": "last_heard", "target": 3}, 1.0)),
    ("send what i said to window 3", Route("tmux.type", {"attach": "last_heard", "target": 3}, 1.0)),
    ("send the output of window 4 to claude", Route("tmux.new", {"run": "claude", "attach": "pane", "attach_target": 4}, 1.0)),
    ("send this page to window 3", Route("tmux.type", {"attach": "tab", "target": 3}, 1.0)),
    ("send my clipboard to window 3", Route("tmux.type", {"attach": "clipboard", "target": 3}, 1.0)),
    ("send my clipboard to claude in window 3", Route("tmux.type", {"attach": "clipboard", "target": 3}, 1.0)),
    ("send the output of window 4 to claude in window 3", Route("tmux.type", {"attach": "pane", "attach_target": 4, "target": 3}, 1.0)),
    ("status of window three", Route("status.ask", {"who": "3"}, 1.0)),
    ("what's the status of window 2", Route("status.ask", {"who": "2"}, 1.0)),
    ("ask window 3 for its status", Route("status.ask", {"who": "3"}, 1.0)),
    ("ask window 3 for a status", Route("status.ask", {"who": "3"}, 1.0)),
    ("ask window 3 how it's going", Route("status.ask", {"who": "3"}, 1.0)),
    ("full status of window 3", Route("status.ask", {"who": "3", "full": True}, 1.0)),
    ("tell me when window 3 is done", Route("status.watch", {"who": "3"}, 1.0)),
    ("tell me when window 3 finishes", Route("status.watch", {"who": "3"}, 1.0)),
    ("tell me when window 3 has finished", Route("status.watch", {"who": "3"}, 1.0)),
    ("let me know when window 3 is done", Route("status.watch", {"who": "3"}, 1.0)),
    ("let me know when window 3 finishes", Route("status.watch", {"who": "3"}, 1.0)),
    ("let me know when window 3 has finished", Route("status.watch", {"who": "3"}, 1.0)),
    ("what did window 3 say", Route("status.detail", {"who": "3"}, 1.0)),
])
def test_plan14_phrase_rows(route, said, want):
    assert route(said) == want


def test_status_of_window_is_ask(route):
    assert route("status of window 3").verb == "status.ask"


@pytest.mark.parametrize("prefix", ["", "what's ", "what is "])
@pytest.mark.parametrize("target", ["my reminders", "web app", "the build"])
def test_bare_status_of_stays_hook_read(route, prefix, target):
    assert route(f"{prefix}status of {target}").verb == "status.get"


@pytest.mark.parametrize("said", ["status of window 3", "status of the build window"])
def test_window_shaped_status_of_asks_session(route, said):
    assert route(said).verb == "status.ask"


@pytest.mark.parametrize("said", ["don't tell me when window two's done",
                                   "tell me when window two's not done",
                                   "tell me when window done",
                                   "tell me when window two's done",
                                   "tell me when window three's done",
                                   "tell me when window two's done tomorrow"])
def test_contracted_watch_rejects_near_misses(route, said):
    assert route(said) is None


def test_full_status_not_get(route):
    r = route("full status of window 3")
    assert (r.verb, r.args) == ("status.ask", {"who": "3", "full": True})


def test_status_alone_is_list(route):
    assert route("status").verb == "status.list"


def test_send_page_not_zen(route):
    assert route("send this page to claude").verb == "tmux.new"
    assert route("send this page to Claude.").verb == "tmux.new"


def test_send_and_ask_goes_to_claude(route):
    assert route("send this page to claude and ask it to explain") is None


def test_type_and_send_it_unchanged(route):
    assert route("type hello and send it").verb == "tmux.type"


def test_paste_phrases_unchanged(route):
    assert route("paste my clipboard into window 3") == Route("tmux.type", {"target": 3, "clipboard": True}, 1.0)


def test_send_same_source_and_target_routes_for_verb_refusal(route):
    assert route("send the output of window 3 to window 3") == Route(
        "tmux.type", {"attach": "pane", "attach_target": 3, "target": 3}, 1.0)


@pytest.mark.parametrize("said,verb,args", [
    ("status of my sessions", "status.list", {}),
    ("status", "status.list", {}),
    ("is window 2 done", "status.get", {"who": "2"}),
    ("what is window 2 doing", "status.get", {"who": "window 2"}),
    ("what did window 3 do", "status.detail", {"who": "3"}),
])
def test_existing_status_routes_unchanged(route, said, verb, args):
    r = route(said)
    assert (r.verb, r.args) == (verb, args)


@pytest.mark.parametrize("said", ["tell me a joke", "tell me when the kettle boils"])
def test_watch_does_not_take_non_window_request(route, said):
    r = route(said)
    assert r is None or r.verb != "status.watch"


def test_eleven_words_gated(route):
    assert route("status of window alpha beta gamma delta epsilon zeta eta") is not None
    assert route("status of window alpha beta gamma delta epsilon zeta eta theta") is None
