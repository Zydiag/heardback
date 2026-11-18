"""Zen verbs: talk to the heardback extension through the zen-host unix socket."""
import configparser
import functools
import hashlib
import json
import os
import re
import shutil
import socket
import time
import zipfile
from pathlib import Path
from urllib.parse import quote, quote_plus, urlsplit

from jv import hypr, media
from jv.verb import JvError, Verb, fuzzy, _readback
from jv.zen_host import sock_path

EXT_ID = "jarvis@zydiag.local"
HOST_NAME = "jarvis_zen"
HOST_DIRS = ["~/.mozilla/native-messaging-hosts", "/usr/lib/mozilla/native-messaging-hosts"]  # [1] only if the probe fails
REPO = Path(__file__).resolve().parents[2]   # src/jv/zen.py -> repo (editable install)
XPI = "dist/heardback-zen.xpi"
CLAUDE_PREFIX = "https://claude.ai/"
URL_RE = r"(https?://\S+|[\w-]+(\.[\w-]+)+([/?#]\S*)?)"
YT_SEARCH = "https://www.youtube.com/results?search_query="
YT_WATCH = "https://www.youtube.com/watch?v="
YT_TIMEOUT = 8.0   # yt_first: the add-on fetches for 6 s, the host waits 7 s
AUTOPLAY_WAIT = 5.0   # a started video turns audible in ~1-3 s; after this, ask the page why it didn't
ADDON_OLD_SAY = "Zen's heardback add-on is out of date: reload it in about:addons."
BLOCKED_SAY = "Opened it, but Zen blocked autoplay: allow autoplay for YouTube once in the padlock menu."
ZEN_DIR = "~/.config/zen"   # profiles.ini lives here
AUTOPLAY_PREF = re.compile(r'user_pref\("media\.autoplay\.default",\s*(\d+)\);')
PAGE_TIMEOUT = 14.0   # click_result: the add-on polls 12 s, the host waits 13 s; the brain allows ZEN_PAGE_TIMEOUT_S
EVERY = {"all", "every", "my", "these", "those", "other", "the other", "all my", "all the", "all of my"}
_NTH = {w: i for i, w in enumerate("first second third fourth fifth sixth seventh eighth ninth tenth".split(), 1)}
_NTH |= {w: i for i, w in enumerate("one two three four five six seven eight nine ten".split(), 1)}
# "second result", "the 3rd video on youtube", "result 2", "the first one": a pick, not a title
RESULT_ONLY = re.compile(r"(?:the )?(?:(\w+) (?:result|video|one|1)|(?:result|video) (?:number )?(\w+))"
                         r"(?: (?:on|in|from) (?:youtube|you tube|google|(?:your |the )?search(?: results)?))?")


def result_number(text: str) -> int | None:
    """N if the words only name search result N (raw or router-normalised text), else None."""
    m = RESULT_ONLY.fullmatch(" ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split()))
    w = m and (m[1] or m[2])
    if not w:
        return None
    if d := re.fullmatch(r"(\d+)(?:st|nd|rd|th)?", w):
        return int(d[1])
    return _NTH.get(w)


def call(op: str, args: dict | None = None, timeout: float = 3.5) -> dict:
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(timeout)
            s.connect(sock_path())
            s.sendall((json.dumps({"op": op, "args": args or {}}) + "\n").encode())
            line = s.makefile().readline()
    except (FileNotFoundError, ConnectionRefusedError):
        raise JvError(1, "zen bridge not connected (is Zen open? run jv zen ping)", say="Zen isn't connected.")
    except TimeoutError:
        raise JvError(1, "zen did not reply", say="Zen didn't reply.")
    try:
        rep = json.loads(line)
    except ValueError:
        raise JvError(1, "zen did not reply", say="Zen didn't reply.")
    if rep.get("error") == "unknown op":   # P3: the add-on (or zen-host) predates this jv; stale: callers keep the say
        raise JvError(1, f"zen add-on out of date: unknown op {op!r}", say=ADDON_OLD_SAY, stale=True)
    if not rep.get("ok"):   # diag: why the page had no match (logged with the jv output, never spoken)
        raise JvError(1, rep.get("error") or rep.get("reason") or "zen error",
                      **({"diag": str(rep["diag"])[:200]} if rep.get("diag") else {}))
    return rep


COVERAGE = {"scope": "browser.tabs.query", "zen_workspaces": "unknown", "absence": "unconfirmed"}


def inventory_metadata(rep: dict) -> dict:
    """Active is per window; only an explicit, consistent current-tab ID identifies the current tab."""
    w, tab = rep.get("current_window"), rep.get("current_tab")
    valid_id = lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0
    active = [t for t in rep["tabs"] if t.get("windowId") == w and t.get("active")]
    known = valid_id(w) and valid_id(tab) and len(active) == 1 and active[0].get("id") == tab
    return {"current_window": w if valid_id(w) else None, "current_tab": tab if known else None,
            "current_identity": "known" if known else "unknown", "creation_guard": rep.get("creation_guard") is True,
            "ask_evidence": rep.get("ask_evidence") == 1,
            "navigation_focus": rep.get("navigation_focus") == 1,
            "coverage": dict(COVERAGE)}


def inventory(all_windows: bool = True) -> dict:
    rep, now = call("list"), time.time() * 1000
    seen = set()
    for t in rep["tabs"]:
        if any(not isinstance(t.get(k), int) or isinstance(t[k], bool) or t[k] < 0 for k in ("id", "windowId")) \
                or t["id"] in seen:
            raise JvError(1, "invalid or duplicate browser tab identity")
        seen.add(t["id"])
    meta = inventory_metadata(rep)
    ts = [{"id": t["id"], "window": t["windowId"], "windowId": t["windowId"],
             "current": meta["current_tab"] == t["id"], "title": t.get("title") or "", "url": t.get("url") or "",
             "pinned": bool(t.get("pinned")), "active": bool(t.get("active")), "audible": bool(t.get("audible")),
             "lastAccessed": t.get("lastAccessed"),
             "idle_h": round((now - (t.get("lastAccessed") or now)) / 3.6e6, 1)}
            for t in rep["tabs"] if all_windows or t["windowId"] == meta["current_window"]]
    return {**meta, "tabs": ts}


def tabs(all_windows: bool = True) -> list[dict]:
    return inventory(all_windows)["tabs"]


def focus_zen_client(title: str) -> int | None:
    """A single Zen compositor client plus matching title is the only reliable local mapping."""
    if not title:
        return None
    try:
        zs = [c for c in hypr.clients() if c.get("class") == "zen"]
        # With several windows a unique title can belong to a different window while activation's title lags.
        if len(zs) != 1:
            return None
        cs = [c for c in zs if c.get("title", "").removesuffix(" — Zen Browser") == title
              or c.get("title", "").removesuffix(" — Zen") == title]
        if len(cs) != 1:
            return None
        c = cs[0]
        ws = int(c["workspace"]["id"])
        hypr.dispatch(f"hl.dsp.focus({{workspace = {ws}}})")
        hypr.focus_window(c["address"])
        return ws
    except JvError:   # the tab already switched; window focus is a bonus
        return None


def _protected(t: dict) -> bool:
    return t["pinned"] or t["active"] or t["audible"] or t["url"].startswith(CLAUDE_PREFIX)


def unused(hours: float) -> list[dict]:
    return sorted((t for t in tabs() if t["idle_h"] >= hours and not _protected(t)),
                  key=lambda t: t["idle_h"], reverse=True)


def close_matches(args: dict) -> tuple[list[dict], bool]:
    """(tabs, fuzzy): fuzzy is True when the single tab came from the fuzzy title fallback."""
    ts = [t for t in tabs() if not t["pinned"]]
    if args.get("ids"):
        ids = {int(i) for i in args["ids"].split(",")}
        return [t for t in ts if t["id"] in ids], False
    if not args.get("query") or args["query"].casefold() in EVERY:   # "close all tabs": Claude asks which
        raise JvError(2, "say which tab to close")
    q = args["query"].casefold()
    head, _, rest = q.partition(" ")
    if head in {"this", "current", "active", "focused"}:   # live 16:59 "close this zen tab", 20:16 "this youtube"
        cur = [t for t in ts if t.get("current")]
        if rest in ("", "zen") or any(rest in t["title"].casefold() or rest in t["url"].casefold() for t in cur):
            return cur, True
        q = rest   # the current tab isn't "<rest>": match "<rest>" like "close <rest> tab"
    hits = [t for t in ts if q in t["title"].casefold() or q in t["url"].casefold()]
    if hits or not ts:
        return hits, False
    by_title = {t["title"]: t for t in ts}
    return [by_title[fuzzy(q, list(by_title))]], True


def _n(n: int, word: str) -> str:
    return f"{n} {word}{'s' * (n != 1)}"


def _titles(ts: list[dict]) -> str:
    """At most 2 titles, ~6 words each: a read-back of 3 full YouTube titles took ~10 s (friction2 #38)."""
    s = ", ".join(_short(t["title"], 6) or "a page" for t in ts[:2])
    return s + (f" and {len(ts) - 2} more" if len(ts) > 2 else "")


def _shared(ts: list[dict], query: str | None) -> str | None:
    """The query as the titles spell it ("YouTube") when every title contains it, else None."""
    q = (query or "").strip().casefold()
    if not q or not all(q in t["title"].casefold() for t in ts):
        return None
    i = ts[0]["title"].casefold().index(q)
    return ts[0]["title"][i:i + len(q)]


def _remove(ts: list[dict]) -> int:
    n = len(call("remove", {"tabIds": [t["id"] for t in ts]})["closed"])
    media.claim_browser()   # duckfix: "close youtube" moves Zen's one MPRIS player; other players (Spotify) still resume
    return n


def _list(a: dict) -> dict:
    state = inventory(a.get("window") == "all")
    return {**state, "say": f"{_n(len(state['tabs']), 'tab')} visible to the add-on; other Zen workspaces may be unseen"}


def _switch(a: dict) -> dict:
    state = inventory()
    ts = state["tabs"]
    q = _open_query(a["query"])
    t = open_tab(q, ts, state["current_window"])
    if not t:
        if re.fullmatch(URL_RE, q, re.I):
            raise JvError(1, "no matching tab visible to the add-on; other Zen workspaces may be unseen",
                          say="I couldn't see that tab; other Zen workspaces may be unseen.")
        try:
            title = fuzzy(a["query"], sorted({t["title"] for t in ts}))
            hits = [t for t in ts if t["title"].casefold() == title.casefold()]
        except JvError as e:
            if e.code != 1:
                raise
            try:
                url = fuzzy(a["query"], sorted({t["url"] for t in ts}))
            except JvError as miss:
                if miss.code != 1:
                    raise
                raise JvError(1, "no matching tab visible to the add-on; other Zen workspaces may be unseen",
                              say="I couldn't see that tab; other Zen workspaces may be unseen.") from None
            hits = [t for t in ts if t["url"].casefold() == url.casefold()]
        t = _choose_tab(hits, a["query"], state["current_window"])
    result = _browse_tab(t, state, background=_background(a))
    result["observed"] = result["background"] or (
        result["activation_confirmed"] and result["browser_focus_observed"] and result["desktop_focus_observed"])
    return result


def _focus_result(r: dict, say: str) -> dict:
    ws = focus_zen_client(r.get("title") or "")
    return {**r, "workspace": ws, "focus_confirmed": ws is not None,
            "say": say if ws is not None else say + ". Please bring Zen to the front."}


def _desktop_focus() -> dict:
    """Browser IDs cannot map several compositor clients; only a sole Zen client is safe."""
    result = {"workspace": None, "desktop_address": None, "desktop_focus_requested": False,
              "desktop_focus_observed": False, "focus_confirmed": False}
    try:
        cs = [c for c in hypr.clients() if c.get("class") == "zen"]
        if len(cs) != 1:
            return result
        c = cs[0]
        addr, ws = hypr._addr(c["address"]), int(c["workspace"]["id"])
        result.update(workspace=ws, desktop_address=addr)
        if hypr._active() != addr:
            if not hypr.FOCUS_WINDOW:
                return result   # no cycle/fallback: one exact dispatch at most
            result["desktop_focus_requested"] = True
            hypr.dispatch(hypr.FOCUS_WINDOW.format(addr=addr))
        for i in range(4):
            if hypr._active() == addr:
                result["focus_confirmed"] = True
                result["desktop_focus_observed"] = True
                break
            if i < 3:
                time.sleep(0.05)
    except (JvError, KeyError, TypeError, ValueError):
        pass   # the tab already exists; uncertain focus must not repeat navigation
    return result


def _background(a: dict) -> bool:
    value = a.get("background", False)
    if not isinstance(value, bool):
        raise JvError(2, "background must be a boolean")
    return value


def _browse_result(r: dict, say: str, state: dict, *, background: bool = False) -> dict:
    result = {**r, "background": background, "workspace": None, "focus_confirmed": False,
              "browser_focus_requested": False, "browser_focus_observed": False,
              "target_validated": False, "activation_confirmed": False,
              "desktop_focus_requested": False, "desktop_focus_observed": False}
    if background:
        return {**result, "say": say + " in the background"}
    if state["navigation_focus"]:
        ids = [r.get(k) for k in ("tabId", "windowId")]
        if all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in ids):
            try:
                media.claim_browser()
                focused = call("activate", {"tabId": ids[0], "windowId": ids[1]})
                if all(type(focused.get(k)) is int for k in ("tabId", "windowId")) \
                        and focused.get("tabId") == ids[0] and focused.get("windowId") == ids[1] \
                        and focused.get("target_validated") is True:
                    result.update(title=focused.get("title") or r.get("title", ""),
                                  url=focused.get("url") or r.get("url", ""),
                                  target_validated=True,
                                  activation_confirmed=focused.get("activation_confirmed") is True,
                                  browser_focus_requested=focused.get("browser_focus_requested") is True,
                                  browser_focus_observed=focused.get("browser_focus_observed") is True)
                    if result["activation_confirmed"] and result["browser_focus_requested"]:
                        result.update(_desktop_focus())
            except JvError as e:
                result["focus_error"] = e.msg
    else:
        result["focus_limitation"] = "add-on lacks exact navigation focus"
    if say.startswith("switched to ") and not result["activation_confirmed"]:
        say = "found " + say.removeprefix("switched to ")
    return {**result, "observed": background or result["focus_confirmed"],
            "say": say if result["focus_confirmed"] else say + ". Please bring Zen to the front."}


def _browse_tab(t: dict, state: dict, *, background: bool = False) -> dict:
    r = {"title": t["title"], "url": t["url"], "tabId": t["id"], "windowId": t["window"]}
    say = ("found " if background else "switched to ") + _page_label(t["url"], t["title"])
    if not state["navigation_focus"] and not background:
        # Old activate cannot validate the expected window; don't risk activating a moved tab.
        say = f"found {_page_label(t['url'], t['title'])}"
    return _browse_result(r, say, state, background=background)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")


def _page_label(url: str, title: str = "") -> str:
    """URLs stay in structured results, not spoken read-backs."""
    host = _host(url if "://" in url else "https://" + url).casefold()
    names = {"youtube.com": "YouTube", "youtu.be": "YouTube", "open.spotify.com": "Spotify",
             "claude.ai": "Claude.ai", "web.whatsapp.com": "WhatsApp", "mail.google.com": "Gmail",
             "github.com": "GitHub", "reddit.com": "Reddit", "chatgpt.com": "ChatGPT"}
    if host in names:
        return names[host]
    clean = re.sub(URL_RE, "page", title or url, flags=re.I)
    return _short(clean, 6) or "that page"


def _resource(url: str) -> tuple:
    u = urlsplit(url)
    return (u.scheme.casefold(), u.netloc.casefold(), u.path or "/", u.query, u.fragment)


def _choose_tab(hits: list[dict], query: str, current_window: int | None = None) -> dict | None:
    if not hits:
        return None
    if len({_resource(t["url"]) for t in hits}) > 1:
        candidates = [{"tabId": t["id"], "windowId": t["window"], "title": t["title"], "url": t["url"],
                       "label": (f"{_page_label(t['url'], t['title'])}, "
                                 f"{_short(re.sub(URL_RE, 'page', t['title'], flags=re.I), 5)}, tab {t['id']}")}
                      for t in sorted(hits, key=lambda t: t["id"])]
        raise JvError(2, f"several visible pages match {query!r}; say which page", candidates=candidates)
    # Equivalent copies: current browser window, active there, exact recency, then stable tab ID.
    return min(hits, key=lambda t: (t["window"] != current_window, not t["active"],
                                   -(t.get("lastAccessed") or 0), t["id"]))


def open_tab(q: str, ts: list[dict], current_window: int | None = None) -> dict | None:
    """An open tab for a URL/domain, or a one-word site name matching a host ("whatsapp" → web.whatsapp.com).
    Anything else (a search phrase) gets None, so it never hijacks a search."""
    web = [t for t in ts if re.match(r"https?://", t["url"], re.I)]
    if re.fullmatch(URL_RE, q, re.I):
        u = q if re.match(r"https?://", q, re.I) else f"https://{q}"
        if re.match(r"https?://", q, re.I) or urlsplit(u).query or urlsplit(u).fragment:
            # Explicit URLs preserve path case, query order/values and fragment; no tracking guesses.
            hits = [t for t in web if _resource(t["url"]) == _resource(u)]
        else:
            host, path = _host(u).casefold(), urlsplit(u).path.rstrip("/")
            hits = [t for t in web if _host(t["url"]).casefold() == host
                    and (urlsplit(t["url"]).path + "/").startswith(path + "/")]
    elif re.fullmatch(r"[\w-]+", q):
        hits = [t for t in web if _host(t["url"]).split(".")[-2:-1] == [q.casefold()]]
    else:
        return None
    return _choose_tab(hits, q, current_window)


DASHBOARD = {"dashboard", "the dashboard", "heardback dashboard", "the heardback dashboard", "my dashboard"}


def dashboard_url() -> str | None:
    from heardback.voice import config
    port = config.load().dash_port
    return f"http://127.0.0.1:{port}/" if port else None


SITE_URLS = {"whatsapp": "web.whatsapp.com", "youtube": "www.youtube.com", "gmail": "mail.google.com",
             "spotify": "open.spotify.com", "github": "github.com", "reddit": "www.reddit.com",
             "chatgpt": "chatgpt.com", "claude": "claude.ai"}


def _open_query(query: str) -> str:
    q = query.strip()
    # Named Spotify searches are paths, even when the title contains spaces.
    # Encode only the search payload; preserve existing percent escapes.
    spotify_search = re.fullmatch(r"(https?://open\.spotify\.com/search/|open\.spotify\.com/search/)(.+)", q, re.I)
    if spotify_search:
        q = spotify_search[1] + quote(spotify_search[2], safe="%")
    if q.casefold().rstrip(".") in DASHBOARD and (u := dashboard_url()):   # friction #12: not a web search
        q = u
    return SITE_URLS.get(q.casefold().rstrip("."), q)


def _open_state(a: dict) -> tuple[str, dict, dict | None]:
    q, state = _open_query(a["query"]), inventory()
    revision_data = [q, _background(a), state["current_window"], state["current_tab"], state["creation_guard"],
                     sorted((t["id"], t["window"], t["url"], t["active"]) for t in state["tabs"])]
    revision = "zen:" + hashlib.sha256(json.dumps(revision_data, separators=(",", ":")).encode()).hexdigest()
    if a.get("source_revision") and a["source_revision"] != revision:
        raise JvError(1, "Zen tabs or the browser target changed; ask to open again",
                      say="Zen's tabs or target changed; please ask to open it again.")
    a["_source_revision"] = revision
    return q, state, open_tab(q, state["tabs"], state["current_window"])


def _open_check(a: dict) -> str | None:
    _, state, t = _open_state(a)
    if a.get("background") and not state["navigation_focus"]:
        raise JvError(1, "background browsing needs the updated heardback add-on", say=ADDON_OLD_SAY)
    if t:
        return None
    if not state["creation_guard"]:
        raise JvError(1, "current browser target unavailable; reload the heardback add-on",
                      say=ADDON_OLD_SAY)
    if state["current_identity"] != "known":
        raise JvError(1, "current browser tab is uncertain", say="Zen's current tab is uncertain; please ask again.")
    q = _open_query(a["query"])
    label = _host(q if "://" in q else "https://" + q) if re.fullmatch(URL_RE, q, re.I) else _page_label(q)
    return _readback(f"Open {label} in a new tab?")


def _open(a: dict, *, focus: bool = True) -> dict:
    q, state, t = _open_state(a)
    background = _background(a)
    if background and not state["navigation_focus"]:
        raise JvError(1, "background browsing needs the updated heardback add-on", say=ADDON_OLD_SAY)
    if t:
        if not focus:
            raise JvError(1, "a matching tab became visible; ask again", say="That tab is now visible; please ask again.")
        return _browse_tab(t, state, background=background)
    if not state["creation_guard"] or state["current_identity"] != "known" \
            or a.get("source_revision") != a["_source_revision"]:
        raise JvError(3, "new tab needs confirmation", confirm=_open_check(a), source_revision=a["_source_revision"])
    target = {"windowId": state["current_window"], "expectedCurrentTab": state["current_tab"]}
    if state["navigation_focus"] and focus:
        target["active"] = not background
    if not background:
        media.claim_browser()
    if re.fullmatch(URL_RE, q, re.I):
        r = call("create", {"url": q if re.match(r"https?://", q, re.I) else f"https://{q}", **target})
    else:
        r = call("search", {"query": q, **target})
    result = {"tabId": r.get("tabId"), "windowId": r.get("windowId"), "url": r.get("url") or q,
              "title": r.get("title") or "",
              "query": q, "source_revision": a["_source_revision"], "coverage": state["coverage"],
              "say": f"opened {_page_label(q)}"}
    return _browse_result(result, result["say"], state, background=background) if focus else result


# ytsearch: "X on youtube" / "youtube for X" -> YouTube's results; a trailing "on google" is just dropped
_YT = r"you ?tube"
YT_QUERY = re.compile(rf"(?:(?P<a>.+?) (?:on|in) {_YT}|{_YT} (?:for )?(?P<b>.+?))[.!?]?", re.I)
GOOGLE_TAIL = re.compile(r"\s+(?:on|in) google[.!?]?$", re.I)


def _search(a: dict) -> dict:
    q = a["query"].strip()
    state, background = inventory(), _background(a)
    if background and not state["navigation_focus"]:
        raise JvError(1, "background browsing needs the updated heardback add-on", say=ADDON_OLD_SAY)
    mode = {"active": not background} if state["navigation_focus"] else {}
    if not background:
        media.claim_browser()
    if m := YT_QUERY.fullmatch(q):
        x = (m["a"] or m["b"]).strip()
        r = call("create", {"url": YT_SEARCH + quote_plus(x), **mode})
        return _browse_result(r, f"searched YouTube for {x}", state, background=background)
    q = GOOGLE_TAIL.sub("", q) or q
    r = call("search", {"query": q, **mode})
    return _browse_result(r, f"searched for {q}", state, background=background)


_SEP = re.compile(r" [|\-–] ")
_BRACKETS = re.compile(r"[\[(【][^\])】]*[\])】]")
_LATIN = re.compile(r"[A-Za-z\u00C0-\u024F]")


def _short(title: str, words: int = 8) -> str:
    """Speakable title: first " | "/" - "/" – " cut (if >= 2 words before), then only words with a Latin letter;
    a bare number stays only right after a kept word. "" when nothing is left (callers fall back)."""
    title = _BRACKETS.sub(" ", title)   # "[beats to relax/study to]", "(Official Video)": noise when spoken
    head = _SEP.split(title, maxsplit=1)[0]
    if len(head.split()) >= 2:
        title = head
    w, prev = [], False
    for t in title.split():
        prev = bool(_LATIN.search(t)) or (prev and t.isdigit())
        if prev:
            w.append(t)
    if w:
        w[-1] = w[-1].rstrip(",;:|-–")
    return " ".join(w[:words]) + (" …" if len(w) > words else "")


def _play(a: dict) -> dict:
    background = _background(a)
    q = a["query"].strip()
    if result_number(q) is not None:
        raise JvError(2, "say which result, like pick result 2")
    state = inventory()
    mode = {"active": not background} if state["navigation_focus"] else {}
    # bgplay: reads the results page's data, so a hidden window (never renders the results list) still plays.
    try:
        v = call("yt_first", {"query": q}, timeout=YT_TIMEOUT)
        r = {"title": v.get("title", ""), "url": YT_WATCH + v["video_id"]}
        created = call("create", {"url": r["url"], **mode})
        r.update(tabId=created["tabId"], windowId=created.get("windowId"))
    except JvError as e:
        if e.extra.get("say") and not e.extra.get("stale"):   # not connected / no reply: the fallback can't do better
            raise
        r = _play_from_results(q, mode)
    media.claim()   # duck: the music paused for this listen stays paused under the new video
    r = _said(r, f"playing {_short(r['title'] or q) or 'the video'}")
    return r if background else _browse_result(r, r["say"], state)


def zen_prefs() -> Path | None:
    """prefs.js of the profile Zen runs: the install's Default, else the Default=1 profile, else the first."""
    root = Path(ZEN_DIR).expanduser()
    ini = configparser.ConfigParser(interpolation=None)
    if not ini.read(root / "profiles.ini"):
        return None
    paths = [ini[s]["Default"] for s in ini.sections() if s.startswith("Install") and "Default" in ini[s]]
    profs = [ini[s] for s in ini.sections() if s.startswith("Profile") and "Path" in ini[s]]
    paths += [p["Path"] for p in sorted(profs, key=lambda p: p.get("Default") != "1")]
    return root / paths[0] / "prefs.js" if paths else None


@functools.lru_cache(maxsize=4)
def _autoplay_pref(path: Path, mtime: float) -> int:
    m = AUTOPLAY_PREF.search(path.read_text(errors="replace"))
    return int(m[1]) if m else 1   # unset = Firefox's default, 1 (block audible)


def autoplay_allowed() -> bool:
    """True only if Zen's prefs say media.autoplay.default = 0; unreadable counts as maybe blocked."""
    try:
        p = zen_prefs()
        return bool(p) and _autoplay_pref(p, p.stat().st_mtime) == 0
    except (OSError, configparser.Error):
        return False


def autoplay_blocked(tab: int, url: str) -> str | None:
    """None once the video in `tab` plays, else why not (`play blocked: NotAllowedError`, ...).
    Firefox gives a gesture-less tab no allowed way to start audibly (MPRIS, add-on play(), muted-then-unmute all
    fail: reports/autoplay.md), so this only finds out. Bounded: AUTOPLAY_WAIT + one page call."""
    vid = url.rsplit("v=", 1)[-1]
    deadline = time.monotonic() + AUTOPLAY_WAIT
    while time.monotonic() < deadline:
        if any(t["id"] == tab and t["audible"] for t in tabs()):
            return None
        try:   # the tab's audible flag can lag; Zen's MPRIS player shows our url as soon as it plays
            if any(vid in p["url"] and p["status"] == "Playing" and not p["ducked"] for p in media.players()):
                return None
        except JvError:
            pass
        time.sleep(0.3)
    try:   # play() on a loaded but blocked video rejects with NotAllowedError
        call("video", {"tabId": tab, "action": "play"})
        return None
    except JvError as e:
        return e.msg


def _said(r: dict, say: str) -> dict:
    """The result of opening a YouTube video: says so plainly when autoplay left it paused; reason in the trace."""
    if not (r.get("tabId") and r.get("url", "").startswith(YT_WATCH)) or autoplay_allowed():
        return {**r, "say": say}   # allowed in prefs: no wait (manager: don't pay 1-3 s for a block that is off)
    why = autoplay_blocked(r["tabId"], r["url"])
    if why and why.startswith("play blocked"):
        say = BLOCKED_SAY.replace("Opened it", f"Opened {_short(r.get('title') or '') or 'the video'}")
    return {**r, "say": say, **({"trace": {"autoplay": why}} if why else {})}


def _play_from_results(q: str, mode: dict) -> dict:
    """Fallback (yt_first failed): open the results page and take its first video from the page."""
    created = call("create", {"url": YT_SEARCH + quote_plus(q), **mode})
    tab = created["tabId"]
    try:   # replace: the video takes the results page's place in history
        r = call("click_result", {"tabId": tab, "n": 1, "replace": True}, timeout=PAGE_TIMEOUT)
    except JvError as e:
        if e.extra.get("stale"):
            raise
        raise JvError(1, f"opened the YouTube results for {q} but couldn't start a video: {e.msg}",
                      **{**e.extra, "say": f"I opened the YouTube results for {q}, but couldn't start a video."})
    return {"title": r.get("title", ""), "url": r.get("url", ""), "tabId": r.get("tabId", tab),
            "windowId": r.get("windowId", created.get("windowId"))}


def _pick(a: dict) -> dict:
    n = int(a["n"])
    try:
        r = call("click_result", {"n": n}, timeout=PAGE_TIMEOUT)
    except JvError as e:   # not connected / no reply keep their own line
        raise JvError(1, e.msg, **{"say": f"I couldn't find result {n} on this page.", **e.extra})
    media.claim()
    r = _focus_result({"title": r.get("title", ""), "url": r.get("url", ""), "tabId": r.get("tabId"),
                       "windowId": r.get("windowId")}, f"opening result {n}, {_short(r.get('title', ''))}".rstrip(", "))
    return _said({**r, "observed": r.get("focus_confirmed") is True}, r["say"])


def _read(a: dict) -> dict:
    r = call("page_text", timeout=6.0)
    return {"title": r.get("title", ""), "url": r.get("url", ""), "text": r.get("text", ""),
            "tabId": r.get("tabId"), "windowId": r.get("windowId"),
            "truncated": bool(r.get("truncated")), "say": f"read {r.get('title') or 'the page'}"}


def video_tabs(action: str, ts: list[dict]) -> list[dict]:
    """Where the video probably is: playing tabs (not for play), then active tabs, then YouTube videos, recent first."""
    yt = [t for t in ts if re.match(r"https://(www\.|m\.)?youtube\.com/watch", t["url"])]
    order = ([t for t in ts if t["audible"]] if action != "play" else []) + [t for t in ts if t["active"]] + yt
    seen, out = set(), []
    for t in order:
        if t["id"] not in seen:
            seen.add(t["id"])
            out.append(t)
    return out


def _video(a: dict) -> dict:
    action = a.get("action") or "toggle"
    # An inventory/action failure cannot prove the command was not dispatched.
    if r := media.zen(action):
        return {**r, "observed": r.get("verification") not in (None, "playback unverified")}
    last = "no video tab open"
    for t in video_tabs(action, sorted(tabs(), key=lambda t: t["idle_h"])):
        media.claim()
        try:
            r = call("video", {"tabId": t["id"], "action": action})
        except JvError:
            raise   # a reply failure cannot prove the page command was not dispatched
        # Page response is command acceptance, not playback evidence.
        if action == "play" or action == "toggle" and not r.get("paused"):
            _, samples, reason = media._verify_play(None, None, t)
            if not reason:
                raise JvError(1, "page playback unverified", say="I couldn't verify the video playing.",
                              dispatched=True, samples=samples, tabId=t["id"])
        elif r.get("paused") is not True or r.get("via") == "button":
            raise JvError(1, "page pause unverified", say="I couldn't verify the video paused.", dispatched=True)
        return {"title": t["title"], "paused": r.get("paused"), "observed": True,
                "say": "paused" if r.get("paused") else "playing"}
    raise JvError(1, last)


def _close_check(a: dict) -> str | None:
    ts, guessed = close_matches(a)
    if not ts:
        raise JvError(1, "no matching tab")
    # The yes must close the tabs the read-back named (live 16:59: a tab switch between them closed another tab).
    revision = "zen:" + hashlib.sha256(json.dumps(sorted(t["id"] for t in ts)).encode()).hexdigest()
    if a.get("source_revision") and a["source_revision"] != revision:
        raise JvError(1, "tabs changed since the read-back", say="The tabs changed; please ask again.")
    a["_source_revision"] = revision
    if guessed:
        return f"Close '{_short(ts[0]['title'], 6) or 'this tab'}'?"   # live 20:24: full titles are long
    if len(ts) < 2:
        return None
    if not a.get("ids") and (name := _shared(ts, a.get("query"))):
        return f"Close {len(ts)} {name} tabs?"
    return f"Close {_n(len(ts), 'tab')}: {_titles(ts)}?"


def _close(a: dict) -> dict:
    ts, _ = close_matches(a)
    if not ts:
        raise JvError(1, "no matching tab")
    n = _remove(ts)
    return {"closed": n, "observed": n == len(ts),
            "say": f"closed {_short(ts[0]['title'], 6) or 'the tab'}" if n == 1 else f"closed {_n(n, 'tab')}"}


def _unused_check(a: dict) -> str | None:
    ts, h = unused(a.get("hours", 24)), a.get("hours", 24)
    if not ts:
        return None
    return f"Close {_n(len(ts), 'tab')} unused for {'a day' if h == 24 else f'{h} hours'}? Biggest: {_titles(ts)}"


def _close_unused(a: dict) -> dict:
    ts = unused(a.get("hours", 24))
    if not ts:
        return {"closed": 0, "observed": True, "say": "No unused tabs."}
    n = _remove(ts)
    return {"closed": n, "observed": n == len(ts), "say": f"closed {_n(n, 'tab')}"}


def claude_tab() -> dict:
    """A pinned Claude.ai conversation; distinct conversations require an explicit choice."""
    state = inventory()
    ts = [t for t in state["tabs"] if t["pinned"] and urlsplit(t["url"]).scheme == "https"
          and urlsplit(t["url"]).hostname == "claude.ai"]
    if not ts:
        raise JvError(1, "no pinned claude tab", say="Please open Claude.ai and pin the chat tab first.")
    return {**_choose_tab(ts, "Claude.ai", state["current_window"]), "ask_evidence": state["ask_evidence"]}


def _ask(a: dict) -> dict:
    t = claude_tab()
    if not t["ask_evidence"]:
        raise JvError(1, "Claude.ai evidence protocol unavailable", say=ADDON_OLD_SAY)
    activated = call("activate", {"tabId": t["id"]})
    ws = focus_zen_client(activated.get("title") or t["title"])
    target = {"tabId": t["id"], "windowId": t["window"], "url": t["url"], "workspace": ws,
              "activation_confirmed": activated.get("tabId") == t["id"], "focus_confirmed": ws is not None}
    focus = "" if ws is not None else " Please bring Zen to the front."
    try:
        r = call("type", {"tabId": t["id"], "text": a["text"], "send": not a.get("no_send"),
                          "expectedUrl": t["url"]})
    except JvError as e:
        raise JvError(1, f"Claude.ai request failed: {e.msg}", **target, submission="unknown",
                      say="I couldn't confirm the Claude.ai request. Check the chat before trying again." + focus)
    verified = r.get("protocol") == 1 and r.get("tabId") == t["id"] and r.get("windowId") == t["window"]
    if not verified:
        return {**target, "inserted": None, "submission": "unknown", "say":
                "I couldn't confirm typing or sending. Update the heardback add-on and check Claude.ai before trying again." + focus}
    outcome = {**target, "inserted": r.get("inserted") is True, "submission": r.get("submission"),
               "observation": r.get("observation"), "reason": r.get("reason")}
    if outcome["submission"] == "observed" and outcome["inserted"] \
            and outcome["observation"] == "composer_cleared_and_generation_started" and not a.get("no_send"):
        say = "Sent to Claude.ai."
    elif outcome["inserted"] and outcome["submission"] != "not_attempted":
        say = "Typed in Claude.ai, but sending isn't confirmed. Check the chat before trying again."
    elif outcome["inserted"]:
        say = "Typed in Claude.ai without sending."
    else:
        say = ("Claude.ai has a draft; I left it untouched." if r.get("reason") == "existing draft left untouched"
               else "I couldn't confirm typing in Claude.ai. Check the chat before trying again.")
        raise JvError(1, "Claude.ai insertion not confirmed", **outcome, say=say + focus)
    return {**outcome, "say": say + focus}


def _ping(a: dict) -> dict:
    n = call("ping")["tabs"]
    return {"tabs": n, "say": f"zen is connected, {_n(n, 'tab')}"}


def _install(a: dict) -> dict:
    host = shutil.which("zen-host")
    if not host:
        raise JvError(1, "run: uv tool install --editable .")
    d = Path(os.path.expanduser(os.environ.get("JV_ZEN_HOST_DIR") or HOST_DIRS[0]))
    d.mkdir(parents=True, exist_ok=True)
    manifest = d / f"{HOST_NAME}.json"
    manifest.write_text(json.dumps({"name": HOST_NAME, "description": "heardback", "path": host,
                                    "type": "stdio", "allowed_extensions": [EXT_ID]}, indent=2) + "\n")
    xpi = REPO / XPI
    xpi.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(xpi, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted((REPO / "extension").iterdir()):
            if f.is_file():
                z.write(f, f.name)
    say = ("in Zen: about:config, set xpinstall.signatures.required to false; "
           f"about:addons, gear, Install Add-on From File, pick {xpi}; then run jv zen ping")
    return {"manifest": str(manifest), "xpi": str(xpi), "say": say}


def _obj(props: dict, required: list[str] = ()) -> dict:
    return {"type": "object", "properties": props, "required": list(required)}


S, TEXT = {"type": "string"}, {"type": "string", "maxLength": 4000}
BACKGROUND = {"type": "boolean", "description": "Keep desktop focus and leave a new tab inactive; reused tabs are not activated."}
PLAY_BACKGROUND = {phrase: {"background": True}
                   for site in ("on youtube", "in youtube", "on you tube")
                   for phrase in (f"play {{query:text}} {site} in (the )?background",
                                  f"play {{query:text}} in (the )?background {site}")}
VERBS = [
    Verb("zen.list", _list, "List query-visible Zen tabs (current browser window or all returned windows); "
         "other internal Zen workspaces may be unseen. active is per window; current_tab is explicit.",
         _obj({"window": {"type": "string", "enum": ["current", "all"]}}), ["--window"],
         args={"window": {"type": "enum", "choices": ["current", "all"]}}, phrases=["list tabs"]),
    Verb("zen.switch", _switch, "Switch to a Zen tab by title or URL and focus its window. background only finds it, without activation."
         " Pass background true only for \"in the background\", \"don't switch\" or \"without switching\".",
         _obj({"query": S, "background": BACKGROUND}, ["query"]), ["query", "--background"], args={"query": {"type": "str"}},
         phrases=["switch to tab {query:str}"], observes=True),
    Verb("zen.open", _open,
         "Open a site in Zen and focus Zen (no need to open Zen first): switches to an open tab whose host "
         "matches that URL or site name. Different matching pages are ambiguous. As other Zen workspaces may "
         "be unseen, creating a new tab requires confirmation. Not for searches: use zen.search. "
         "background keeps focus and leaves new tabs inactive. source_revision binds that choice and is opaque approval metadata; never invent it."
         " Websites like WhatsApp, YouTube or Gmail are Zen tabs: open them with zen_open and the site name (it reuses an open tab). \"Search for X\" or \"look up X\" is zen_search, even when X is a site name; zen_open is for opening a site. \"Open Spotify\" means hypr_open with app Spotify; an explicit Spotify website or Spotify in Zen means zen_open with query spotify. Opening alone is not playback. \"Open Claude.ai\" or \"open Claude web\" is zen_open with query \"claude.ai\". Let the confirmation ask before a possible duplicate; never retry the request as zen_search, zen_play or another creation path to bypass the question, and never use another creation tool after a denied confirmation. Different matching pages require clarification; a missing tab is unseen, not proven closed. Pass background true only for an explicit \"in the background\", \"don't switch\" or \"without switching\"; preserve background and source_revision together during confirmation. Report any focus limitation from the tool's say; don't claim Zen is on screen without confirmation, and a focus limitation never authorizes opening another tab. Speak service names, not URLs.",
         _obj({"query": TEXT, "background": BACKGROUND,
               "source_revision": {"type": "string", "pattern": r"^zen:[0-9a-f]{64}$"}}, ["query"]),
         ["query", "--source-revision", "--background"], confirm="auto", check=_open_check, args={"query": {"type": "text"}},
         phrases=["open site {query:text}", "open( up)? {query:text} in zen", "open( up)? {query:text} on zen",
                  "open claude.ai", "open claude ai", "open claude web", "open spotify website", "open spotify site"],
         phrase_args={**{p: {"query": "claude.ai"} for p in ("open claude.ai", "open claude ai", "open claude web")},
                      **{p: {"query": "spotify"} for p in ("open spotify website", "open spotify site")}}),
    Verb("zen.search", _search, "Web-search the words in a new Zen tab and focus Zen; never reuses an open tab. "
         "\"search X on youtube\" / \"youtube X\" opens YouTube's results for X here (pass the whole "
         "phrase; not zen.play, which starts a video). background leaves the results inactive and keeps desktop focus."
         " Only an explicit search or look-up request authorizes zen_search. Pass background true only for \"in the background\", \"don't switch\" or \"without switching\".",
         _obj({"query": TEXT, "background": BACKGROUND}, ["query"]), ["query", "--background"], args={"query": {"type": "text"}},
         phrases=["search (for )?{query:text}", "google {query:text}"]),
    Verb("zen.play", _play,
         "Play a video: search YouTube for the words and open the first video in a new Zen tab, one step; "
         "Zen comes to the front for a video; set background true for a song or music. "
         "Use for \"play X\" unless the user says Spotify."
         " \"Play X\" (a song, video, artist or show) is zen_play with X, background true for a song, artist or music, false for a video, show or episode (\"and show it\" always false), unless the user says Spotify; never use zen_search or zen_open of YouTube. \"Play X on Spotify\" with a named song or artist: only the existing website search is supported; zen_open open.spotify.com/search/X is a search where they can pick an item, not verified playback. If they explicitly require the desktop app, say named-track search in that app is unsupported; never switch to the website.",
         _obj({"query": TEXT, "background": BACKGROUND}, ["query"]), ["query", "--background"], args={"query": {"type": "text"}},
         phrases=["play {query:text} on youtube", "play {query:text} in youtube", "play {query:text} on you tube",
                  *PLAY_BACKGROUND], phrase_args=PLAY_BACKGROUND),
    Verb("zen.pick", _pick, "Open result N (1 = first) of the search results on the current Zen tab "
         "(YouTube or Google), in the same tab."
         " \"Play\" or \"pick the Nth result\" (or video) after a search is zen_pick with N.",
         _obj({"n": {"type": "integer", "minimum": 1, "maximum": 20}}, ["n"]), ["n"], args={"n": {"type": "int"}},
         phrases=["pick result {n:int}", "pick {n:int} result( from your search)?", "play result {n:int}",
                  "play {n:int} result", "open result {n:int}", "open {n:int} result", "play {n:int} video",
                  "open {n:int} video"]),
    Verb("zen.read", _read, "Read the current Zen tab's text (title, URL, up to 8000 chars). Only when the user "
         "asks about the page (\"read / summarise this page\", \"what does this page say about X\")."
         " \"This page\", \"this tab\", \"this article\" or \"this video\" in a question (read, summarise, what does it say about X) means the current Zen tab: call zen_read, then answer from its text in one or two sentences; read a page only when the user asks about it.", _obj({}), []),
    Verb("zen.video", _video, "Play, pause or toggle the video in Zen (Zen's media player via MPRIS first; "
         "else the playing tab, the current tab, the latest YouTube video)."
         " \"Pause\", \"resume\" or \"play the video\" is zen_video. Music, a song or audio in any app, Spotify in Zen included (stop, pause, resume, next, skip, previous, \"what's playing\"), is media_media, never zen_video.",
         _obj({"action": {"type": "string", "enum": ["play", "pause", "toggle"]}}, ["action"]), ["action"],
         args={"action": {"type": "enum", "choices": ["play", "pause", "toggle"]}},
         phrases=["pause video", "resume video", "play video", "unpause video", "stop video"],
         phrase_args={"pause video": {"action": "pause"}, "stop video": {"action": "pause"},
                      "resume video": {"action": "play"}, "play video": {"action": "play"},
                      "unpause video": {"action": "play"}}, observes=True),
    Verb("zen.close", _close, "Close Zen tabs matching a query (or by --ids); never pinned tabs.",
         _obj({"query": S, "ids": {"type": "string", "pattern": r"^\d+(,\d+)*$"},
               "source_revision": {"type": "string", "pattern": r"^zen:[0-9a-f]{64}$"}}), ["query", "--ids", "--source-revision"],
         confirm="auto", check=_close_check, args={"query": {"type": "str"}},
         phrases=["close tab {query:str}", "close {query:str} tab(s)?", "close {query:str} on zen", "close {query:str} in zen",
                  "close {query:str} tab(s)? on zen", "close {query:str} tab(s)? in zen"], observes=True),
    Verb("zen.close-unused", _close_unused,
         "Close tabs idle for N hours (default 24); never pinned, playing, active or claude.ai tabs.",
         _obj({"hours": {"type": "integer", "minimum": 1}}), ["--hours"], confirm=True, check=_unused_check,
         phrases=["close (all )?unused (zen )?tabs", "close (all )?idle (zen )?tabs", "close (all )?old (zen )?tabs",
                  "close (all )?(zen )?tabs (that )?i havent used", "close (all )?(zen )?tabs (that )?i have not used",
                  "close (all )?(zen )?tabs (that )?i didnt use", "close (all )?(zen )?tabs (that )?i did not use"], observes=True),
    Verb("zen.ask", _ask, "Type into the existing pinned Claude.ai chat and attempt to send (--no-send: just type). "
         "Cannot create a new chat/session. Use the returned insertion/submission evidence and say; never retry "
         "an uncertain send or claim a new session was created."
         " \"Ask Claude.ai X\" is zen_ask with X for the existing pinned chat. A request for a NEW Claude.ai/web chat or session cannot be fulfilled: say new web chat creation isn't supported yet and ask the user to open a new chat and pin it first. Never silently reuse a pinned chat for a requested new session. Speak the tool's say, preserve typed-only and unverified states, and never retry in the same turn after uncertain insertion or submission. \"Sent\" requires observed submission.",
         _obj({"text": TEXT, "no_send": {"type": "boolean"}}, ["text"]), ["text", "--no-send"],
         args={"text": {"type": "text"}}, phrases=["ask (in )?claude {text:text}", "ask (in )?claude.ai {text:text}"]),
    Verb("zen.ping", _ping, "Check the Zen bridge is connected and count tabs.", _obj({}), []),
    Verb("zen.install", _install, "Install the native-messaging manifest and build the extension .xpi.",
         _obj({}), [], voice=False),
]
