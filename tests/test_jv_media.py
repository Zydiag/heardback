"""jv media (bgplay task 4): MPRIS via a fake playerctl at the subprocess boundary. Never the real players."""
import subprocess
from copy import deepcopy

import pytest

from jv import media

ZEN = "firefox.instance_1_658"
PHONE = "kdeconnect.mpris_cce6"


@pytest.fixture(autouse=True)
def _fast_verify(monkeypatch):
    monkeypatch.setattr(media, "VERIFY_S", 0.05)
    monkeypatch.setattr(media, "POLL_S", 0.0)


@pytest.fixture
def pc(monkeypatch):
    """stateful fake playerctl: players = (name, status, title, artist[, url]); play/pause/play-pause change the
    status, next/previous the title (NEXT[title] or "<title> >"). Names in `stuck` ignore every action."""
    calls, players, stuck = [], [], set()
    nxt = {}
    positions = {}
    next_urls = {}

    def run(argv, **kw):
        assert argv[0] == "playerctl" and "shell" not in kw
        calls.append(argv[1:])
        if argv[1:3] == ["-a", "metadata"]:
            out = "".join("\t".join((list(q) + [""])[:5]) + "\n" for q in players)
            return subprocess.CompletedProcess(argv, 0 if players else 1, out, "" if players else "No players found")
        if argv[1] == "-p" and argv[3] == "position":
            positions[argv[2]] = positions.get(argv[2], 0) + 0.2
            return subprocess.CompletedProcess(argv, 0, str(positions[argv[2]]), "")
        if argv[1] == "-p" and argv[2] not in stuck:
            i = next(i for i, q in enumerate(players) if q[0] == argv[2])
            q, act = list(players[i]), argv[3]
            if act in ("play", "pause", "play-pause"):
                q[1] = {"play": "Playing", "pause": "Paused"}.get(act) or ("Paused" if q[1] == "Playing" else "Playing")
            elif act in ("next", "previous"):
                old_title = q[2]
                q[2] = nxt.get(q[2], q[2] + " >")
                if old_title in next_urls:
                    q.extend([""] * max(0, 5 - len(q)))
                    q[4] = next_urls[old_title]
            players[i] = tuple(q)
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(media.subprocess, "run", run)

    def start(*ps, stuck_on=(), next_titles=None, next_urls_after=None):
        players[:] = ps
        stuck.update(stuck_on)
        nxt.update(next_titles or {})
        next_urls.update(next_urls_after or {})
        return calls
    start.players = players
    return start


def _acts(calls):
    return [c for c in calls if c[0] == "-p" and c[-1] != "position"]


@pytest.mark.parametrize("ps,action,player,say", [
    # live 2026-10-31 22:41: Spotify in a Zen tab, "stop the music"
    ([("mpd", "Paused", "Akhar", "Amrinder Gill"), (ZEN, "Playing", "Aarzu", "Noor")], "pause", ZEN, "paused Aarzu"),
    ([(PHONE, "Playing", "Phone song", "")], "pause", PHONE, "paused Phone song"),   # the only one playing
    ([(PHONE, "Paused", "x", ""), ("mpd", "Paused", "Akhar", "")], "play", "mpd", "playing Akhar"),
    ([(ZEN, "Playing", "Aarzu", "")], "next", ZEN, "next: Aarzu >"),
    ([(ZEN, "Playing", "Aarzu", "")], "toggle", ZEN, "paused Aarzu"),
])
def test_media_targets(pc, run_jv, ps, action, player, say):
    calls = pc(*ps)
    code, out, _ = run_jv("media", action, "--json")
    pcact = {"toggle": "pause"}.get(action, action)
    assert code == 0 and _acts(calls) == [["-p", player, pcact]] and out["say"] == say


def test_status_says_title_and_app(pc, run_jv):
    calls = pc(("mpd", "Paused", "Akhar", ""), (ZEN, "Playing", "Aarzu", "Noor, Khan"))
    out = run_jv("media", "status", "--json")[1]
    assert out["say"] == "Aarzu by Noor, Khan, on Zen" and not _acts(calls)


@pytest.mark.parametrize("ps,action,say", [
    ([("mpd", "Paused", "Akhar", "")], "pause", "Nothing is playing."),
    ([], "status", "Nothing is playing."),
    ([(PHONE, "Paused", "x", "")], "play", "There's no music player open."),   # never resumes the phone
])
def test_nothing_to_do(pc, run_jv, ps, action, say):
    calls = pc(*ps)
    code, out, _ = run_jv("media", action, "--json")
    assert code == 1 and out["say"] == say and not _acts(calls)


def test_zen_video_tries_mpris_first(pc, run_jv):   # Spotify has no page <video>: no zen-host needed
    calls = pc(("mpd", "Playing", "Akhar", ""), (ZEN, "Playing", "Aarzu", ""))
    code, out, _ = run_jv("zen", "video", "pause", "--json")
    assert code == 0 and _acts(calls) == [["-p", ZEN, "pause"]] and out["say"] == "paused Aarzu"


def test_zen_video_without_zen_player_falls_back_to_the_page(pc, run_jv):
    pc(("mpd", "Playing", "Akhar", ""))
    code, out, _ = run_jv("zen", "video", "pause", "--json")
    assert code == 1 and "not connected" in out["error"]   # went on to the zen-host DOM path


def test_no_playerctl_says_so(monkeypatch, run_jv):
    def missing(argv, **kw):
        raise FileNotFoundError(argv[0])
    monkeypatch.setattr(media.subprocess, "run", missing)
    code, out, _ = run_jv("media", "pause", "--json")
    assert code == 1 and out["say"] == "Media control isn't installed."


# duck (2026-10-31): the voice paused these for the listen; they still count as playing for the command
def test_ducked_player_counts_as_playing_for_stop(pc, run_jv):   # "stop the music" said over music
    media.duck_path().write_text(ZEN + "\n")
    calls = pc(("mpd", "Paused", "Akhar", ""), (ZEN, "Paused", "Retro Jazz", ""))
    code, out, _ = run_jv("media", "pause", "--json")
    assert code == 0 and _acts(calls) == [["-p", ZEN, "pause"]] and out["say"] == "paused Retro Jazz"
    assert not media.duck_path().exists()   # claimed: the voice won't resume it


@pytest.mark.parametrize("action,acts,say", [
    ("next", [["-p", ZEN, "next"], ["-p", ZEN, "play"]], "next: Retro Jazz >"),   # skipped while paused: start it
    ("toggle", [["-p", ZEN, "pause"]], "paused Retro Jazz"),            # never play-pause a ducked player
])
def test_ducked_player_actions(pc, run_jv, action, acts, say):
    media.duck_path().write_text(ZEN + "\n")
    calls = pc((ZEN, "Paused", "Retro Jazz", ""))
    code, out, _ = run_jv("media", action, "--json")
    assert code == 0 and _acts(calls) == acts and out["say"] == say and not media.duck_path().exists()


def test_status_of_ducked_player_stays_unclaimed(pc, run_jv):   # "what's playing": the music comes back after
    media.duck_path().write_text(ZEN + "\n")
    calls = pc((ZEN, "Paused", "Retro Jazz", "Noor"))
    out = run_jv("media", "status", "--json")[1]
    assert out["say"] == "Retro Jazz by Noor, on Zen" and not _acts(calls) and media.duck_path().exists()


# mediafix (live 2026-11-01 09:45): the reply comes from the player AFTER the action, never the stale read before it
def test_changed_item_does_not_verify_resume(pc, run_jv):   # Zen's one MPRIS player: stale Shorts title, Spotify starts
    calls = pc((ZEN, "Paused", "Mahakal Shorts", ""))

    def run(argv, **kw):
        r = real(argv, **kw)
        if argv[1:3] == ["-p", ZEN]:
            pc.players[0] = (ZEN, "Playing", "Bad Liar", "", "https://open.spotify.com/")
        return r
    real = media.subprocess.run
    media.subprocess.run = run
    out = run_jv("media", "play", "--json")[1]
    assert out["verification"] == "playback unverified" and out["after"]["title"] == "Bad Liar"


@pytest.mark.parametrize("action,status,say", [
    ("play", "Paused", "I couldn't verify Zen playing."), ("pause", "Playing", "I couldn't verify Zen paused."),
    ("toggle", "Paused", "I couldn't verify Zen playing."),
])
def test_unverified_action_fails(pc, run_jv, action, status, say):
    pc((ZEN, status, "Aarzu", ""), stuck_on={ZEN})
    code, out, _ = run_jv("media", action, "--json")
    assert code == 1 and out["say"] == say


def test_next_without_a_new_track_fails_with_before_after_evidence(pc, run_jv):
    pc((ZEN, "Playing", "Aarzu", ""), stuck_on={ZEN})
    code, out, _ = run_jv("media", "next", "--json")
    assert code == 1 and out["say"] == "I couldn't confirm Zen changed tracks."
    assert out["track_changed"] is False
    assert out["before"]["title"] == out["after"]["title"] == "Aarzu"
    assert out["before"]["status"] == out["after"]["status"] == "Playing"


def test_next_uses_metadata_when_spotify_reports_its_generic_page(pc, run_jv):
    calls = pc((ZEN, "Playing", "Before", "Artist", "https://open.spotify.com/"),
               next_titles={"Before": "After"})
    code, out, _ = run_jv("media", "next", "--json")
    assert code == 0 and out["track_changed"] is True
    assert out["before"]["url"] == out["after"]["url"] == "https://open.spotify.com/"
    assert out["after"]["title"] == "After" and _acts(calls) == [["-p", ZEN, "next"]]


@pytest.mark.parametrize("action", ["next", "previous"])
@pytest.mark.parametrize("url", [
    "https://open.spotify.com/",
    "https://open.spotify.com/intl-de/",
    "https://open.spotify.com/playlist/37i9dQZF1DX",
    "https://open.spotify.com/intl-de/playlist/37i9dQZF1DX",
    "https://open.spotify.com/album/album-id",
    "https://open.spotify.com/intl-es/album/album-id",
    "https://open.spotify.com/collection/tracks",
    "https://open.spotify.com/intl-de/collection/tracks",
])
def test_spotify_non_item_pages_use_metadata_identity(pc, run_jv, action, url):
    calls = pc((ZEN, "Playing", "Before", "Artist", url), next_titles={"Before": "After"})
    code, out, _ = run_jv("media", action, "--json")
    assert code == 0 and out["track_changed"] is True
    assert out["before"]["url"] == out["after"]["url"] == url
    assert out["after"]["title"] == "After"
    assert _acts(calls) == [["-p", ZEN, action]]


@pytest.mark.parametrize("action", ["next", "previous"])
def test_spotify_non_item_page_with_unchanged_metadata_fails(pc, run_jv, action):
    url = "https://open.spotify.com/intl-de/playlist/playlist-id"
    calls = pc((ZEN, "Playing", "Before", "Artist", url), next_titles={"Before": "Before"})
    code, out, _ = run_jv("media", action, "--json")
    assert code == 1 and out["track_changed"] is False
    assert "couldn't confirm" in out["say"] and _acts(calls) == [["-p", ZEN, action]]


@pytest.mark.parametrize("url", [
    "https://open.spotify.com/track/track-id",
    "https://open.spotify.com/intl-de/track/track-id",
    "https://open.spotify.com/episode/episode-id",
    "https://open.spotify.com/intl-es/episode/episode-id",
    "https://music.test/track/item-id",
])
def test_spotify_item_and_unrelated_urls_remain_authoritative(pc, run_jv, url):
    calls = pc((ZEN, "Playing", "Before", "Artist", url), next_titles={"Before": "After"})
    code, out, _ = run_jv("media", "next", "--json")
    assert code == 1 and out["track_changed"] is False
    assert out["before"]["url"] == out["after"]["url"] == url
    assert _acts(calls) == [["-p", ZEN, "next"]]


@pytest.mark.parametrize("action,before_url,after_url", [
    ("next", "https://open.spotify.com/track/track-one", "https://open.spotify.com/track/track-two"),
    ("previous", "https://open.spotify.com/intl-de/episode/episode-one",
     "https://open.spotify.com/intl-de/episode/episode-two"),
])
def test_spotify_item_url_change_remains_authoritative(pc, run_jv, action, before_url, after_url):
    calls = pc((ZEN, "Playing", "Same title", "Artist", before_url),
               next_titles={"Same title": "Same title"}, next_urls_after={"Same title": after_url})
    code, out, _ = run_jv("media", action, "--json")
    assert code == 0 and out["track_changed"] is True
    assert out["before"]["url"] == before_url and out["after"]["url"] == after_url
    assert _acts(calls) == [["-p", ZEN, action]]


def test_next_waits_for_delayed_spotify_metadata(pc, monkeypatch, run_jv):
    pc((ZEN, "Playing", "Before", "Artist", "https://open.spotify.com/"), stuck_on={ZEN})
    real = media.subprocess.run
    reads = 0

    def run(argv, **kwargs):
        nonlocal reads
        result = real(argv, **kwargs)
        if argv[1:3] == ["-a", "metadata"]:
            reads += 1
            if reads == 4:
                pc.players[0] = (ZEN, "Playing", "After", "Artist", "https://open.spotify.com/")
        return result

    monkeypatch.setattr(media.subprocess, "run", run)
    code, out, _ = run_jv("media", "next", "--json")
    assert code == 0 and out["after"]["title"] == "After" and reads >= 4


def test_generic_spotify_url_with_mixed_artist_evidence_is_unknown(pc, monkeypatch, run_jv):
    pc((ZEN, "Playing", "Before", "Artist", "https://open.spotify.com/"),
       next_titles={"Before": "After"})
    real = media.subprocess.run

    def run(argv, **kwargs):
        result = real(argv, **kwargs)
        if argv[1:3] == ["-p", ZEN]:
            pc.players[0] = (ZEN, "Playing", "After", "", "https://open.spotify.com/")
        return result

    monkeypatch.setattr(media.subprocess, "run", run)
    code, out, _ = run_jv("media", "next", "--json")
    assert code == 1 and out["track_changed"] is None
    assert "couldn't verify whether" in out["say"]


def test_generic_spotify_url_to_missing_url_is_unknown(pc, monkeypatch, run_jv):
    pc((ZEN, "Playing", "Before", "Artist", "https://open.spotify.com/"),
       next_titles={"Before": "After"})
    real = media.subprocess.run

    def run(argv, **kwargs):
        result = real(argv, **kwargs)
        if argv[1:3] == ["-p", ZEN]:
            pc.players[0] = (ZEN, "Playing", "After", "Artist", "")
        return result

    monkeypatch.setattr(media.subprocess, "run", run)
    code, out, _ = run_jv("media", "next", "--json")
    assert code == 1 and out["track_changed"] is None
    assert "couldn't verify whether" in out["say"]


def test_previous_says_the_new_title(pc, run_jv):
    pc(("mpd", "Playing", "Akhar", ""), next_titles={"Akhar": "Ishq"})
    assert run_jv("media", "previous", "--json")[1]["say"] == "previous: Ishq"


def test_next_verifies_url_change_before_saying_it_skipped(pc, run_jv):
    calls = pc((ZEN, "Playing", "Same title", "Artist", "https://music.test/track/1"),
               next_urls_after={"Same title": "https://music.test/track/2"})
    code, out, _ = run_jv("media", "next", "--json")
    assert code == 0 and out["track_changed"] is True
    assert out["after"]["url"] == "https://music.test/track/2" and _acts(calls) == [["-p", ZEN, "next"]]


def test_url_identity_takes_precedence_over_changed_title(pc, run_jv):
    calls = pc((ZEN, "Playing", "Same title", "Artist", "https://music.test/track/1"),
               next_titles={"Same title": "Different title"})
    code, out, _ = run_jv("media", "next", "--json")
    assert code == 1 and out["track_changed"] is False
    assert out["before"]["url"] == out["after"]["url"] == "https://music.test/track/1"
    assert _acts(calls) == [["-p", ZEN, "next"]]


def test_unknown_track_identity_sends_no_skip(pc, run_jv):
    calls = pc((ZEN, "Playing", "", "", ""))
    code, out, _ = run_jv("media", "next", "--json")
    assert code == 1 and "can't verify the current track" in out["say"]
    assert not _acts(calls)


@pytest.mark.parametrize("said", ["next song", "play next song", "play the next music"])
def test_next_fast_phrases_reach_real_cli_parser(pc, run_jv, said):
    from heardback.brain import catalogue, router
    from jv.cli import all_verbs, catalogue as jv_catalogue

    verbs = deepcopy(all_verbs())
    next(v for v in verbs if v.name == "tmux.select").args["window"]["choices"] = lambda: []
    cat = catalogue._index(jv_catalogue(verbs))
    route = router.Router(cat).route(said)
    assert route.verb == "media" and route.args == {"action": "next"}
    argv = catalogue.build_argv(cat[route.verb], route.args, "--json")
    assert argv == ["jv", "media", "--json", "--", "next"]
    pc((ZEN, "Playing", "Track", "Artist", ""))
    code, out, _ = run_jv(*argv[1:])
    assert code == 0 and out["track_changed"] is True and out["after"]["title"] == "Track >"


def test_ducked_skip_does_not_resume_if_candidate_changes_before_play(monkeypatch, pc, run_jv):
    from jv import media

    media.duck_path().write_text(f"{ZEN}\t\tRetro Jazz\n")
    calls = pc((ZEN, "Paused", "Retro Jazz", ""))
    states = iter([
        {"name": ZEN, "status": "Paused", "title": "Selected track", "artist": "", "url": "", "ducked": False},
        {"name": ZEN, "status": "Paused", "title": "Different track", "artist": "", "url": "", "ducked": False},
    ])
    monkeypatch.setattr(media, "_current", lambda name: next(states))
    code, out, _ = run_jv("media", "next", "--json")
    assert code == 1 and "didn't resume it" in out["say"]
    assert _acts(calls) == [["-p", ZEN, "next"]]


# mediafix task 2: "play music in spotify" -> the open.spotify.com tab through the add-on, verified on the player
from test_zen import fake_zen  # noqa: E402,F401

SP_URL = "https://open.spotify.com/"


def _sp_tabs(spotify=True):
    t = [{"id": 7, "windowId": 1, "title": "Shorts", "url": "https://www.youtube.com/shorts/x", "active": True}]
    return t + [{"id": 8, "windowId": 1, "title": "Spotify", "url": SP_URL + "playlist/1"}] * spotify


def _zen_ops(fake_zen, pc, starts=True, video=None):
    def vid(a):
        if starts:
            pc.players[0] = (ZEN, "Playing", "Bad Liar", "Imagine Dragons", SP_URL)
        return video or {"ok": True, "paused": False}
    tabs = _sp_tabs()
    def listing(a):
        if starts and pc.players and pc.players[0][1] == "Playing":
            tabs[-1]["audible"] = True
        return {"ok": True, "current_window": 1, "tabs": tabs}
    return fake_zen({"list": listing, "video": vid})


def test_play_spotify_starts_the_spotify_tab(fake_zen, pc, run_jv):   # live 09:45: stale Shorts title, Spotify tab
    pc((ZEN, "Paused", "Mahakal Shorts", ""))
    ops = _zen_ops(fake_zen, pc)
    code, out, _ = run_jv("media", "--target", "spotify", "play", "--json")
    assert code == 0 and out["say"] == "playing Spotify"
    assert {"op": "video", "args": {"tabId": 8, "action": "play"}} in ops
    assert not any(o["op"] == "activate" for o in ops)   # Zen stays in the background (user)


def test_play_spotify_unverified_fails(fake_zen, pc, run_jv):
    pc((ZEN, "Paused", "Mahakal Shorts", ""))
    _zen_ops(fake_zen, pc, starts=False)
    code, out, _ = run_jv("media", "--target", "spotify", "play", "--json")
    assert code == 1 and out["say"] == "Spotify accepted the command, but I couldn't verify playback."


def test_play_spotify_failed_page_control_never_falls_back_to_mpris(fake_zen, pc, run_jv):   # 0.3.2: no Spotify button support
    calls = pc((ZEN, "Paused", "Bad Liar", "", SP_URL))
    _zen_ops(fake_zen, pc, starts=False, video={"ok": False, "reason": "no video on this page"})
    code, out, _ = run_jv("media", "--target", "spotify", "play", "--json")
    assert code == 1 and ["-p", ZEN, "play"] not in calls and out["dispatched"]


def test_play_spotify_without_a_tab_asks_then_opens_it(fake_zen, pc, run_jv):
    pc()
    ops = fake_zen({"list": {"ok": True, "current_window": 1, "current_tab": 7, "creation_guard": True,
                             "tabs": _sp_tabs(spotify=False)}})
    code, out, _ = run_jv("media", "--target", "spotify", "play", "--json")
    assert code == 3 and out["confirm"] == "Open open.spotify.com in a new tab?" and not any(o["op"] == "create" for o in ops)
    code, out, _ = run_jv("media", "--target", "spotify", "play", "--source-revision", out["source_revision"], "--yes", "--json")
    assert code == 0 and out["opened"] and out["observed"] is False and out["say"] == "I couldn't confirm that."
    assert {"op": "create", "args": {"url": SP_URL.rstrip("/"), "windowId": 1, "expectedCurrentTab": 7}} in ops
    assert not any(o["op"] == "activate" for o in ops)


# mediafix task 4: the voice path, router -> catalogue.build_argv -> the real jv parser (6c0b090 shipped because
# tests only called jv directly)
@pytest.mark.parametrize("said,argv,say", [
    ("Play music in Spotify.", ["media", "--target", "spotify", "--json", "--", "play"], "playing Spotify"),
    ("play some music on spotify", ["media", "--target", "spotify", "--json", "--", "play"], "playing Spotify"),
    ("Play Spotify", ["media", "--target", "spotify", "--json", "--", "play"], "playing Spotify"),
    ("resume spotify", ["media", "--target", "spotify", "--json", "--", "play"], "playing Spotify"),
    ("stop the music", ["media", "--json", "--", "pause"], "paused Mahakal Shorts"),
])
def test_voice_path_runs_through_the_real_parser(fake_zen, pc, run_jv, said, argv, say):
    from heardback.brain import catalogue, router
    from jv.cli import all_verbs, catalogue as jv_catalogue
    cat = catalogue._index(jv_catalogue(all_verbs()))
    r = router.Router(cat).route(said)
    built = catalogue.build_argv(cat[r.verb], r.args, "--json")
    assert built == ["jv", *argv]
    pc((ZEN, "Playing" if "pause" in argv else "Paused", "Mahakal Shorts", ""))
    _zen_ops(fake_zen, pc)
    code, out, _ = run_jv(*built[1:])
    assert code == 0 and out["say"] == say


@pytest.mark.parametrize("said", ["Play music", "play music in zen"])
def test_music_intent_router_parser_does_not_resume_unknown_video(pc, run_jv, said):
    from heardback.brain import catalogue, router
    from jv.cli import catalogue as jv_catalogue
    cat = catalogue._index(jv_catalogue(media.VERBS))
    route = router.Router(cat).route(said)
    assert route and route.args["intent"] == "music"
    calls = pc((ZEN, "Paused", "Neuroscience", "", "https://www.youtube.com/watch?v=lecture"))
    argv = catalogue.build_argv(cat[route.verb], route.args, "--json")
    rc, data, _ = run_jv(*argv[1:])
    assert rc == 2 and data["clarification"] == "music_source" and not _acts(calls)


def test_music_creation_guard_excludes_fuzzy_fast_matching():
    from heardback.brain import catalogue, router
    from jv.cli import catalogue as jv_catalogue
    cat = catalogue._index(jv_catalogue(media.VERBS))
    assert router.Router(cat).route("play musc") is None


@pytest.mark.parametrize("url,known", [
    ("https://open.spotify.com/track/abc", True),
    ("https://open.spotify.com/episode/abc", False),
    ("https://open.spotify.com/playlist/abc", False),
    ("https://open.spotify.com.evil.test/track/abc", False),
    ("https://www.youtube.com/watch?v=music", False),
])
def test_only_exact_supported_music_identity_qualifies(url, known):
    assert media._known_music({"url": url, "title": "music", "artist": "Artist"}) is known


@pytest.mark.parametrize("changed", [None, {"name": ZEN, "status": "Playing", "title": "Other", "artist": "", "url": "https://example.com/other"}])
def test_audible_tab_cannot_validate_vanished_or_changed_mpris(monkeypatch, changed):
    from jv import zen
    before = {"name": ZEN, "status": "Paused", "title": "Original", "artist": "", "url": "https://example.com/item"}
    tab = {"id": 1, "url": before["url"], "audible": True}
    monkeypatch.setattr(media, "_current", lambda name: changed)
    monkeypatch.setattr(media, "_position", lambda name: 2.0)
    monkeypatch.setattr(zen, "tabs", lambda: [tab])
    after, samples, reason = media._verify_play(ZEN, media._track_identity(before), tab)
    assert reason is None and samples


@pytest.mark.parametrize("current", [
    {"id": 2, "url": "https://example.com/item", "audible": True},
    {"id": 1, "url": "https://example.com/other", "audible": True},
    {"id": 1, "url": "https://example.com/item", "audible": True, "muted": True},
])
def test_page_play_requires_the_exact_unmuted_selected_tab(monkeypatch, current):
    from jv import zen
    selected = {"id": 1, "url": "https://example.com/item"}
    monkeypatch.setattr(zen, "tabs", lambda: [current])
    _, samples, reason = media._verify_play(None, None, selected)
    assert reason is None and samples


@pytest.mark.parametrize("failure", ["nonzero", "timeout", "missing"])
def test_failed_command_cannot_claim_success_or_restore_duck(monkeypatch, run_jv, pc, failure):
    pc((ZEN, "Paused", "Original", ""))
    media.duck_path().write_text(ZEN + "\n")
    real = media.subprocess.run
    def run(argv, **kwargs):
        if argv[-1] == "play":
            if failure == "timeout": raise subprocess.TimeoutExpired(argv, 3)
            if failure == "missing": raise FileNotFoundError(argv[0])
            return subprocess.CompletedProcess(argv, 1, "", "rejected")
        return real(argv, **kwargs)
    monkeypatch.setattr(media.subprocess, "run", run)
    rc, data, _ = run_jv("media", "play", "--json")
    assert rc == 1 and not media.duck_path().exists()


def test_two_plausible_players_clarify_instead_of_ranking(pc, run_jv):
    calls = pc((ZEN, "Paused", "Video", ""), ("mpd", "Paused", "Song", ""))
    rc, data, _ = run_jv("media", "play", "--json")
    assert rc == 2 and not _acts(calls)


@pytest.mark.parametrize("action", ["next", "previous"])
def test_explicit_spotify_skip_ignores_other_playing_app(pc, run_jv, action):
    calls = pc((ZEN, "Paused", "Before", "Artist", SP_URL), ("mpd", "Playing", "Other", ""))
    rc, data, _ = run_jv("media", action, "--target", "spotify", "--json")
    assert rc == 0 and data["after"]["title"] == "Before >"
    assert _acts(calls) == [["-p", ZEN, action]]


@pytest.mark.parametrize("target,player", [("zen", ZEN), ("mpd", "mpd")])
def test_explicit_app_selects_only_that_player(pc, run_jv, target, player):
    calls = pc((ZEN, "Paused", "Browser", ""), ("mpd", "Paused", "Music", ""))
    rc, data, _ = run_jv("media", "next", "--target", target, "--json")
    assert rc == 0 and data["player"] == player and _acts(calls) == [["-p", player, "next"]]


@pytest.mark.parametrize("args", [
    ["--target", "spotify"], ["--player", "gone"], ["--target", "mpd", "--player", ZEN],
])
def test_missing_or_mismatched_explicit_selection_never_falls_back(pc, run_jv, args):
    calls = pc((ZEN, "Playing", "Video", "", "https://youtube.com/watch?v=x"), ("mpd", "Paused", "Music", ""))
    rc, data, _ = run_jv("media", "next", *args, "--json")
    assert rc == 1 and not _acts(calls)


def test_exact_candidate_can_select_paused_mpd_over_playing_zen(pc, run_jv):
    calls = pc((ZEN, "Playing", "Video", ""), ("mpd", "Paused", "Music", ""))
    rc, data, _ = run_jv("media", "next", "--player", "mpd", "--json")
    assert rc == 0 and data["player"] == "mpd" and _acts(calls) == [["-p", "mpd", "next"]]


def test_player_clarification_retains_action_and_exact_candidates(pc, run_jv):
    calls = pc((ZEN, "Paused", "Video", ""), ("mpd", "Paused", "Music", ""))
    rc, data, _ = run_jv("media", "previous", "--json")
    assert rc == 2 and data["clarification"] == "media_player" and data["action"] == "previous"
    assert data["candidates"] == [ZEN, "mpd"] and data["player_labels"] == {ZEN: "Zen", "mpd": "mpd"}
    assert "Zen or mpd" in data["say"] and not _acts(calls)


@pytest.mark.parametrize("said,action,target", [
    ("next in Spotify", "next", "spotify"), ("Skip Next in Spotify", "next", "spotify"),
    ("play next song on Zen", "next", "zen"), ("previous song in MPD", "previous", "mpd"),
    ("play the previous song on Spotify", "previous", "spotify"), ("play next", "next", None),
])
def test_targeted_skip_voice_path_preserves_target_in_cli(pc, run_jv, said, action, target):
    from heardback.brain import catalogue, router
    from jv.cli import catalogue as jv_catalogue
    cat = catalogue._index(jv_catalogue(media.VERBS))
    route = router.Router(cat).route(said)
    assert route and route.args == {"action": action, **({"target": target} if target else {})}
    calls = pc((ZEN, "Playing", "Before", "Artist", SP_URL)) if target != "mpd" else pc(("mpd", "Playing", "Before", ""))
    rc, data, _ = run_jv(*catalogue.build_argv(cat[route.verb], route.args, "--json")[1:])
    assert rc == 0 and data["track_changed"] and len(_acts(calls)) == 1


def test_spotify_unverified_dispatch_path_survives_trace_truncation(fake_zen, pc, run_jv, monkeypatch):
    from jv import trace
    calls = pc((ZEN, "Paused", "Other page", ""))
    reply = {"ok": True, "via": "button", "clicked": True, "before_label": "Play", "after_label": "Play"}
    ops = _zen_ops(fake_zen, pc, starts=False, video=reply)
    events = []
    monkeypatch.setattr(trace, "emit", lambda *args, **fields: events.append((args, fields)))
    monkeypatch.setenv("HEARDBACK_TURN", "spotify-evidence")
    rc, data, _ = run_jv("media", "play", "--target", "spotify", "--json")
    assert rc == 1 and data["dispatch"]["via"] == "button" and data["dispatch"]["clicked"]
    dispatch, verification = events
    assert dispatch[0] == ("jv", "media_dispatch", "spotify-evidence")
    assert dispatch[1]["dispatch"] == reply and verification[1]["last"]["tab"]["audible"] is False
    assert len([o for o in ops if o["op"] == "video"]) == 1 and not _acts(calls)


def test_spotify_dispatch_error_keeps_diagnostics_without_second_action(fake_zen, pc, run_jv):
    calls = pc((ZEN, "Paused", "Other page", ""))
    ops = _zen_ops(fake_zen, pc, starts=False, video={"ok": False, "reason": "control disabled", "diag": "via=button disabled=true"})
    rc, data, _ = run_jv("media", "play", "--target", "spotify", "--json")
    assert rc == 1 and data["dispatch_details"]["diag"] == "via=button disabled=true"
    assert len([o for o in ops if o["op"] == "video"]) == 1 and not _acts(calls)


# Desktop selection and approval binding: fake playerctl/bridge only.
NATIVE = ("spotify", "Paused", "Desktop song", "Artist", "spotify:track:abc")
SITE = (ZEN, "Paused", "Site song", "Artist", "https://open.spotify.com/track/site")


def _no_bridge(monkeypatch):
    from jv import zen
    def forbidden(*args, **kwargs):
        raise AssertionError("native Spotify must not query the browser")
    monkeypatch.setattr(zen, "tabs", forbidden)
    monkeypatch.setattr(zen, "_open_check", forbidden)
    monkeypatch.setattr(zen, "call", forbidden)


@pytest.mark.parametrize("args", [["--intent", "music"], ["--target", "spotify"],
                                  ["--target", "spotify", "--source", "desktop"]])
def test_start_music_prefers_native_without_bridge(pc, run_jv, monkeypatch, args):
    calls = pc(NATIVE, SITE, ("mpd", "Paused", "Other song", "Artist"))
    _no_bridge(monkeypatch)
    rc, data, _ = run_jv("media", "play", *args, "--json")
    assert rc == 0 and data["player"] == "spotify"
    assert _acts(calls) == [["-p", "spotify", "play"]]
    assert data["source_revision"].startswith("media:desktop:")


@pytest.mark.parametrize("action", ["play", "pause", "toggle", "next", "previous", "status"])
@pytest.mark.parametrize("url", ["spotify:track:abc", "https://open.spotify.com/track/abc"])
def test_native_spotify_all_controls_bypass_zen(pc, run_jv, monkeypatch, action, url):
    calls = pc(("spotify", "Playing", "Before", "Artist", url), SITE,
               next_urls_after={"Before": url.replace("abc", "def")})
    _no_bridge(monkeypatch)
    rc, data, _ = run_jv("media", action, "--target", "spotify", "--json")
    assert rc == 0 and data["player"] == "spotify"
    assert all(c[1] == "spotify" for c in _acts(calls))
    if action in ("next", "previous"):
        assert data["track_changed"] is True


@pytest.mark.parametrize("action", ["play", "pause", "next", "previous", "status"])
def test_current_controls_keep_unique_active_site(pc, run_jv, action):
    calls = pc(NATIVE, (ZEN, "Playing", "Site song", "Artist", SITE[4]),
               next_urls_after={"Site song": SITE[4] + "next"})
    rc, data, _ = run_jv("media", action, "--intent", "resume", "--json")
    assert rc == 0 and data["player"] == ZEN
    assert all(c[1] == ZEN for c in _acts(calls))


@pytest.mark.parametrize("args", [[], ["--intent", "resume"]])
def test_two_active_players_never_choose_idle_default(pc, run_jv, args):
    calls = pc(("spotify", "Playing", "Native", "Artist", NATIVE[4]),
               (ZEN, "Playing", "Site", "Artist", SITE[4]))
    rc, data, _ = run_jv("media", "next", *args, "--json")
    assert rc == 2 and set(data["candidates"]) == {"spotify", ZEN} and not _acts(calls)


def test_idle_app_beats_equivalent_site_but_not_unrelated_player(pc, run_jv):
    calls = pc(NATIVE, SITE, next_urls_after={"Desktop song": "spotify:track:def"})
    rc, data, _ = run_jv("media", "next", "--json")
    assert rc == 0 and data["player"] == "spotify"
    calls.clear()
    pc(NATIVE, SITE, ("mpd", "Paused", "Other", "Artist"))
    rc, data, _ = run_jv("media", "play", "--json")
    assert rc == 2 and data["candidates"] == ["spotify", "mpd"] and not _acts(calls)


@pytest.mark.parametrize("args", [["--player", ZEN], ["--target", "spotify", "--player", ZEN],
                                  ["--target", "spotify", "--source", "site", "--player", ZEN]])
def test_exact_site_player_overrides_native(pc, run_jv, args):
    calls = pc(NATIVE, SITE, next_urls_after={"Site song": SITE[4] + "next"})
    rc, data, _ = run_jv("media", "next", *args, "--json")
    assert rc == 0 and data["player"] == ZEN and _acts(calls) == [["-p", ZEN, "next"]]


def test_explicit_site_play_uses_only_page_control(fake_zen, pc, run_jv):
    calls = pc((ZEN, "Paused", "Old", ""), NATIVE)
    ops = _zen_ops(fake_zen, pc)
    rc, data, _ = run_jv("media", "play", "--target", "spotify", "--source", "site", "--json")
    assert rc == 0 and data["source_revision"].startswith("media:site:")
    assert any(op["op"] == "video" for op in ops) and not _acts(calls)


@pytest.mark.parametrize("args", [["--intent", "music"], ["--target", "spotify"]])
def test_multiple_native_instances_require_exact_answer(pc, run_jv, args):
    calls = pc(NATIVE, ("spotify.instance123", "Playing", "Other", "Artist", "spotify:track:def"))
    rc, data, _ = run_jv("media", "play", *args, "--json")
    assert rc == 2 and data["clarification"] == "media_player"
    assert data["candidates"] == ["spotify", "spotify.instance123"] and not _acts(calls)


@pytest.mark.parametrize("name", ["notspotify", "spotify-fake", "spotify.other", "chromium.spotify"])
def test_arbitrary_player_names_never_become_native(pc, run_jv, name):
    calls = pc((name, "Paused", "Track", "Artist", NATIVE[4]))
    rc, data, _ = run_jv("media", "play", "--target", "spotify", "--source", "desktop", "--json")
    assert rc == 1 and not _acts(calls)


@pytest.mark.parametrize("url,title,known", [("spotify:track:abc", "Song", True),
    ("spotify:episode:abc", "Podcast", False), ("spotify:track:abc", "", False),
    ("", "Song", False), ("https://open.spotify.com/track/abc", "", False)])
def test_native_music_requires_track_identity_and_metadata(url, title, known):
    assert media._known_music({"name": "spotify", "url": url, "title": title, "artist": "Artist"}) is known


@pytest.mark.parametrize("change", ["arrive", "disappear", "action", "source", "duplicate"])
def test_native_preflight_binding_cannot_redirect_execution(pc, run_jv, change):
    calls = pc(NATIVE, ("mpd", "Paused", "Other", "Artist"))
    args = ["--target", "spotify", "--source", "desktop"]
    rc, checked, _ = run_jv("media", "play", *args, "--dry-run", "--json")
    assert rc == 0 and not _acts(calls)
    action = "play"
    if change in ("arrive", "duplicate"):
        pc.players.append(("spotify.instance123", "Paused", "Other", "Artist", "spotify:track:def"))
    elif change == "disappear":
        pc.players.pop(0)
    elif change == "action":
        action = "pause"
    else:
        args = ["--target", "spotify", "--source", "site"]
    rc, data, _ = run_jv("media", action, *args, "--source-revision", checked["source_revision"], "--yes", "--json")
    assert rc in (1, 2) and not _acts(calls)


def test_confirmed_site_cannot_switch_to_arriving_native(fake_zen, pc, run_jv):
    calls = pc()
    ops = fake_zen({"list": {"ok": True, "current_window": 1, "current_tab": 7, "creation_guard": True,
                              "tabs": _sp_tabs(spotify=False)}})
    rc, checked, _ = run_jv("media", "play", "--target", "spotify", "--json")
    assert rc == 3
    pc.players.append(NATIVE)
    rc, data, _ = run_jv("media", "play", "--target", "spotify", "--source-revision", checked["source_revision"],
                          "--yes", "--json")
    assert rc == 1 and not _acts(calls) and not any(o["op"] in {"create", "video"} for o in ops)


@pytest.mark.parametrize("dry_run,action,claimed", [(False, "play", True), (True, "play", False), (False, "status", False)])
def test_cli_preflight_failure_preserves_duck_policy(pc, run_jv, dry_run, action, claimed):
    calls = pc((ZEN, "Paused", "Current", "Artist"))
    media.duck_path().write_text(ZEN + "\n")
    rc, data, _ = run_jv("media", action, "--player", "gone", *(["--dry-run"] if dry_run else []), "--json")
    assert rc == 1 and media.duck_path().exists() is not claimed and not _acts(calls)


@pytest.mark.parametrize("said,intent", [("play music", "music"), ("resume music", "resume"), ("unpause music in zen", "resume")])
def test_start_and_resume_phrases_are_distinct(said, intent):
    from heardback.brain import catalogue, router
    from jv.cli import catalogue as jv_catalogue
    cat = catalogue._index(jv_catalogue(media.VERBS))
    route = router.Router(cat).route(said)
    assert route.args["intent"] == intent


@pytest.mark.parametrize("said,source", [("play spotify app", "desktop"), ("pause desktop spotify", "desktop"),
                                        ("resume spotify website", "site"), ("play spotify in zen", "site")])
def test_explicit_source_phrases_survive_argv_parser(pc, run_jv, said, source):
    from heardback.brain import catalogue, router
    from jv.cli import catalogue as jv_catalogue
    cat = catalogue._index(jv_catalogue(media.VERBS))
    route = router.Router(cat).route(said)
    assert route.args["source"] == source and route.args["target"] == "spotify"
    calls = pc(NATIVE)
    argv = catalogue.build_argv(cat["media"], route.args, "--json", "--dry-run")
    assert argv[argv.index("--source") + 1] == source
    if source == "desktop":
        assert run_jv(*argv[1:])[0] == 0 and not _acts(calls)


def test_new_music_does_not_choose_between_two_active_players(pc, run_jv):
    calls = pc(NATIVE, (ZEN, "Playing", "Site song", "Artist", SITE[4]),
               ("mpd", "Playing", "Other song", "Artist"))
    rc, data, _ = run_jv("media", "play", "--intent", "music", "--json")
    assert rc == 2 and data["candidates"] == [ZEN, "mpd"] and not _acts(calls)


@pytest.mark.parametrize("failure", ["position", "unchanged", "command"])
def test_native_uncertain_playback_never_falls_back_or_recovers(pc, run_jv, monkeypatch, failure):
    calls = pc(NATIVE, SITE, stuck_on={"spotify"} if failure == "unchanged" else ())
    media.duck_path().write_text("spotify\n")
    original = media._pc
    def control(*args, **kwargs):
        if args == ("-p", "spotify", "position") and failure == "position":
            from jv.verb import JvError
            raise JvError(1, "unsupported position")
        if args == ("-p", "spotify", "play") and failure == "command":
            from jv.verb import JvError
            raise JvError(1, "rejected")
        return original(*args, **kwargs)
    monkeypatch.setattr(media, "_pc", control)
    _no_bridge(monkeypatch)
    rc, data, _ = run_jv("media", "play", "--target", "spotify", "--json")
    assert rc == 1 and not media.duck_path().exists()
    assert all(c[1] == "spotify" for c in _acts(calls))


def test_site_toggle_keeps_ducked_state_until_selected_page_command(fake_zen, pc, run_jv):
    calls = pc((ZEN, "Paused", "Song", "Artist", SP_URL + "playlist/1"), NATIVE)
    media.duck_path().write_text(ZEN + "\n")
    seen = []
    def video(args):
        seen.append(args)
        return {"ok": True, "paused": True}
    fake_zen({"list": {"ok": True, "current_window": 1, "tabs": _sp_tabs()}, "video": video})
    rc, data, _ = run_jv("media", "toggle", "--target", "spotify", "--source", "site", "--json")
    assert rc == 0 and seen == [{"tabId": 8, "action": "pause"}] and not _acts(calls)


def test_ducked_native_next_preserves_track_change_and_same_item_resume(pc, run_jv):
    media.duck_path().write_text("spotify\tspotify:track:abc\tBefore\n")
    calls = pc(("spotify", "Paused", "Before", "Artist", "spotify:track:abc"), SITE,
               next_urls_after={"Before": "spotify:track:def"})
    rc, data, _ = run_jv("media", "next", "--json")
    assert rc == 0 and data["player"] == "spotify" and data["track_changed"] is True
    assert data["after"]["url"] == "spotify:track:def" and data["after"]["status"] == "Playing"
    assert _acts(calls) == [["-p", "spotify", "next"], ["-p", "spotify", "play"]]
