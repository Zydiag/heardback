"""Duck (2026-10-31): voice.Media over jv.media, with a fake playerctl at the subprocess boundary."""
import json
import subprocess

import pytest

from jv import media as jvmedia
from heardback.voice.media import Media

ZEN, PHONE = "firefox.instance_1_658", "kdeconnect.mpris_cce6"


@pytest.fixture
def players(monkeypatch):
    """state: name → status; urls: name → xesam:url (default ""); returns (state, play/pause calls, urls)."""
    state, acts, urls = {}, [], {}

    def run(argv, **kw):
        assert argv[0] == "playerctl" and "shell" not in kw
        if argv[1:3] == ["-a", "metadata"]:
            out = "".join(f"{n}\t{s}\tt\ta\t{urls.get(n, '')}\n" for n, s in state.items())
            return subprocess.CompletedProcess(argv, 0 if state else 1, out, "" if state else "No players found")
        _, name, action = argv[1:]
        acts.append(f"{name} {action}")
        state[name] = {"pause": "Paused", "play": "Playing"}.get(action, state[name])
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(jvmedia.subprocess, "run", run)
    return state, acts, urls


def _ducks():
    from jv import trace
    p = trace.path()
    return [(e["action"], e["players"]) for e in map(json.loads, p.read_text().splitlines()) if e["ev"] == "duck"] \
        if p.exists() else []


def test_pause_then_resume_ours(players):
    state, acts, _ = players
    state.update({"mpd": "Paused", ZEN: "Playing", PHONE: "Playing"})
    m = Media()
    m.pause("t1")
    assert acts == [f"{ZEN} pause"] and list(m.ours) == [ZEN]   # never the phone, never an already-paused one
    assert jvmedia.duck_path().read_text() == f"{ZEN}\t\tt\n" and jvmedia.ducked() == {ZEN}
    m.pause("t1")                                            # a follow-up listen: nothing new
    m.resume("t1")
    assert acts == [f"{ZEN} pause", f"{ZEN} play"] and not jvmedia.duck_path().exists()
    assert _ducks() == [("paused", [ZEN]), ("resumed", [ZEN])]


def test_playback_verb_keeps_it_paused(players):   # live 22:57:59: zen.play ran, then resume restarted the old music
    state, acts, _ = players
    state.update({ZEN: "Playing"})
    m = Media()
    m.pause("t1")
    jvmedia.claim()                  # what zen.play / media do
    state[ZEN] = "Paused"            # Zen's one MPRIS player still shows the old tab before the new one plays
    m.resume("t1")
    assert acts == [f"{ZEN} pause"] and _ducks()[-1] == ("kept", [ZEN])


def test_bare_stop_keeps_it_paused(players):
    state, acts, _ = players
    state.update({"mpd": "Playing"})
    m = Media()
    m.pause()
    m.keep()
    m.resume()
    assert acts == ["mpd pause"] and state["mpd"] == "Paused"


@pytest.mark.parametrize("background", [False, True])
def test_ordinary_browsing_recovers_native_players_but_guards_lagging_browser_metadata(players, monkeypatch, background):
    from jv import zen
    state, acts, urls = players
    state.update({"spotify": "Playing", "mpd": "Playing", ZEN: "Playing"})
    urls.update({"spotify": "spotify:track:ABC", ZEN: "https://youtube.com/watch?v=old"})
    m = Media()
    m.pause("t1")
    monkeypatch.setattr(zen, "call", lambda op, args: {
        "ok": True, "tabId": 2, "windowId": 1, "target_validated": True, "activation_confirmed": True})
    monkeypatch.setattr(zen, "_desktop_focus", lambda: {"workspace": None, "focus_confirmed": False})
    zen._browse_tab({"id": 2, "window": 1, "title": "WhatsApp", "url": "https://web.whatsapp.com/"},
                    {"navigation_focus": True}, background=background)
    # The browser still advertises the old URL; that lag must not defeat selective invalidation.
    m.resume("t1")
    assert "spotify play" in acts and "mpd play" in acts
    assert (f"{ZEN} play" in acts) is background


def test_selective_claim_with_no_native_rows_never_recovers_browser(players):
    state, acts, _ = players
    state[ZEN] = "Playing"
    m = Media()
    m.pause("t1")
    jvmedia.claim_browser()
    assert jvmedia.ducked() == set()
    m.resume("t1")
    assert acts == [f"{ZEN} pause"] and not jvmedia.duck_path().exists()


def test_selective_claim_cannot_recreate_a_concurrently_claimed_file(players, monkeypatch):
    from pathlib import Path
    state, acts, _ = players
    state.update({"spotify": "Playing", ZEN: "Playing"})
    m = Media()
    m.pause("t1")
    original = Path.open

    class ConcurrentClaim:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def readlines(self):
            rows = self.stream.readlines()
            jvmedia.claim()
            return rows
        def __getattr__(self, name):
            return getattr(self.stream, name)

    def opening(path, mode="r", *args, **kw):
        stream = original(path, mode, *args, **kw)
        return ConcurrentClaim(stream) if path == jvmedia.duck_path() and mode == "r+" else stream

    monkeypatch.setattr(Path, "open", opening)
    jvmedia.claim_browser()
    assert not jvmedia.duck_path().exists()
    m.resume("t1")
    assert not any(action.endswith(" play") for action in acts)


def test_user_stopped_it_meanwhile(players):
    state, acts, _ = players
    state.update({"mpd": "Playing"})
    m = Media()
    m.pause()
    state["mpd"] = "Stopped"
    m.resume()
    assert acts == ["mpd pause"] and m.ours == {}


def test_disabled_does_nothing(players):
    state, acts, _ = players
    state.update({"mpd": "Playing"})
    m = Media(enabled=False)
    m.pause()
    m.resume()
    assert acts == [] and state["mpd"] == "Playing"


def test_stale_duck_file_is_cleared_on_start(players):
    jvmedia.duck_path().write_text("mpd\n")
    Media()
    assert not jvmedia.duck_path().exists()


def test_missing_playerctl_is_quiet(monkeypatch):
    def missing(argv, **kw):
        raise FileNotFoundError(argv[0])
    monkeypatch.setattr(jvmedia.subprocess, "run", missing)
    m = Media()
    m.pause()
    m.resume()
    assert m.ours == {}


# duckfix (live 2026-10-31 ~23:50): YouTube ducked, "close youtube", Zen's one player now shows the Spotify tab
YT, SPOTIFY = "https://www.youtube.com/watch?v=jfKfPfyJRdk", "https://open.spotify.com/"


def test_player_now_on_another_tab_is_not_resumed(players):
    state, acts, urls = players
    state[ZEN], urls[ZEN] = "Playing", YT
    m = Media()
    m.pause("t1")
    assert jvmedia.duck_path().read_text() == f"{ZEN}\t{YT}\tt\n"
    urls[ZEN] = SPOTIFY              # the tab closed; nothing claimed (not a playback verb)
    m.resume("t1")
    assert acts == [f"{ZEN} pause"] and state[ZEN] == "Paused"
    from jv import trace
    last = json.loads(trace.path().read_text().splitlines()[-1])
    assert last["action"] == "kept" and last["reason"] == "changed" and last["players"] == [ZEN]


def test_same_url_is_resumed(players):
    state, acts, urls = players
    state.update({ZEN: "Playing", "mpd": "Playing"})
    urls[ZEN] = YT
    m = Media()
    m.pause("t1")
    m.resume("t1")
    assert sorted(acts) == sorted([f"{ZEN} pause", "mpd pause", f"{ZEN} play", "mpd play"])   # mpd: no url, same title
    assert _ducks()[-1] == ("resumed", [ZEN, "mpd"])


def test_failed_pause_is_not_owned_and_failed_resume_not_reported(players, monkeypatch):
    from jv.verb import JvError
    state, acts, _ = players
    state[ZEN] = "Playing"
    m = Media()
    def rejected(*args, **kwargs):
        raise JvError(1, "rejected")
    monkeypatch.setattr(jvmedia, "_pc", rejected)
    m.pause("failed")
    assert not m.ours and not jvmedia.duck_path().exists()
    assert not any(action == "paused" for action, _ in _ducks())


@pytest.mark.parametrize("changed", [False, True])
def test_native_spotify_nonmedia_turn_recovers_only_same_uri(players, changed):
    state, acts, urls = players
    state["spotify"], urls["spotify"] = "Playing", "spotify:track:abc"
    m = Media()
    m.pause("native-browser-open")
    if changed:
        urls["spotify"] = "spotify:track:def"
    m.resume("native-browser-open")
    assert acts == ["spotify pause"] + ([] if changed else ["spotify play"])
