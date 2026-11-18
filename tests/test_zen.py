import json
import shutil
import socket
import socketserver
import threading
import time
import zipfile

import pytest

from jv import zen
from jv.cli import all_verbs
from jv.verb import JvError
from jv.zen_host import sock_path
from test_hypr import _fake, fake_hypr  # noqa: F401  (hypr fake lives there)


def _serve(handler):
    srv = socketserver.ThreadingUnixStreamServer(sock_path(), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


ID_CLAUDE, ID_AUDIO, ID_ACTIVE, ID_DOCS, ID_BLOG = 1, 2, 3, 4, 5
TITLES = {ID_CLAUDE: "Claude", ID_AUDIO: "Lofi Radio", ID_ACTIVE: "Inbox", ID_DOCS: "Old Docs", ID_BLOG: "Old Blog"}
ZEN_CLIENTS = [{"address": "0xddd", "class": "zen", "title": "Old Docs — Zen Browser", "workspace": {"id": 2},
                "focusHistoryID": 0},
               {"address": "0xeee", "class": "zen", "title": "Claude — Zen Browser", "workspace": {"id": 2},
                "focusHistoryID": 1}]


def _tabs():
    now = time.time() * 1000
    t = lambda i, url, h, **kw: {"id": i, "windowId": 1, "title": TITLES[i], "url": url, "pinned": False,
                                 "active": False, "audible": False, "lastAccessed": now - h * 3.6e6, **kw}
    return [t(ID_CLAUDE, "https://claude.ai/new", 50, pinned=True), t(ID_AUDIO, "https://music.example.com/", 30, audible=True),
            t(ID_ACTIVE, "https://mail.example.com/", 60, active=True), t(ID_DOCS, "https://docs.example.com/", 30),
            t(ID_BLOG, "https://blog.example.com/", 40)]


@pytest.fixture(autouse=True)
def _no_autoplay_wait(monkeypatch, tmp_path):   # autoplay: no prefs (= maybe blocked), no poll, straight to play()
    monkeypatch.setattr(zen, "AUTOPLAY_WAIT", 0)
    monkeypatch.setattr(zen, "ZEN_DIR", str(tmp_path / "no-zen"))


@pytest.fixture
def fake_zen(runtime_dir):
    servers = []

    def start(replies: dict | None = None) -> list:
        """Default replies for list/activate/create/search/remove, overridden by `replies`."""
        ops = []
        base = {"list": {"ok": True, "current_window": 1, "current_tab": ID_ACTIVE, "creation_guard": True,
                         "ask_evidence": 1, "navigation_focus": 1, "tabs": _tabs()},
                "activate": lambda a: {"ok": True, "tabId": a["tabId"], "title": TITLES.get(a["tabId"], ""), "windowId": 1,
                                       "target_validated": True, "activation_confirmed": True,
                                       "browser_focus_requested": True, "browser_focus_observed": True},
                "create": {"ok": True, "tabId": 9, "windowId": 1}, "search": {"ok": True, "tabId": 9, "windowId": 1},
                "remove": lambda a: {"ok": True, "closed": a["tabIds"]}, "video": {"ok": True, "paused": False}}
        base.update(replies or {})

        class H(socketserver.StreamRequestHandler):
            def handle(self):
                req = json.loads(self.rfile.readline())
                ops.append(req)
                r = base[req["op"]]
                self.wfile.write((json.dumps(r(req["args"]) if callable(r) else r) + "\n").encode())
        servers.append(_serve(H))
        return ops
    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


def test_ping(fake_zen, run_jv):
    ops = fake_zen({"ping": {"ok": True, "tabs": 4}})
    out = run_jv("zen", "ping", "--json")[1]
    assert out["tabs"] == 4 and out["say"] == "zen is connected, 4 tabs" and ops == [{"op": "ping", "args": {}}]


def test_no_socket(runtime_dir, run_jv):
    code, _, err = run_jv("zen", "ping")
    assert code == 1 and "zen bridge not connected" in err


def test_call_reply_error(fake_zen):
    fake_zen({"list": {"ok": False, "reason": "nope"}})
    with pytest.raises(JvError, match="nope"):
        zen.call("list")


def test_call_timeout(runtime_dir):
    hold = threading.Event()

    class Silent(socketserver.StreamRequestHandler):
        def handle(self):
            hold.wait(5)
    srv = _serve(Silent)
    try:
        t = time.monotonic()
        with pytest.raises(JvError, match="did not reply"):
            zen.call("ping", timeout=0.2)
        assert time.monotonic() - t < 1
    finally:
        hold.set()
        srv.shutdown()
        srv.server_close()


def test_install_writes_manifest_and_xpi(tmp_path, monkeypatch, run_jv):
    shutil.copytree(zen.REPO / "extension", tmp_path / "extension")
    monkeypatch.setattr(zen, "REPO", tmp_path)
    monkeypatch.setenv("JV_ZEN_HOST_DIR", str(tmp_path))
    monkeypatch.setattr(shutil, "which", lambda n: "/x/zen-host")
    assert run_jv("zen", "install")[0] == 0
    m = json.loads((tmp_path / "jarvis_zen.json").read_text())
    assert m == {"name": "jarvis_zen", "description": "heardback", "path": "/x/zen-host", "type": "stdio",
                 "allowed_extensions": ["jarvis@zydiag.local"]}
    assert set(zipfile.ZipFile(zen.REPO / zen.XPI).namelist()) >= {"manifest.json", "background.js"}


def test_install_needs_zen_host(tmp_path, monkeypatch, run_jv):
    monkeypatch.setenv("JV_ZEN_HOST_DIR", str(tmp_path))
    monkeypatch.setattr(shutil, "which", lambda n: None)
    code, _, err = run_jv("zen", "install")
    assert code == 1 and "uv tool install --editable ." in err
    assert not (tmp_path / "jarvis_zen.json").exists()


def test_install_hidden_from_voice():
    assert next(v for v in all_verbs() if v.name == "zen.install").voice is False


def test_manifest_matches_ids():
    m = json.loads((zen.REPO / "extension/manifest.json").read_text())
    assert m["browser_specific_settings"]["gecko"]["id"] == zen.EXT_ID
    assert zen.HOST_NAME in (zen.REPO / "extension/background.js").read_text()


def _ops(ops, name):
    return [o["args"] for o in ops if o["op"] == name]


def _confirmed_open(run_jv, *query):
    code, pending, _ = run_jv("zen", "open", *query, "--json")
    assert code == 3 and pending["source_revision"].startswith("zen:")
    return run_jv("zen", "open", *query, "--source-revision", pending["source_revision"], "--yes", "--json")


def _creation(url):
    return {"url": url, "windowId": 1, "expectedCurrentTab": ID_ACTIVE, "active": True}


def test_list_current_window(fake_zen, run_jv):
    fake_zen()
    out = run_jv("zen", "list", "--json")[1]
    assert out["say"].startswith("5 tabs visible") and out["tabs"][3]["title"] == "Old Docs" and out["tabs"][3]["idle_h"] == 30.0
    assert out["current_window"] == 1 and out["current_tab"] == ID_ACTIVE
    assert out["coverage"]["zen_workspaces"] == "unknown" and out["tabs"][2]["current"]
    assert out["tabs"][0]["windowId"] == 1
    assert run_jv("zen", "list", "--window", "nope")[0] == 2


def test_unused_never_pinned_active_audible_claude(fake_zen):
    fake_zen()
    assert [t["title"] for t in zen.unused(24)] == ["Old Blog", "Old Docs"]


def test_close_unused_confirm_then_remove(fake_zen, run_jv):
    ops = fake_zen()
    code, out, _ = run_jv("zen", "close-unused", "--json")
    assert code == 3 and out["confirm"].startswith("Close 2 tabs unused for a day? Biggest: Old Blog")
    assert not _ops(ops, "remove")
    assert run_jv("zen", "close-unused", "--yes")[0] == 0
    assert sorted(_ops(ops, "remove")[0]["tabIds"]) == [ID_DOCS, ID_BLOG]


def test_close_unused_none(fake_zen, run_jv):
    ops = fake_zen()
    code, out, _ = run_jv("zen", "close-unused", "--hours", "100", "--json")
    assert code == 0 and out["say"] == "No unused tabs." and not _ops(ops, "remove")


def test_close_single_no_confirm(fake_zen, run_jv):
    ops = fake_zen()
    from jv import media
    media.duck_path().write_text("firefox.instance_1_658\thttps://www.youtube.com/watch?v=x\tx\nspotify\t\tSong\n")
    code, out, _ = run_jv("zen", "close", "blog", "--json")
    assert code == 0 and out["say"] == "closed Old Blog" and _ops(ops, "remove") == [{"tabIds": [ID_BLOG]}]
    assert media.duck_path().read_text() == "spotify\t\tSong\n"   # Zen's row dropped, Spotify still resumes (live 15:39)


def test_close_many_confirms(fake_zen, run_jv):
    fake_zen()
    code, out, _ = run_jv("zen", "close", "old", "--json")
    assert code == 3 and out["confirm"] == "Close 2 Old tabs?"   # fix2 P10: all titles share the query


def test_close_never_pinned(fake_zen, run_jv):
    ops = fake_zen()
    assert run_jv("zen", "close", "--ids", str(ID_CLAUDE), "--yes")[0] == 1
    assert run_jv("zen", "close", "claude", "--yes")[0] == 1
    assert not _ops(ops, "remove")


def test_close_needs_query_or_ids(fake_zen, run_jv):
    fake_zen()
    assert run_jv("zen", "close")[0] == 2
    assert run_jv("zen", "close", "--ids", "4;rm")[0] == 2


def test_open_url_vs_search(fake_zen, monkeypatch, run_jv):
    _fake(monkeypatch, clients=ZEN_CLIENTS)
    ops = fake_zen()
    _confirmed_open(run_jv, "github.com/zydiag")
    code, out, err = _confirmed_open(run_jv, "rust borrow checker")
    assert _ops(ops, "create") == [_creation("https://github.com/zydiag")]
    assert _ops(ops, "search") == [{"query": "rust borrow checker", "windowId": 1, "expectedCurrentTab": ID_ACTIVE, "active": True}]
    assert out["say"].startswith("opened rust borrow checker") and out["workspace"] is None
    assert "hl.dsp.focus" not in err


def test_search_never_reuses_a_tab(fake_zen, monkeypatch, run_jv):   # "search for whatsapp" switched tabs
    _fake(monkeypatch, clients=ZEN_CLIENTS)
    ops = fake_zen(_with((10, "WhatsApp", "https://web.whatsapp.com/", 5)))
    code, out, err = run_jv("zen", "search", "whatsapp", "--json")
    assert code == 0 and _ops(ops, "activate") == [{"tabId": 9, "windowId": 1}]
    assert _ops(ops, "search") == [{"query": "whatsapp", "active": True}]
    assert out["say"].startswith("searched for whatsapp") and "hl.dsp.focus" not in err


@pytest.mark.parametrize("q,x", [("Shiv Thunder on YouTube", "Shiv Thunder"), ("lofi in youtube", "lofi"),
                                 ("lofi beats on you tube.", "lofi beats"), ("youtube shiv thunder", "shiv thunder"),
                                 ("YouTube for lofi hip hop", "lofi hip hop")])
def test_search_on_youtube_opens_results(fake_zen, monkeypatch, run_jv, q, x):   # live 2026-11-01 08:59
    _fake(monkeypatch, clients=ZEN_CLIENTS)
    ops = fake_zen()
    code, out, err = run_jv("zen", "search", "--json", "--", q)
    assert code == 0 and not _ops(ops, "search")
    assert _ops(ops, "create") == [{"url": zen.YT_SEARCH + x.replace(" ", "+"), "active": True}]
    assert out["say"].startswith(f"searched YouTube for {x}") and "hl.dsp.focus" not in err


@pytest.mark.parametrize("q,sent", [("lofi", "lofi"), ("lofi on google", "lofi"), ("weather in Google", "weather"),
                                    ("youtube", "youtube")])
def test_search_google_unchanged(fake_zen, monkeypatch, run_jv, q, sent):
    _fake(monkeypatch, clients=ZEN_CLIENTS)
    ops = fake_zen()
    code, out, err = run_jv("zen", "search", "--json", "--", q)
    assert code == 0 and _ops(ops, "search") == [{"query": sent, "active": True}] and not _ops(ops, "create")


def _with(*extra):
    now = time.time() * 1000
    return {"list": {"ok": True, "current_window": 1, "current_tab": ID_ACTIVE, "creation_guard": True, "navigation_focus": 1, "tabs": _tabs() + [
        {"id": i, "windowId": 1, "title": title, "url": url, "pinned": False, "active": False, "audible": False,
         "lastAccessed": now - h * 3.6e6} for i, title, url, h in extra]}}


@pytest.mark.parametrize("q,tab", [("whatsapp", 10), ("WhatsApp", 10), ("docs.example.com", ID_DOCS),
                                   ("https://docs.example.com/", ID_DOCS), ("github.com/zydiag", 12)])
def test_open_switches_to_open_tab(fake_zen, monkeypatch, run_jv, q, tab):
    _fake(monkeypatch, clients=ZEN_CLIENTS)
    titles = {10: "WhatsApp", 11: "GitHub", 12: "zydiag", ID_DOCS: "Old Docs"}
    ops = fake_zen({**_with((10, "WhatsApp", "https://web.whatsapp.com/", 5), (11, "GitHub", "https://github.com/", 1),
                            (12, "zydiag", "https://github.com/zydiag/heardback", 2)),
                    "activate": lambda a: {"ok": True, "tabId": a["tabId"], "windowId": 1, "title": titles[a["tabId"]],
                                           "target_validated": True, "activation_confirmed": True,
                                           "browser_focus_requested": True}})
    code, out, err = run_jv("zen", "open", q, "--json")
    assert code == 0 and _ops(ops, "activate") == [{"tabId": tab, "windowId": 1}] and out["say"].startswith("switched to ")
    assert "http" not in out["say"]
    assert out["tabId"] == tab and out["windowId"] == 1
    assert not _ops(ops, "create") and not _ops(ops, "search")


@pytest.mark.parametrize("q,op", [("how to build heardback", "search"), ("whatsapp web", "search"),
                                  ("web", "search"), ("com", "search"),
                                  ("github.co", "create"), ("github.com/zydiag/other", "create")])
def test_open_never_hijacks(fake_zen, monkeypatch, run_jv, q, op):
    _fake(monkeypatch, clients=ZEN_CLIENTS)
    ops = fake_zen(_with((10, "how to build heardback - Search", "https://web.whatsapp.com/", 5),
                         (12, "zydiag", "https://github.com/zydiag/heardback", 2),
                         (13, "about", "about:preferences", 0)))
    code, out, _ = _confirmed_open(run_jv, q)
    assert code == 0 and _ops(ops, "activate") == [{"tabId": 9, "windowId": 1}] and _ops(ops, op)
    assert out["say"].startswith(f"opened {zen._page_label(zen._open_query(q))}")


def test_switch_focuses_hypr(fake_zen, monkeypatch, run_jv):
    _fake(monkeypatch, clients=[ZEN_CLIENTS[0]])
    ops = fake_zen()
    from jv import media
    media.duck_path().write_text("mpd\t\tAkhar\n")
    code, out, err = run_jv("zen", "switch", "docs", "--json")
    assert code == 0 and out["workspace"] == 2 and not out["focus_confirmed"]
    assert out["observed"] is False and out["say"] == "I couldn't confirm that."
    assert _ops(ops, "activate") == [{"tabId": ID_DOCS, "windowId": 1}]
    assert err.count("address:0xddd") == 1
    assert media.duck_path().read_text() == "mpd\t\tAkhar\n"


def test_switch_without_hypr_still_ok(fake_zen, monkeypatch, run_jv):
    _fake(monkeypatch, inst="[]")
    fake_zen()
    code, out, _ = run_jv("zen", "switch", "blog", "--json")
    assert code == 0 and out["workspace"] is None


def test_ask_types_into_pinned_claude(fake_zen, monkeypatch, run_jv):
    _fake(monkeypatch, clients=[ZEN_CLIENTS[1]])
    ops = fake_zen({"type": {"ok": True, "protocol": 1, "tabId": ID_CLAUDE, "windowId": 1,
                             "inserted": True, "submission": "observed",
                             "observation": "composer_cleared_and_generation_started"}})
    code, out, err = run_jv("zen", "ask", "what is a monad", "--json")
    assert code == 0 and out["say"] == "Sent to Claude.ai." and out["focus_confirmed"] and "address:0xeee" in err
    assert out["tabId"] == ID_CLAUDE and out["activation_confirmed"]
    assert ops[-2] == {"op": "activate", "args": {"tabId": ID_CLAUDE}}
    assert ops[-1] == {"op": "type", "args": {"tabId": ID_CLAUDE, "text": "what is a monad", "send": True,
                                             "expectedUrl": "https://claude.ai/new"}}


def test_ask_no_send(fake_zen, fake_hypr, run_jv):
    ops = fake_zen({"type": {"ok": True, "protocol": 1, "tabId": ID_CLAUDE, "windowId": 1,
                             "inserted": True, "submission": "not_attempted"}})
    code, out, _ = run_jv("zen", "ask", "hi", "--no-send", "--json")
    assert code == 0 and out["say"].startswith("Typed in Claude.ai without sending.")
    assert ops[-1]["args"]["send"] is False


def test_ask_composer_missing(fake_zen, fake_hypr, run_jv):
    fake_zen({"type": {"ok": False, "reason": "composer not found"}})
    code, _, err = run_jv("zen", "ask", "x")
    assert code == 1 and "composer not found" in err


def test_ask_no_pinned_tab(fake_zen, fake_hypr, run_jv):
    ops = fake_zen({"list": {"ok": True, "current_window": 1, "tabs": [t for t in _tabs() if not t["pinned"]]}})
    code, _, err = run_jv("zen", "ask", "x")
    assert code == 1 and "no pinned claude tab" in err and [o["op"] for o in ops] == ["list"]


def test_ask_old_addon_never_dispatches(fake_zen, run_jv):
    ops = fake_zen({"list": {"ok": True, "tabs": _tabs()}})
    code, out, _ = run_jv("zen", "ask", "hi", "--json")
    assert code == 1 and out["say"] == zen.ADDON_OLD_SAY
    assert [o["op"] for o in ops] == ["list"]


@pytest.mark.parametrize("reply,phrase", [
    ({"ok": True}, "couldn't confirm typing or sending"),
    ({"ok": True, "protocol": 1, "tabId": ID_CLAUDE, "windowId": 1, "inserted": True,
      "submission": "unverified"}, "sending isn't confirmed"),
    ({"ok": True, "protocol": 1, "tabId": ID_CLAUDE, "windowId": 1, "inserted": False,
      "submission": "not_attempted", "reason": "existing draft left untouched"}, "draft"),
])
def test_ask_uncertain_or_draft_is_never_retried(fake_zen, monkeypatch, run_jv, reply, phrase):
    _fake(monkeypatch, clients=ZEN_CLIENTS)
    ops = fake_zen({"type": reply})
    _, out, _ = run_jv("zen", "ask", "hi", "--json")
    assert phrase in out["say"] and "Please bring Zen to the front" in out["say"]
    assert "Sent" not in out["say"] and len(_ops(ops, "type")) == 1 and not out["focus_confirmed"]


def test_ask_distinct_chats_need_choice(fake_zen, run_jv):
    base = _tabs()
    ops = fake_zen({"list": {"ok": True, "ask_evidence": 1,
                             "tabs": [base[0], {**base[0], "id": 9, "url": "https://claude.ai/chat/other"}]}})
    code, out, _ = run_jv("zen", "ask", "hi", "--json")
    assert code == 2 and len(out["candidates"]) == 2 and [o["op"] for o in ops] == ["list"]


@pytest.mark.parametrize("url", ["https://claude.ai.evil.example/new", "http://claude.ai/new"])
def test_ask_requires_exact_secure_claude_host(fake_zen, run_jv, url):
    ops = fake_zen({"list": {"ok": True, "ask_evidence": 1, "tabs": [{**_tabs()[0], "url": url}]}})
    assert run_jv("zen", "ask", "hi", "--json")[0] == 1
    assert [o["op"] for o in ops] == ["list"]


def test_claude_tab_equivalent_copies_prefer_current_browser_window(fake_zen, monkeypatch):
    base = _tabs()
    two = [{**base[0], "id": 7, "windowId": 9}, {**base[2], "id": 8, "windowId": 9, "title": "Elsewhere"},
           base[0], base[2]]
    fake_zen({"list": {"ok": True, "current_window": 1, "tabs": two}})
    _fake(monkeypatch, clients=[{**ZEN_CLIENTS[0], "workspace": {"id": 5}, "title": "Elsewhere — Zen"},
                                {**ZEN_CLIENTS[0], "address": "0xfff", "title": "Inbox — Zen"}])
    assert zen.claude_tab()["id"] == ID_CLAUDE


def test_focus_zen_client_never_falls_back_to_home_ws(monkeypatch, capsys):
    _fake(monkeypatch, clients=[{**ZEN_CLIENTS[0], "address": "0x111", "workspace": {"id": 5}, "title": "a"},
                                {**ZEN_CLIENTS[0], "address": "0x222", "focusHistoryID": 3, "title": "b"}])
    assert zen.focus_zen_client("lagging title") is None and "address:" not in capsys.readouterr().err


def test_focus_zen_client_lagging_title_off_home_ws(monkeypatch, capsys):
    # A lagging title supplies no browser-to-compositor identity, even with one visible client.
    _fake(monkeypatch, clients=[{**ZEN_CLIENTS[0], "address": "0x333", "workspace": {"id": 3}, "title": "old tab"}])
    assert zen.focus_zen_client("WhatsApp") is None
    assert "address:" not in capsys.readouterr().err


def test_focus_zen_client_title_collision_is_uncertain(monkeypatch, capsys):
    _fake(monkeypatch, clients=[{**ZEN_CLIENTS[0], "address": "0x111", "workspace": {"id": 5}},
                                {**ZEN_CLIENTS[0], "address": "0x222", "focusHistoryID": 3}])
    assert zen.focus_zen_client("Old Docs") is None and "address:" not in capsys.readouterr().err


def test_one_tab_singular(fake_zen, run_jv):
    fake_zen()
    code, out, _ = run_jv("zen", "close-unused", "--hours", "35", "--json")
    assert code == 3 and out["confirm"].startswith("Close 1 tab unused for 35 hours?")
    assert run_jv("zen", "close-unused", "--hours", "35", "--yes", "--json")[1]["say"] == "closed 1 tab"


def test_extension_types_via_json_only():
    js = (zen.REPO / "extension/background.js").read_text()
    assert "JSON.stringify(text)" in js and "JSON.stringify(CLAUDE_SELECTORS)" in js



def test_extension_all_sites_fixed_ops():   # zenplay: <all_urls>, and ops stay a fixed list (no "run this JS")
    import re
    from jv.zen_host import OPS
    m = json.loads((zen.REPO / "extension/manifest.json").read_text())
    assert m["version"] == "0.3.7" and "<all_urls>" in m["permissions"]
    js = (zen.REPO / "extension/background.js").read_text()
    body = js[js.index("const OPS = {"):js.index("async function handle")]
    assert set(re.findall(r"^  async (\w+)\(", body, re.M)) == OPS
    assert "eval(" not in js and "new Function" not in js
    assert "JSON.stringify(a)" in js   # page functions get their arguments as JSON only


def test_close_fuzzy_fallback_confirms(fake_zen, run_jv):
    ops = fake_zen()
    code, out, _ = run_jv("zen", "close", "old blgo", "--json")
    assert code == 3 and out["confirm"] == "Close 'Old Blog'?" and not _ops(ops, "remove")
    assert run_jv("zen", "close", "old blgo", "--yes")[0] == 3 and not _ops(ops, "remove")   # no revision: asks again
    rev = out["source_revision"]
    assert run_jv("zen", "close", "old blgo", "--yes", "--source-revision", rev)[0] == 0 and _ops(ops, "remove") == [{"tabIds": [ID_BLOG]}]


# --- zenplay: play / pick / read / video ---------------------------------------------------
VID = {"ok": True, "tabId": 9, "title": "Lofi Hip Hop Radio beats to relax study to 24/7 live", "kind": "youtube",
       "url": "https://www.youtube.com/watch?v=jfKfPfyJRdk"}


YT_MISS = {"yt_first": {"ok": False, "reason": "no video in the results"}}   # bgplay: falls back to the page


@pytest.mark.parametrize("background", [False, True])
def test_play_opens_the_first_video_from_data(fake_zen, monkeypatch, run_jv, background):
    _fake(monkeypatch, clients=[{**ZEN_CLIENTS[0], "title": "heardback"}])
    ops = fake_zen({"yt_first": {"ok": True, "video_id": "jfKfPfyJRdk", "title": VID["title"]}})
    focused = []
    from jv import hypr, media
    monkeypatch.setattr(hypr, "_active", lambda: "0xddd" if focused else "0xaaa")
    def dispatch(command):
        assert [o["op"] for o in ops] == ["list", "yt_first", "create", "video", "activate"]
        focused.append(command)
    monkeypatch.setattr(hypr, "dispatch", dispatch)
    media.duck_path().write_text("firefox.instance_1_658\n")   # duck: the jazz the voice paused for this listen
    code, out, err = run_jv("zen", "play", "lofi", "hip", "hop", *(["--background"] if background else []), "--json")
    assert code == 0 and [o["op"] for o in ops] == ["list", "yt_first", "create", "video", *([] if background else ["activate"])]
    assert _ops(ops, "video") == [{"tabId": 9, "action": "play"}]
    assert not media.duck_path().exists()   # claimed: the voice keeps the jazz paused (live 22:57:59)
    assert _ops(ops, "yt_first") == [{"query": "lofi hip hop"}]
    assert _ops(ops, "create") == [{"url": VID["url"], "active": not background}]
    assert out["say"] == "playing Lofi Hip Hop Radio beats to relax study …" and out["url"] == VID["url"]
    assert _ops(ops, "activate") == ([] if background else [{"tabId": 9, "windowId": 1}])
    assert focused == ([] if background else [hypr.FOCUS_WINDOW.format(addr="0xddd")])
    if background:
        assert "workspace" not in out
    else:
        assert out["workspace"] == 2 and out["focus_confirmed"] is True


@pytest.mark.parametrize("background", ["true", 1, None, []])
def test_play_rejects_bad_background_before_creating_a_tab(fake_zen, background):
    ops = fake_zen()
    with pytest.raises(JvError, match="background must be a boolean") as error:
        zen._play({"query": "video", "background": background})
    assert error.value.code == 2 and not ops


@pytest.mark.parametrize("miss", [YT_MISS, {"yt_first": {"ok": False, "error": "unknown op"}}], ids=["miss", "old-host"])
def test_play_falls_back_to_the_results_page(fake_zen, monkeypatch, run_jv, miss):
    _fake(monkeypatch, clients=ZEN_CLIENTS)
    ops = fake_zen({"click_result": VID, **miss})
    code, out, err = run_jv("zen", "play", "lofi", "hip", "hop", "--background", "--json")
    assert code == 0 and [o["op"] for o in ops] == ["list", "yt_first", "create", "click_result", "video"]
    assert _ops(ops, "create") == [{"url": "https://www.youtube.com/results?search_query=lofi+hip+hop", "active": False}]
    assert _ops(ops, "click_result") == [{"tabId": 9, "n": 1, "replace": True}]   # same tab, results page replaced
    assert out["say"] == "playing Lofi Hip Hop Radio beats to relax study …" and out["url"] == VID["url"]


BLOCKED = {"video": {"ok": False, "reason": "play blocked: NotAllowedError"}}
BLOCKED_VIDEO_SAY = zen.BLOCKED_SAY.replace("Opened it", "Opened " + zen._short(VID["title"]))


def test_play_says_autoplay_was_blocked(fake_zen, monkeypatch, run_jv):   # autoplay: no false "playing"
    from jv import cli
    _fake(monkeypatch, clients=ZEN_CLIENTS)
    fake_zen({"yt_first": {"ok": True, "video_id": "jfKfPfyJRdk", "title": VID["title"]}, **BLOCKED})
    code, out, err = run_jv("zen", "play", "lofi", "--background", "--json")
    assert code == 0 and out["say"] == BLOCKED_VIDEO_SAY and "trace" not in out
    assert cli._trace == {"autoplay": "play blocked: NotAllowedError"}   # into the jv run trace event
    assert "hl.dsp.focus" not in err   # still no focus (user)


def _profile(tmp_path, monkeypatch, prefs: str | None):
    root = tmp_path / "zen"
    (root / "abc.Default (release)").mkdir(parents=True)
    (root / "xyz.Default Profile").mkdir()
    (root / "profiles.ini").write_text("[Profile1]\nName=Default Profile\nIsRelative=1\nPath=xyz.Default Profile\n"
                                       "Default=1\n\n[Profile0]\nName=Default (release)\nIsRelative=1\n"
                                       "Path=abc.Default (release)\n\n[Install15B7]\nDefault=abc.Default (release)\n")
    (root / "xyz.Default Profile" / "prefs.js").write_text('user_pref("media.autoplay.default", 0);\n')   # not used
    if prefs is not None:
        (root / "abc.Default (release)" / "prefs.js").write_text(prefs)
    monkeypatch.setattr(zen, "ZEN_DIR", str(root))


@pytest.mark.parametrize("prefs,checked", [
    ('user_pref("browser.x", 1);\nuser_pref("media.autoplay.default", 0);\n', False),   # allowed: no wait
    ('user_pref("media.autoplay.default", 1);\n', True),
    ('user_pref("browser.x", 1);\n', True),   # unset = Firefox default 1 (block audible)
    (None, True),   # no prefs.js: maybe blocked
], ids=["allowed", "blocked", "unset", "missing"])
def test_play_checks_autoplay_only_when_prefs_may_block(fake_zen, monkeypatch, run_jv, tmp_path, prefs, checked):
    _fake(monkeypatch, clients=ZEN_CLIENTS)
    _profile(tmp_path, monkeypatch, prefs)
    ops = fake_zen({"yt_first": {"ok": True, "video_id": "jfKfPfyJRdk", "title": "Song"}, **BLOCKED})
    out = run_jv("zen", "play", "lofi", "--background", "--json")[1]
    assert [o["op"] for o in ops] == ["list", "yt_first", "create", *(["video"] if checked else [])]
    assert out["say"] == (zen.BLOCKED_SAY.replace("Opened it", "Opened Song") if checked else "playing Song")


def test_autoplay_pref_rereads_after_a_change(tmp_path, monkeypatch):   # cached by mtime, not forever
    import os
    _profile(tmp_path, monkeypatch, 'user_pref("media.autoplay.default", 0);\n')
    assert zen.autoplay_allowed()
    p = zen.zen_prefs()
    p.write_text('user_pref("media.autoplay.default", 1);\n')
    os.utime(p, (1, 1))
    assert not zen.autoplay_allowed()


def test_play_other_check_failure_keeps_playing_line(fake_zen, monkeypatch, run_jv):
    from jv import cli
    _fake(monkeypatch, clients=ZEN_CLIENTS)
    fake_zen({"yt_first": {"ok": True, "video_id": "jfKfPfyJRdk", "title": "Song"},
              "video": {"ok": False, "reason": "no video on this page"}})
    out = run_jv("zen", "play", "lofi", "--background", "--json")[1]
    assert out["say"] == "playing Song" and cli._trace == {"autoplay": "no video on this page"}


def test_play_audible_tab_needs_no_page_call(fake_zen, monkeypatch, run_jv):
    from jv import cli
    monkeypatch.setattr(zen, "AUTOPLAY_WAIT", 2.0)
    _fake(monkeypatch, clients=ZEN_CLIENTS)
    listing = _with((9, "Song - YouTube", VID["url"], 0))
    listing["list"]["tabs"][-1]["audible"] = True
    ops = fake_zen({"yt_first": {"ok": True, "video_id": "jfKfPfyJRdk", "title": "Song"}, **listing, **BLOCKED})
    t = time.monotonic()
    out = run_jv("zen", "play", "lofi", "--background", "--json")[1]
    assert time.monotonic() - t < 1 and out["say"] == "playing Song" and cli._trace == {}
    assert [o["op"] for o in ops] == ["list", "yt_first", "create", "list"]


@pytest.mark.parametrize("result,say", [
    (VID, BLOCKED_VIDEO_SAY),
    ({"ok": True, "tabId": 9, "title": "Docs", "url": "https://docs.example.com/"},
     "opening result 1, Docs. Please bring Zen to the front."),
], ids=["video", "not-a-video"])
def test_pick_says_autoplay_was_blocked_only_for_videos(fake_zen, monkeypatch, run_jv, result, say):
    _fake(monkeypatch, clients=ZEN_CLIENTS)
    ops = fake_zen({"click_result": result, **BLOCKED})
    assert run_jv("zen", "pick", "1", "--json")[1]["say"] == say
    assert bool(_ops(ops, "video")) == (say == BLOCKED_VIDEO_SAY)


def test_play_not_connected_skips_the_fallback(runtime_dir, run_jv):
    code, out, _ = run_jv("zen", "play", "lofi", "--json")
    assert code == 1 and out["say"] == "Zen isn't connected."


def test_play_no_video_says_results_open(fake_zen, monkeypatch, run_jv):
    _fake(monkeypatch, clients=ZEN_CLIENTS)
    fake_zen({"click_result": {"ok": False, "reason": "no search results on this page"}, **YT_MISS})
    code, _, err = run_jv("zen", "play", "asdfgh")
    assert code == 1 and "opened the YouTube results for asdfgh but couldn't start a video" in err


@pytest.mark.parametrize("argv,replies,say", [   # playfix: short plain spoken lines, detail stays in `error`
    (["play", "asdfgh"], {"click_result": {"ok": False, "reason": "no search results on this page"}, **YT_MISS},
     "I opened the YouTube results for asdfgh, but couldn't start a video."),
    (["pick", "3"], {"click_result": {"ok": False, "reason": "only 2 results"}}, "I couldn't find result 3 on this page."),
])
def test_zen_failures_say_one_plain_sentence(fake_zen, monkeypatch, run_jv, argv, replies, say):
    _fake(monkeypatch, clients=ZEN_CLIENTS)
    fake_zen(replies)
    code, out, _ = run_jv("zen", *argv, "--json")
    assert code == 1 and out["say"] == say and ":" not in say and out["error"] != say


def test_zen_not_connected_says_so(runtime_dir, run_jv):   # no zen-host socket
    code, out, _ = run_jv("zen", "pick", "2", "--json")
    assert code == 1 and out["say"] == "Zen isn't connected." and "not connected" in out["error"]


def test_play_no_video_logs_the_layout(fake_zen, monkeypatch, run_jv):   # playfix: diag in the JSON, for the log
    _fake(monkeypatch, clients=ZEN_CLIENTS)
    diag = "watch_links=0 tags=yt-lockup-view-model"
    fake_zen({"click_result": {"ok": False, "reason": "no search results on this page", "diag": diag}, **YT_MISS})
    code, out, _ = run_jv("zen", "play", "asdfgh", "--json")
    assert code == 1 and out["diag"] == diag and "no search results on this page" in out["error"]


def test_result_waits_line_up():   # playfix: add-on 12 s < host 13 s < jv 14 s < brain/MCP 16 s
    import re
    from heardback.brain import catalogue
    from jv import zen_host
    js = (zen.REPO / "extension/background.js").read_text()
    addon = int(re.search(r"const RESULT_WAIT_MS = (\d+);", js)[1]) / 1000
    fetch = int(re.search(r"const YT_FETCH_MS = (\d+);", js)[1]) / 1000
    pick = catalogue.jv_timeout(["jv", "zen", "pick", "--json", "--", "2"], catalogue.JV_TIMEOUT_S)
    assert addon >= 12 and addon < zen_host.SLOW["click_result"] < zen.PAGE_TIMEOUT < pick
    # bgplay: play = yt_first (6 < 7 < 8 s), then the results-page fallback, inside the brain's wait
    assert fetch < zen_host.SLOW["yt_first"] < zen.YT_TIMEOUT
    check = zen.AUTOPLAY_WAIT + 3.5   # autoplay: audible poll + one `video` call (zen.call's default wait)
    assert zen.YT_TIMEOUT + zen.PAGE_TIMEOUT + check + 4 * 3.5 < catalogue.jv_timeout(["jv", "zen", "play", "--json", "--", "x"], 10)
    assert zen.PAGE_TIMEOUT + check < pick
    assert catalogue.jv_timeout(["jv", "zen", "list"], 10) == 10


@pytest.mark.parametrize("q", [["second", "result"], ["the", "3rd", "video", "on", "youtube"], ["result", "2"]])
def test_play_refuses_a_bare_result_number(fake_zen, run_jv, q):   # pickfix: "second result" isn't a title
    ops = fake_zen()
    code, _, err = run_jv("zen", "play", *q)
    assert code == 2 and "say which result, like pick result 2" in err and not ops


def test_pick_current_tab(fake_zen, monkeypatch, run_jv):
    _fake(monkeypatch, clients=ZEN_CLIENTS)
    ops = fake_zen({"click_result": {**VID, "title": "Second one"}})
    from jv import media
    media.duck_path().write_text("mpd\n")
    code, out, _ = run_jv("zen", "pick", "2", "--json")
    assert not media.duck_path().exists()
    assert code == 0 and _ops(ops, "click_result") == [{"n": 2}] and out["say"].startswith("opening result 2, Second one")
    assert out["focus_confirmed"] is False
    assert run_jv("zen", "pick", "0")[0] == 2 and run_jv("zen", "pick", "21")[0] == 2   # schema: 1-20


def test_read_returns_page_text(fake_zen, run_jv):
    ops = fake_zen({"page_text": {"ok": True, "tabId": 3, "title": "Inbox", "url": "https://mail.example.com/",
                                  "text": "hello " * 10, "truncated": False}})
    out = run_jv("zen", "read", "--json")[1]
    assert ops == [{"op": "page_text", "args": {}}]
    assert out["text"].startswith("hello hello") and out["title"] == "Inbox" and out["say"] == "read Inbox"


def _video_tabs():
    return _with((20, "Song - YouTube", "https://www.youtube.com/watch?v=a", 3),
                 (21, "Talk - YouTube", "https://www.youtube.com/watch?v=b", 1))


def test_video_pause_hits_the_playing_tab(fake_zen, monkeypatch, run_jv):
    monkeypatch.setattr(zen.media, "zen", lambda action: None)
    ops = fake_zen({**_video_tabs(), "video": {"ok": True, "paused": True}})
    out = run_jv("zen", "video", "pause", "--json")[1]
    assert _ops(ops, "video") == [{"tabId": ID_AUDIO, "action": "pause"}] and out["say"] == "paused"


def test_video_page_failure_never_tries_a_second_tab(fake_zen, monkeypatch, run_jv):
    monkeypatch.setattr(zen.media, "zen", lambda action: None)
    def video(a):   # the active tab (Inbox) has no video; the latest YouTube video does
        return {"ok": True, "paused": False} if a["tabId"] == 21 else {"ok": False, "reason": "no video on this page"}
    ops = fake_zen({**_video_tabs(), "video": video})
    code, out, _ = run_jv("zen", "video", "play", "--json")
    assert code == 1 and "no video on this page" in out["error"]
    assert [a["tabId"] for a in _ops(ops, "video")] == [ID_ACTIVE]


def test_video_none_anywhere(fake_zen, monkeypatch, run_jv):
    monkeypatch.setattr(zen.media, "zen", lambda action: None)
    fake_zen({"video": {"ok": False, "reason": "no video on this page"}})
    code, _, err = run_jv("zen", "video", "play")
    assert code == 1 and "no video on this page" in err


def test_video_mpris_uncertainty_never_dispatches_to_page(fake_zen, monkeypatch, run_jv):
    def uncertain(action):
        raise JvError(1, "playback verification failed", dispatched=True)
    monkeypatch.setattr(zen.media, "zen", uncertain)
    ops = fake_zen(_video_tabs())
    code, out, _ = run_jv("zen", "video", "toggle", "--json")
    assert code == 1 and out["dispatched"] is True and not ops


def test_video_page_play_observes_selected_tab_audibility(fake_zen, monkeypatch, run_jv):
    monkeypatch.setattr(zen.media, "zen", lambda action: None)
    listing = _video_tabs()
    next(t for t in listing["list"]["tabs"] if t["id"] == ID_ACTIVE)["audible"] = True
    ops = fake_zen({**listing, "video": {"ok": True, "paused": False}})
    code, out, _ = run_jv("zen", "video", "play", "--json")
    assert code == 0 and out["say"] == "playing"
    assert _ops(ops, "video") == [{"tabId": ID_ACTIVE, "action": "play"}]


@pytest.mark.parametrize("q", [["dashboard"], ["heardback", "dashboard"], ["the", "dashboard"]])
def test_open_dashboard_is_local(fake_zen, monkeypatch, tmp_path, run_jv, q):   # friction #12
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    _fake(monkeypatch, clients=ZEN_CLIENTS)
    ops = fake_zen()
    assert _confirmed_open(run_jv, *q)[0] == 0
    assert _ops(ops, "create") == [_creation("http://127.0.0.1:8787/")] and not _ops(ops, "search")


@pytest.mark.parametrize("q,url", [("whatsapp", "https://web.whatsapp.com"), ("WhatsApp.", "https://web.whatsapp.com"),
                                   ("gmail", "https://mail.google.com"), ("chatgpt", "https://chatgpt.com"),
                                   ("reddit", "https://www.reddit.com")])
def test_open_bare_site_name_opens_the_site(fake_zen, monkeypatch, run_jv, q, url):   # fix2 P8
    _fake(monkeypatch, clients=ZEN_CLIENTS)
    ops = fake_zen()
    code, out, _ = _confirmed_open(run_jv, q)
    assert code == 0 and _ops(ops, "create") == [_creation(url)] and not _ops(ops, "search")
    assert out["say"].startswith(f"opened {zen._page_label(zen._open_query(q))}")


def test_close_read_back_short(fake_zen, monkeypatch, run_jv):   # fix2 P10, friction2 #38
    yt = [(20 + i, f"Video number {i} with a long title ✨ [beats] - YouTube", "https://www.youtube.com/watch?v=" + str(i), 1)
          for i in range(7)]
    fake_zen(_with(*yt))
    code, out, _ = run_jv("zen", "close", "youtube", "--json")
    assert code == 3 and out["confirm"] == "Close 7 YouTube tabs?"
    code, out, _ = run_jv("zen", "close", "watch", "--json")   # matched by url only: titles, at most 2, short
    assert code == 3 and out["confirm"] == \
        "Close 7 tabs: Video number 0 with a long …, Video number 1 with a long … and 5 more?"


OLD = {"ok": False, "error": "unknown op"}


def test_old_addon_unknown_op_says_reload(runtime_dir):   # P3: real zen-host relaying an old add-on's reply
    from test_zen_host import _relay
    _relay(runtime_dir, lambda m: {"id": m["id"], **OLD})
    with pytest.raises(JvError) as e:
        zen.call("page_text")
    assert e.value.code == 1 and "'page_text'" in e.value.msg and e.value.extra["say"] == zen.ADDON_OLD_SAY


@pytest.mark.parametrize("verb", [["play", "lofi"], ["pick", "2"], ["video", "pause"], ["ask", "hi"]])
def test_old_addon_said_plainly_by_every_verb(fake_zen, monkeypatch, run_jv, verb):
    from jv import cli
    _fake(monkeypatch, clients=ZEN_CLIENTS)
    monkeypatch.setattr(zen.media, "zen", lambda action: None)   # no MPRIS: _video goes to the page op
    tabs = _tabs()
    tabs[0]["url"], tabs[1]["url"] = "https://claude.ai/new", "https://www.youtube.com/watch?v=x"
    ops = fake_zen({"list": {"ok": True, "current_window": 1, "tabs": tabs},
              **{op: OLD for op in ("yt_first", "click_result", "video", "type")}})
    code, out, _ = run_jv("zen", *verb, "--json")
    assert code == 1 and out["say"] == zen.ADDON_OLD_SAY
    if verb[0] == "ask":
        assert out["error"] == "Claude.ai evidence protocol unavailable"
        assert [o["op"] for o in ops] == ["list"]
    else:
        assert "unknown op" in out["error"]
        assert "unknown op '" in cli._error   # the op name reaches the jv run trace event


@pytest.mark.parametrize("q", ["all", "every", "other"])
def test_close_all_tabs_goes_to_claude(fake_zen, run_jv, q):   # fix3: "close all tabs" never closes the "all" match
    fake_zen()
    code, _, err = run_jv("zen", "close", q, "--json")
    assert code == 2 and "which tab" in err


# title worker: speak only the Latin-script part of a title (rule: cut at first " | "/" - "/" – " if >= 2 words
# before it, drop words without a Latin letter, a bare number stays only right after a kept word)
@pytest.mark.parametrize("title,want", [
    ("Pokémon Journeys एपिसोड 9 | एक लेजेंड की कहानी", "Pokémon Journeys"),
    ("Pokémon Journeys episode 9 | एक लेजेंड", "Pokémon Journeys episode 9"),
    ("एक लेजेंड की कहानी", ""),
    ("Lofi Hip Hop Radio beats to relax study to 24/7 live", "Lofi Hip Hop Radio beats to relax study …"),
    ("Lofi hip hop radio - beats to relax", "Lofi hip hop radio"),
    ("Solo - Live", "Solo Live"),
])
def test_short_speaks_only_latin_part(title, want):
    assert zen._short(title) == want


def test_play_pure_hindi_title_says_the_video(fake_zen, run_jv):
    fake_zen({"yt_first": {"ok": True, "video_id": "abc", "title": "एक लेजेंड की कहानी"},
              "create": {"ok": True, "tabId": 9}})
    code, out, err = run_jv("zen", "play", "कहानी", "--background", "--json")
    assert code == 0 and out["say"] == "playing the video"
