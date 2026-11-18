"""`jv media`: play / pause / skip what's playing (Zen tabs incl. Spotify, mpd) through MPRIS via playerctl.

Fixed argv, no shell. Player names come from `playerctl` itself.
"""
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from jv.verb import JvError, Verb, runtime_path

TARGETS = ["spotify", "zen", "mpd"]
SOURCES = ["desktop", "site"]
REVISION_RE = r"media:(?:(?:desktop|player):[0-9a-f]{64}|site:[0-9a-f]{64}:[0-9a-f]{64})"
ACTIONS = ["play", "pause", "toggle", "next", "previous", "status"]
PC = {"play": "play", "pause": "pause", "toggle": "play-pause", "next": "next", "previous": "previous"}
FMT = "{{playerInstance}}\t{{status}}\t{{title}}\t{{artist}}\t{{xesam:url}}"
VERIFY_S, POLL_S = 1.5, 0.2   # mediafix: re-read the player after an action, this long
DUCK_FILE = "heardback-ducked"   # players the voice paused while it listens: `name\turl\ttitle` per line (duckfix)
PLAYBACK_STATES = {"Playing", "Paused", "Stopped"}


def duck_path() -> Path:
    return Path(runtime_path(DUCK_FILE))


def ducked() -> set[str]:
    try:
        return {line.split("\t")[0] for line in duck_path().read_text().splitlines() if line}
    except OSError:
        return set()


def claim() -> None:
    """A playback verb ran (or the user said stop): the voice must not resume what it paused for the listen."""
    duck_path().unlink(missing_ok=True)


def claim_browser() -> None:
    """Changing the active Zen tab invalidates its shared MPRIS item, even while metadata lags."""
    path = duck_path()
    try:
        with path.open("r+") as stream:
            rows = stream.readlines()
            retained = [row for row in rows if not row.split("\t", 1)[0].startswith("firefox")]
            if retained != rows:
                # Keep this inode: a concurrent full claim must never be undone by recreating the path.
                stream.seek(0)
                stream.writelines(retained)
                stream.truncate()
    except FileNotFoundError:
        return


def _pc(*args: str, inventory: bool = False) -> str:
    try:
        p = subprocess.run(["playerctl", *args], capture_output=True, text=True, timeout=3)
    except FileNotFoundError:
        raise JvError(1, "playerctl is not installed", say="Media control isn't installed.")
    except subprocess.TimeoutExpired:
        raise JvError(1, "playerctl timed out", say="The player didn't answer.")
    if p.returncode:
        if inventory and p.returncode == 1 and p.stderr.strip().lower() == "no players found":
            return ""
        raise JvError(1, "playerctl rejected the command", say="The media command failed.",
                      command_status=p.returncode, dispatched=not inventory)
    return p.stdout


def players() -> list[dict]:
    """MPRIS players with metadata, in playerctl's order. One the voice paused for the listen counts as Playing
    ("ducked"): "stop the music" / "next song" / "what's playing" said over music mean that player."""
    out, duck = [], ducked()
    for line in _pc("-a", "metadata", "--format", FMT, inventory=True).splitlines():
        name, status, title, artist, url = (line.split("\t") + [""] * 5)[:5]
        if name:
            d = name in duck and status == "Paused"
            out.append({"name": name, "status": "Playing" if d else status, "title": title, "artist": artist,
                        "url": url, "ducked": d, "observed_status": status})
    return out


def _rank(p: dict) -> int:   # Zen first, then mpd, other apps, the phone (kdeconnect) last
    n = p["name"]
    return 0 if n.startswith("firefox") else 1 if n == "mpd" else 3 if n.startswith("kdeconnect") else 2


def _app(p: dict) -> str:
    n = p["name"]
    return "Zen" if n.startswith("firefox") else "your phone" if n.startswith("kdeconnect") else n.split(".")[0]


def _native_spotify(p: dict) -> bool:
    return bool(re.fullmatch(r"spotify(?:\.instance_?[0-9]+(?:_[0-9]+)?)?", p.get("name", "")))


def _spotify_site(p: dict) -> bool:
    try:
        return _rank(p) == 0 and urlsplit(p.get("url") or "").hostname == "open.spotify.com"
    except ValueError:
        return False


def _prefer_app(ps: list[dict]) -> list[dict]:
    return [p for p in ps if not _spotify_site(p)] if any(_native_spotify(p) for p in ps) else ps


def target(action: str, ps: list[dict]) -> dict | None:
    """Select a unique current player; a browser name never resolves ambiguity."""
    playing = [p for p in ps if p["status"] == "Playing"]
    candidates = playing or ([] if action in ("pause", "status") else
                             _prefer_app([p for p in ps if not p["name"].startswith("kdeconnect")]))
    if len(candidates) > 1:
        raise JvError(2, "multiple media players", say="Which player should I use?",
                      candidates=[p["name"] for p in candidates])
    return candidates[0] if candidates else None


def _title(p: dict) -> str:
    return p["title"] or _app(p)


def _after(name: str, done) -> dict | None:
    """Re-read player `name` until `done(p)` or VERIFY_S; the last state seen (None if it vanished)."""
    deadline = time.monotonic() + VERIFY_S
    while True:
        p = _current(name)
        remaining = deadline - time.monotonic()
        if (p and done(p)) or remaining <= 0:
            return p
        time.sleep(min(POLL_S, remaining))


def _track_identity(p: dict | None) -> tuple[str, ...] | None:
    if not p:
        return None
    url = (p.get("url") or "").strip()
    # Spotify's MPRIS player may report its current playlist/album/collection page
    # instead of the playable item. Compare metadata on those non-item pages, while
    # keeping track and episode URLs authoritative.
    try:
        parsed = urlsplit(url) if url else None
    except ValueError:
        parsed = None  # malformed player metadata is still compared as an opaque URL below
    spotify_page = False
    if parsed is not None and parsed.hostname == "open.spotify.com":
        path = [part for part in parsed.path.split("/") if part]
        if path and path[0].lower().startswith("intl-"):
            path = path[1:]
        spotify_page = not path or path[0].lower() not in {"track", "episode"}
    if url and not spotify_page:
        return ("url", url)
    title = (p.get("title") or "").strip()
    artist = (p.get("artist") or "").strip()
    kind = "spotify-metadata" if spotify_page else "metadata"
    return (kind, title, artist) if title else None


def _track_changed(before: tuple[str, ...] | None, after: dict | None) -> bool | None:
    current = _track_identity(after)
    if before is None or current is None or before[0] != current[0]:
        return None
    if before[0] == "url":
        return before != current
    if before[0] not in ("metadata", "spotify-metadata"):
        return None
    if bool(before[2]) != bool(current[2]):  # artist metadata appeared or disappeared: not comparable
        return None
    return before[1:] != current[1:]


def _evidence(p: dict | None, *, was_ducked: bool = False) -> dict | None:
    if not p:
        return None
    return {
        "player": p["name"],
        "status": "Paused" if was_ducked else (p.get("status") or None),
        "title": p.get("title") or None,
        "artist": p.get("artist") or None,
        "url": p.get("url") or None,
        "ducked": bool(was_ducked or p.get("ducked")),
    }


def _current(name: str) -> dict | None:
    return next((q for q in players() if q["name"] == name), None)


def _skip_failure(action: str, p: dict, before: dict, after: dict | None, before_id,
                  expected_status: str | None, *, action_sent: bool = True,
                  expected_identity: tuple[str, ...] | None = None, resume_attempted: bool = False) -> None:
    changed = _track_changed(before_id, after)
    app = _app(p)
    if not action_sent:
        say = f"I can't verify the current track on {app}, so I didn't skip."
        why = "track identity unavailable before action"
    elif expected_identity is not None and (not after or _track_identity(after) != expected_identity):
        if resume_attempted and (not after or _track_identity(after) is None):
            say = f"I couldn't verify {app}'s selected track while resuming."
            why = "selected track identity unavailable during resume"
        elif resume_attempted:
            say = f"{app} changed tracks while resuming, so playback isn't verified."
            why = "selected track identity changed during resume"
        else:
            say = f"I couldn't verify the selected track on {app}, so I didn't resume it."
            why = "selected track identity changed before resume"
    elif changed is False:
        say = f"I couldn't confirm {app} changed tracks."
        why = "track identity unchanged during verification"
    elif changed is None:
        say = f"I couldn't verify whether {app} changed tracks."
        why = "track identity unknown"
    elif not after or after["status"] not in PLAYBACK_STATES:
        say = f"{app} changed tracks, but its playback state is unknown."
        why = "playback state unknown after track change"
    elif expected_status and after["status"] != expected_status:
        say = f"{app} changed tracks, but playback is {after['status'].lower()}."
        why = f"playback state {after['status']} after track change"
    else:
        say = f"{app} didn't confirm the track change."
        why = "track change not confirmed"
    raise JvError(1, f"{p['name']} {action} verification failed: {why}", say=say, player=p["name"],
                  action=action, track_changed=changed, before=before, after=_evidence(after),
                  resume_attempted=resume_attempted)


def _skip(action: str, p: dict) -> dict:
    was_ducked = bool(p.get("ducked"))
    before_id = _track_identity(p)
    before = _evidence(p, was_ducked=was_ducked)
    if before_id is None:
        _skip_failure(action, p, before, p, before_id, None, action_sent=False)

    expected = None if was_ducked or p["status"] not in PLAYBACK_STATES else p["status"]
    claim()   # claim before dispatch, including rejected or uncertain commands
    _pc("-p", p["name"], PC[action])
    after = _after(p["name"], lambda q: _track_changed(before_id, q) is True and q["status"] in PLAYBACK_STATES
                   and expected in (None, q["status"]))
    changed = _track_changed(before_id, after)
    if changed is not True or not after or after["status"] not in PLAYBACK_STATES:
        _skip_failure(action, p, before, after, before_id, expected)
    if expected and after["status"] != expected:
        _skip_failure(action, p, before, after, before_id, expected)

    if was_ducked and after["status"] != "Playing":
        if after["status"] not in ("Paused", "Stopped"):
            _skip_failure(action, p, before, after, before_id, "Playing")
        # Re-read before resuming: if the selected item changed again, leave it paused.
        candidate = _track_identity(after)
        current = _current(p["name"])
        if not current or _track_identity(current) != candidate:
            _skip_failure(action, p, before, current, before_id, "Playing", expected_identity=candidate)
        if current["status"] == "Playing":
            after = current
        elif current["status"] in ("Paused", "Stopped"):
            _pc("-p", p["name"], "play")
            after = _after(p["name"], lambda q: _track_identity(q) == candidate and q["status"] == "Playing")
            if not after or _track_identity(after) != candidate or after["status"] != "Playing":
                _skip_failure(action, p, before, after, before_id, "Playing", expected_identity=candidate,
                              resume_attempted=True)
        else:
            _skip_failure(action, p, before, current, before_id, "Playing", expected_identity=candidate)

    title = after["title"]
    label = title or "a different track"
    state_suffix = f", {after['status'].lower()}" if after["status"] != "Playing" else ""
    say = f"{action}: {label}{state_suffix}"
    return {"player": p["name"], "title": title, "status": after["status"],
            "paused": after["status"] == "Paused", "say": say,
            "track_changed": True, "before": before, "after": _evidence(after)}


def _position(name: str) -> float | None:
    try:
        return float(_pc("-p", name, "position").strip())
    except (JvError, ValueError):
        return None


def _verify_play(name: str | None, identity, tab: dict | None = None) -> tuple[dict | None, list[dict], str | None]:
    """Observe progress on the same item, or audibility on the exact unchanged tab."""
    deadline, samples, previous, after = time.monotonic() + VERIFY_S, [], None, None
    while True:
        after = _current(name) if name else None
        position = _position(name) if name and after else None
        sample = {"at": time.monotonic(), "position": position, "after": _evidence(after)}
        samples.append(sample)
        same = after and identity is not None and _track_identity(after) == identity
        if same and after["status"] == "Playing" and not after.get("ducked"):
            if previous is not None and position is not None and position > previous:
                return after, samples, "same-item progress"
            previous = position
        else:
            previous = None
        if tab is not None:
            from jv import zen
            current = next((t for t in zen.tabs() if t["id"] == tab["id"] and t["url"] == tab["url"]), None)
            sample["tab"] = {"id": tab["id"], "present": current is not None,
                             "audible": bool(current and current.get("audible")),
                             "muted": bool(current and current.get("muted"))}
            if current and current.get("audible") and not current.get("muted") and \
                    (name is None or same and after["status"] == "Playing" and not after.get("ducked")):
                return after, samples, "selected tab audible"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return after, samples, None
        time.sleep(min(POLL_S, remaining))


def act(action: str, p: dict) -> dict:
    if action == "status":
        by = f" by {p['artist']}" if p["artist"] else ""
        return {"player": p["name"], "title": p["title"], "say": f"{_title(p)}{by}, on {_app(p)}"}
    if action in ("next", "previous"):
        return _skip(action, p)
    before, identity = _evidence(p), _track_identity(p)
    observed = p.get("observed_status", "Paused" if p.get("ducked") else p["status"])
    if action == "toggle":
        action = "pause" if observed == "Playing" or p.get("ducked") else "play"
    claim()   # failed/uncertain actions must not trigger turn-end recovery
    _pc("-p", p["name"], PC[action])
    if action == "play":
        tab = None
        if _rank(p) == 0 and p.get("url"):
            from jv import zen
            try:
                matches = [t for t in zen.tabs() if t["url"] == p["url"]]
                tab = matches[0] if len(matches) == 1 else None
            except JvError:
                pass   # independent same-item progress can still establish playback
        after, samples, reason = _verify_play(p["name"], identity, tab)
    else:
        after = _after(p["name"], lambda q: q["status"] in ("Paused", "Stopped"))
        samples = []
        reason = "observed paused" if after and after["status"] in ("Paused", "Stopped") and \
            identity is not None and _track_identity(after) == identity else None
    evidence = {"player": p["name"], "before": before, "after": _evidence(after),
                "samples": samples, "verification": reason or "playback unverified", "dispatched": True,
                "command_status": 0, "selection_reason": "unique player"}
    if not reason:
        raise JvError(1, "playback verification failed", **evidence,
                      say=f"I couldn't verify {_app(p)} {'playing' if action == 'play' else 'paused'}.")
    prefix = "Already playing" if action == "play" and observed == "Playing" else "playing" if action == "play" else "paused"
    return {**evidence, "title": after["title"], "paused": action == "pause", "say": f"{prefix} {_title(after)}"}


def zen(action: str) -> dict | None:
    """zen.video's first try: Zen's MPRIS player (any media element, e.g. Spotify); None if Zen has none."""
    p = target(action, [p for p in players() if _rank(p) == 0])
    return act(action, p) if p else None


SPOTIFY = "open.spotify.com"


def _spotify(action: str, source_revision: str | None = None) -> dict:
    """The open.spotify.com tab in Zen, through the add-on's `video` op on that tab (Zen stays in the background);
    verified by Zen's MPRIS player showing Spotify or the tab turning audible. No tab: open one, nothing to play."""
    from jv import zen   # zen imports media
    if source_revision:
        _, state, _ = zen._open_state({"query": "spotify", "source_revision": source_revision})
        all_tabs = state["tabs"]
    else:
        all_tabs = zen.tabs()
    ts = [t for t in all_tabs if zen._host(t["url"]) == SPOTIFY]
    if not ts:
        claim()
        r = zen._open({"query": "spotify", **({"source_revision": source_revision} if source_revision else {})}, focus=False)
        claim()
        return {**r, "opened": True, "intent": "music", "verification": "opened only",
                "say": "Opened Spotify; pick something to play."}
    if len(ts) != 1:
        raise JvError(2, "multiple Spotify tabs", say="Which Spotify tab should I use?", candidates=ts)
    t = ts[0]
    bound = [p for p in players() if _rank(p) == 0 and p["url"] == t["url"]]
    p = bound[0] if len(bound) == 1 else None
    before = _evidence(p)
    if action == "toggle":
        observed = (p or {}).get("observed_status")
        if observed not in PLAYBACK_STATES:
            raise JvError(1, "Spotify playback state unknown", say="I couldn't tell whether Spotify is playing.")
        action = "pause" if observed == "Playing" or p.get("ducked") else "play"
    claim()
    from jv import trace
    try:
        dispatch = zen.call("video", {"tabId": t["id"], "action": action})
    except JvError as e:
        # A failed reply can follow a successful click: never toggle again or use an unrelated shared player.
        trace.emit("jv", "media_dispatch", os.environ.get("HEARDBACK_TURN"), tabId=t["id"], action=action, error=e.msg, details=e.extra)
        raise JvError(1, f"spotify tab: {e.msg}", say="I couldn't control Spotify; playback is unverified.",
                      dispatched=True, tabId=t["id"], before=before, dispatch_error=e.msg, dispatch_details=e.extra) from e
    trace.emit("jv", "media_dispatch", os.environ.get("HEARDBACK_TURN"), tabId=t["id"], action=action, dispatch=dispatch)
    if action == "play":
        after, samples, reason = _verify_play((p or {}).get("name"), _track_identity(p), t)
    else:
        after = _after(p["name"], lambda q: q["status"] in ("Paused", "Stopped")) if p else None
        samples = []
        reason = "observed paused" if p and after and _track_identity(after) == _track_identity(p) and \
            after["status"] in ("Paused", "Stopped") else None
    evidence = {"player": "spotify", "tabId": t["id"], "before": before, "after": _evidence(after),
                "samples": samples, "verification": reason or "playback unverified", "dispatched": True,
                "command_status": "accepted", "selection_reason": "explicit Spotify tab", "dispatch": dispatch}
    trace.emit("jv", "media_verification", os.environ.get("HEARDBACK_TURN"), tabId=t["id"], action=action, before=before, after=_evidence(after),
               verification=evidence["verification"], first=samples[0] if samples else None,
               last=samples[-1] if samples else None)
    if not reason:
        raise JvError(1, "Spotify playback verification failed", **evidence,
                      say="Spotify accepted the command, but I couldn't verify playback.")
    title = (after or {}).get("title") or "Spotify"
    return {**evidence, "title": title, "paused": action == "pause",
            "say": f"{'playing' if action == 'play' else 'paused'} {title}"}


def _known_music(p: dict) -> bool:
    url = p.get("url") or ""
    if _native_spotify(p) and not (p.get("title") or "").strip():
        return False
    if _native_spotify(p) and re.fullmatch(r"spotify:track:[A-Za-z0-9]+", url):
        return True
    try:
        u = urlsplit(url)
        path = [part for part in u.path.split("/") if part]
        if path and path[0].startswith("intl-"):
            path = path[1:]
        return u.hostname == SPOTIFY and len(path) == 2 and path[0] == "track" and bool(path[1])
    except ValueError:
        return False


def _select(a: dict, ps: list[dict]) -> tuple[str, dict | None]:
    """One policy for preflight and dispatch; only explicit Spotify may open its site."""
    action = a.get("action") or "toggle"
    starting_music = action == "play" and a.get("intent") == "music"
    requested = a.get("target")
    source = a.get("source")
    if source and requested != "spotify":
        raise JvError(2, "source requires target spotify", say="Which Spotify source should I use?")
    if requested == "spotify":
        ps = [p for p in ps if (_native_spotify(p) and source != "site") or
              (_spotify_site(p) and source != "desktop")]
    elif requested == "zen":
        ps = [p for p in ps if _rank(p) == 0]
    elif requested == "mpd":
        ps = [p for p in ps if p["name"] == "mpd"]
    if a.get("player"):
        ps = [p for p in ps if p["name"] == a["player"]]
    elif requested == "spotify":
        native = [p for p in ps if _native_spotify(p)]
        if native:
            ps = native
        elif source != "desktop" and action in ("play", "pause", "toggle"):
            return "site", None
    if (requested or a.get("player")) and not ps:
        raise JvError(1, "requested media player unavailable", target=requested, player=a.get("player"),
                      say="Spotify isn't the current system player. Start it first." if requested == "spotify" else
                          "That media player is no longer available.")
    if starting_music and not a.get("player"):
        active = [p for p in ps if p["status"] == "Playing"]
        if not requested and len(active) > 1:
            ps = active
        else:
            ps = [p for p in ps if _known_music(p)]
            native = [p for p in ps if _native_spotify(p)]
            ps = native if native and not requested else _prefer_app(ps)
        if not ps:
            raise JvError(2, "music target unknown", clarification="music_source", intent="music",
                          action=action, target=requested, source=source,
                          say="Where should I play music—Spotify or YouTube?", candidates=["spotify", "youtube"])
    # Explicit selections may name a paused player, even for pause/status.
    try:
        if len(ps) > 1 and (requested == "spotify" and all(_native_spotify(p) for p in ps) or
                            starting_music):
            raise JvError(2, "multiple media players", candidates=[p["name"] for p in ps])
        p = ps[0] if len(ps) == 1 and (requested or a.get("player") or starting_music) else target(action, ps)
    except JvError as e:
        if e.code == 2 and e.extra.get("candidates"):
            labels = {p["name"]: _app(p) for p in ps if p["name"] in e.extra["candidates"]}
            options = [label if list(labels.values()).count(label) == 1 else name for name, label in labels.items()]
            e.extra.update(clarification="media_player", action=action, target=requested, source=source,
                           intent=a.get("intent"), player_labels=labels,
                           say=f"Which player—{' or '.join(options)}?")
        raise
    if not p:
        raise JvError(1, "nothing is playing" if action in ("pause", "status") else "no media player",
                      say="Nothing is playing." if action in ("pause", "status") else "There's no music player open.")
    return "desktop" if _native_spotify(p) else "player", p


def _prepare(a: dict) -> tuple[str, dict | None, str | None]:
    mode, p = _select(a, players())
    expected = a.get("source_revision") or a.get("_source_revision")
    if expected and not expected.startswith(f"media:{mode}:"):
        raise JvError(1, "media backend changed", say="The media source changed; please ask again.")
    sentence, zen_revision = None, None
    if mode == "site":
        from jv import zen
        opening = {"query": "spotify"}
        if expected:
            opening["source_revision"] = "zen:" + expected.rsplit(":", 1)[-1]
        sentence = zen._open_check(opening)
        zen_revision = opening["_source_revision"]
    binding = {k: a.get(k) for k in ("action", "target", "player", "source", "intent")}
    binding.update(mode=mode, selected=p["name"] if p else None, zen_revision=zen_revision)
    digest = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    revision = f"media:{mode}:{digest}" + (":" + zen_revision.split(":", 1)[1] if zen_revision else "")
    if expected and expected != revision:
        raise JvError(1, "media target changed", say="The media source changed; please ask again.")
    a["_source_revision"], a["_zen_revision"] = revision, zen_revision
    return mode, p, sentence


def _media(a: dict) -> dict:
    action = a.get("action") or "toggle"
    try:
        mode, p, _ = _prepare(a)
        result = _spotify(action, a["_zen_revision"]) if mode == "site" else act(action, p)
        # act/_spotify verify before returning; opening a tab alone never verifies playback.
        return {**result, "observed": result.get("opened") is not True,
                "intent": a.get("intent") or "resume", "source_revision": a["_source_revision"]}
    finally:
        if action != "status":
            claim()


def _media_check(a: dict) -> str | None:
    return _prepare(a)[2]


_PHRASES = {**{f"{p}{suffix}": {"action": a, **({"target": "zen"} if targeted else {}),
                                  **({"intent": "music" if p == "play music" else "resume"}
                                     if p in ("resume music", "play music", "unpause music") else {})} for p, a in [
                ("stop music", "pause"), ("pause music", "pause"), ("pause song", "pause"), ("pause this song", "pause"),
                ("resume music", "play"), ("play music", "play"), ("unpause music", "play"), ("next song", "next"),
                ("play( the)? next song", "next"), ("play( the)? next music", "next"),
                ("previous song", "previous")]
            for suffix, targeted in [("( playing)?", False), ("( playing)? on zen", True), ("( playing)? in zen", True)]},
            "skip( this)?( song)?": {"action": "next"}, "play next": {"action": "next"}, "what's playing": {"action": "status"},
            "what is playing": {"action": "status"}, "what song is this": {"action": "status"},
            **{p: {"action": "play", "target": "spotify"} for p in [
                "play( some)? music( on)?( in)? spotify", "play spotify", "resume spotify", "play( the)? spotify music",
                "resume( the)? music( on)?( in)? spotify", "start spotify", "start( the)? music( on)?( in)? spotify"]},
            **{p: {"action": "pause", "target": "spotify"} for p in ["pause spotify", "stop spotify"]}}
_PHRASES.update({
    f"{phrase} {prep} {name}": {"action": action, "target": name}
    for name in TARGETS
    for prep in ("on", "in")
    for phrase, action in [("next( song)?", "next"), ("play( the)? next( song)?", "next"),
                           ("skip( next)?( song)?", "next"), ("previous( song)?", "previous"),
                           ("play( the)? previous( song)?", "previous")]
})
_PHRASES.update({
    f"{phrase} {name}": {"action": action, "target": "spotify", "source": source}
    for name, source in [("spotify app", "desktop"), ("desktop spotify", "desktop"),
                         ("spotify website", "site"), ("spotify site", "site"), ("spotify in zen", "site")]
    for phrase, action in [("play", "play"), ("resume", "play"), ("pause", "pause"), ("stop", "pause"),
                           ("play( some)? music on", "play"), ("play( some)? music in", "play"),
                           ("resume music on", "play"), ("resume music in", "play"),
                           ("next on", "next"), ("next in", "next"),
                           ("previous on", "previous"), ("previous in", "previous")]
})
VERBS = [
    Verb("media", _media,
         "Music and audio in any app (Zen tabs incl. Spotify and YouTube, mpd): play, pause, toggle, next, "
         "previous, or status. intent music starts identified music, preferring native Spotify; intent resume follows the current "
         "playing/ducked player. target spotify prefers native Spotify, falling back to its identified website; source desktop/site "
         "makes that choice explicit. Only named Spotify may ask to open an unseen site; never silently launch an app. "
         "Desktop catalogue search is unsupported. target zen/mpd selects that app; player selects an exact "
         "current candidate name returned by a media_player clarification. Never substitute another player. Its say is what really "
         "happened (verified on the player)."
         " \"Play music on Spotify\", \"play Spotify\" or \"resume Spotify\" (no song named) is media_media with action play and target spotify: it prefers the native Spotify player. An explicit Spotify app/desktop request adds source desktop; an explicit website or Spotify in Zen adds source site; preserve this choice in every follow-up. Music, a song or audio in any app, Spotify in Zen included (stop, pause, resume, next, skip, previous, \"what's playing\"), is media_media, never zen_video. \"Play music\" without a source: action play, intent music. \"Resume music\" or \"unpause music\": action play, intent resume. Say what plays, paused or skipped only from the tool's say: never claim something is playing that the tool didn't confirm, and if it failed, say it didn't play. Never launch an app or create a tab as an unnamed music fallback. Preserve a returned source_revision exactly. Answer a media_player clarification with the selected player. Never automatically retry a failed or unverified media action or switch players in the same attempt; a fresh explicit request, including \"play next\", is a new attempt: call media_media even if an earlier one failed.",
         {"type": "object", "properties": {"action": {"type": "string", "enum": ACTIONS},
                                           "target": {"type": "string", "enum": TARGETS},
                                           "source": {"type": "string", "enum": SOURCES},
                                           "player": {"type": "string", "maxLength": 200},
                                           "intent": {"type": "string", "enum": ["music", "resume"]},
                                           "source_revision": {"type": "string", "pattern": "^" + REVISION_RE + "$"}}, "required": ["action"]},
         ["action", "--target", "--source", "--player", "--intent", "--source-revision"], confirm="auto", check=_media_check, observes=True,
         args={"action": {"type": "enum", "choices": ACTIONS},
                                       "target": {"type": "enum", "choices": TARGETS},
                                       "source": {"type": "enum", "choices": SOURCES},
                                       "intent": {"type": "enum", "choices": ["music", "resume"]}},
         phrases=list(_PHRASES), phrase_args=_PHRASES),
]
