"""Fake-only Zen inventory/reuse and confirmation regressions; no desktop or browser."""
import copy

import pytest

from jv import ctx, media, zen
from jv.verb import JvError


def tab(i, url, *, window=1, active=False, title="Page", accessed=10):
    return {"id": i, "windowId": window, "url": url, "title": title, "active": active,
            "lastAccessed": accessed}


@pytest.fixture
def bridge(monkeypatch):
    reply = {"ok": True, "current_window": 1, "current_tab": 1, "creation_guard": True,
             "navigation_focus": 1,
             "tabs": [tab(1, "https://example.com/", active=True)]}
    ops = []

    def call(op, args=None, **kw):
        ops.append((op, args))
        if op == "list":
            return copy.deepcopy(reply)
        if op == "activate":
            t = next(t for t in reply["tabs"] if t["id"] == args["tabId"])
            return {"ok": True, "tabId": t["id"], "windowId": t["windowId"], "title": t["title"], "url": t["url"],
                    "target_validated": True, "activation_confirmed": True,
                    "browser_focus_requested": True, "browser_focus_observed": True}
        created = tab(99, args.get("url", "https://search.example/"), window=args.get("windowId", 1), title="")
        reply["tabs"].append(created)
        return {"ok": True, "tabId": 99, "windowId": created["windowId"], "url": created["url"], "title": ""}

    monkeypatch.setattr(zen, "call", call)
    monkeypatch.setattr(zen, "focus_zen_client", lambda title: None)
    monkeypatch.setattr(zen, "_desktop_focus", lambda: {"workspace": None, "focus_confirmed": False})
    monkeypatch.setattr(media, "claim", lambda: None)
    return reply, ops


@pytest.mark.parametrize("alias,url", [("Gmail.", "https://mail.google.com/"),
                                      ("WhatsApp.", "https://web.whatsapp.com/")])
def test_alias_reuse_precedes_creation(bridge, run_jv, alias, url):
    reply, ops = bridge
    reply["tabs"].append(tab(2, url))
    rc, out, _ = run_jv("zen", "open", alias, "--json")
    assert rc == 0 and out["tabId"] == 2 and out["windowId"] == 1
    assert not any(op in ("create", "search") for op, _ in ops)


@pytest.mark.parametrize("wanted,existing", [
    ("https://youtube.com/watch?v=BBB", "https://youtube.com/watch?v=AAA"),
    ("https://example.com/Case", "https://example.com/case"),
    ("https://example.com/page#second", "https://example.com/page#first"),
    ("example.com/?q=BBB", "https://example.com/?q=AAA"),
])
def test_resource_miss_is_confirmed_for_open_and_never_fuzzy_switched(bridge, run_jv, wanted, existing):
    reply, ops = bridge
    reply["tabs"][0]["url"] = existing
    assert run_jv("zen", "open", wanted, "--json")[0] == 3
    rc, out, _ = run_jv("zen", "switch", wanted, "--json")
    assert rc == 1 and "unseen" in out["say"]
    assert not any(op in ("create", "search", "activate") for op, _ in ops)


def test_domain_shorthand_keeps_descendant_match(bridge, run_jv):
    reply, _ = bridge
    reply["tabs"][0]["url"] = "https://github.com/zydiag/heardback"
    assert run_jv("zen", "open", "github.com/zydiag", "--json")[1]["tabId"] == 1


@pytest.mark.parametrize("verb", ["open", "switch"])
def test_different_pages_with_duplicate_titles_keep_candidates(bridge, run_jv, verb):
    reply, ops = bridge
    reply["tabs"] = [tab(1, "https://github.com/one", active=True, title="GitHub"),
                     tab(2, "https://github.com/two", window=2, title="GitHub")]
    rc, out, _ = run_jv("zen", verb, "github", "--json")
    assert rc == 2 and {c["tabId"] for c in out["candidates"]} == {1, 2}
    assert {c["windowId"] for c in out["candidates"]} == {1, 2}
    assert not any(op == "activate" for op, _ in ops)


def test_equivalent_duplicates_prefer_current_window_then_recency_and_id(bridge, run_jv):
    reply, _ = bridge
    reply["tabs"] += [tab(9, "https://mail.google.com/", window=2, active=True, accessed=100),
                      tab(4, "https://mail.google.com/", accessed=30),
                      tab(3, "https://mail.google.com/", accessed=30)]
    assert run_jv("zen", "open", "gmail", "--json")[1]["tabId"] == 3
    reply["tabs"].reverse()
    assert run_jv("zen", "switch", "gmail", "--json")[1]["tabId"] == 3


def test_current_tab_uses_explicit_window_identity_and_ctx_preserves_it(bridge):
    reply, _ = bridge
    reply["tabs"].append(tab(2, "https://elsewhere.com/", window=2, active=True, title="Elsewhere"))
    reply.update(current_window=2, current_tab=2)
    state = zen.inventory()
    assert [t["id"] for t in state["tabs"] if t["current"]] == [2]
    assert ctx._tab()["windowId"] == 2 and ctx._tab()["tabId"] == 2
    reply["current_tab"] = 1
    assert zen.inventory()["current_identity"] == "unknown"
    with pytest.raises(JvError):
        ctx._tab()


def test_unknown_coverage_requires_yes_and_bound_revision(bridge, run_jv):
    _, ops = bridge
    rc, pending, _ = run_jv("zen", "open", "Gmail.", "--json")
    assert rc == 3 and pending["confirm"] == "Open mail.google.com in a new tab?"
    assert run_jv("zen", "open", "Gmail.", "--yes", "--json")[0] == 3
    assert not any(op == "create" for op, _ in ops)
    rc, out, _ = run_jv("zen", "open", "Gmail.", "--source-revision", pending["source_revision"], "--yes", "--json")
    assert rc == 0 and out["say"].startswith("opened Gmail")
    assert ("create", {"url": "https://mail.google.com", "windowId": 1, "expectedCurrentTab": 1, "active": True}) in ops


@pytest.mark.parametrize("query", ["https://evil.example/private?token=secret", "evil.example"])
def test_new_tab_readback_names_host(bridge, run_jv, query):
    _, ops = bridge
    rc, pending, _ = run_jv("zen", "open", query, "--json")
    assert rc == 3 and pending["confirm"] == "Open evil.example in a new tab?"
    assert not any(op == "create" for op, _ in ops)


@pytest.mark.parametrize("change", ["query", "window", "tabs"])
def test_changed_request_or_target_rejects_old_approval(bridge, run_jv, change):
    reply, ops = bridge
    pending = run_jv("zen", "open", "gmail", "--json")[1]
    q = "reddit" if change == "query" else "gmail"
    if change == "window":
        reply.update(current_window=2, current_tab=2)
        reply["tabs"].append(tab(2, "https://other.com/", window=2, active=True))
    if change == "tabs":
        reply["tabs"].append(tab(3, "https://other.com/"))
    assert run_jv("zen", "open", q, "--source-revision", pending["source_revision"], "--yes", "--json")[0] == 1
    assert not any(op in ("create", "search", "activate") for op, _ in ops)


def test_execution_rechecks_after_cli_check(bridge, run_jv, monkeypatch):
    reply, ops = bridge
    pending = run_jv("zen", "open", "gmail", "--json")[1]
    original = zen.call
    lists = 0

    def changing(op, args=None, **kw):
        nonlocal lists
        if op == "list":
            lists += 1
            if lists == 2:
                reply["tabs"].append(tab(7, "https://changed.com/"))
        return original(op, args, **kw)

    monkeypatch.setattr(zen, "call", changing)
    assert run_jv("zen", "open", "gmail", "--source-revision", pending["source_revision"], "--yes", "--json")[0] == 1
    assert not any(op == "create" for op, _ in ops)


def test_old_addon_reuses_visible_tabs_but_cannot_create(bridge, run_jv):
    reply, ops = bridge
    reply.pop("current_tab")
    reply.pop("creation_guard")
    reply.pop("navigation_focus")
    assert run_jv("zen", "open", "example.com", "--json")[0] == 0
    rc, out, _ = run_jv("zen", "open", "gmail", "--yes", "--json")
    assert rc == 1 and out["say"] == zen.ADDON_OLD_SAY
    assert not any(op == "create" for op, _ in ops)
    assert not any(op == "activate" for op, _ in ops)


def test_current_identity_race_is_not_reported_as_old_addon(bridge, run_jv):
    reply, _ = bridge
    reply["tabs"].append(tab(2, "https://other.com/", active=True))
    rc, out, _ = run_jv("zen", "open", "gmail", "--json")
    assert rc == 1 and "uncertain" in out["say"] and out["say"] != zen.ADDON_OLD_SAY


@pytest.mark.parametrize("q", ["what is C++?", "open.spotify.com/search/AC/DC love%20song"])
def test_query_punctuation_and_existing_spotify_encoding_survive(bridge, run_jv, q):
    _, ops = bridge
    pending = run_jv("zen", "open", q, "--json")[1]
    assert run_jv("zen", "open", q, "--source-revision", pending["source_revision"], "--yes", "--json")[0] == 0
    sent = next(args for op, args in ops if op in ("search", "create"))
    if q.startswith("open.spotify.com"):
        assert sent["url"] == "https://open.spotify.com/search/AC%2FDC%20love%20song"
    else:
        assert sent["query"] == q


def test_missing_spotify_uses_existing_confirmation_and_stays_in_background(bridge, run_jv, monkeypatch):
    _, ops = bridge
    monkeypatch.setattr(media, "players", lambda: [])
    monkeypatch.setattr(zen, "focus_zen_client", lambda title: pytest.fail("Spotify must stay in background"))
    rc, pending, _ = run_jv("media", "play", "--target", "spotify", "--json")
    assert rc == 3 and pending["confirm"] == "Open open.spotify.com in a new tab?"
    rc, out, _ = run_jv("media", "play", "--target", "spotify", "--source-revision", pending["source_revision"], "--yes", "--json")
    assert rc == 0 and out["opened"] and out["windowId"] == 1
    assert any(op == "create" for op, _ in ops)


def test_multiple_compositor_windows_cannot_be_mapped_by_a_unique_lagging_title(monkeypatch):
    monkeypatch.setattr(zen.hypr, "clients", lambda: [
        {"class": "zen", "title": "Wanted — Zen Browser", "address": "0xwrong", "workspace": {"id": 2}},
        {"class": "zen", "title": "Old title — Zen Browser", "address": "0xright", "workspace": {"id": 5}}])
    monkeypatch.setattr(zen.hypr, "dispatch", lambda *_: pytest.fail("must not focus an unrelated window"))
    assert zen.focus_zen_client("Wanted") is None


def test_background_creation_never_activates_an_unexpected_visible_match(bridge):
    _, ops = bridge
    with pytest.raises(JvError, match="became visible"):
        zen._open({"query": "example.com"}, focus=False)
    assert not any(op == "activate" for op, _ in ops)


@pytest.mark.parametrize("title", ["", "Loading…", "WhatsApp"])
def test_guarded_open_retains_identity_and_focuses_without_a_loaded_title(bridge, run_jv, monkeypatch, title):
    _, ops = bridge
    original = zen.call

    def loading(op, args=None, **kw):
        result = original(op, args, **kw)
        if op in ("create", "activate"):
            result["title"] = title
        return result

    monkeypatch.setattr(zen, "call", loading)
    monkeypatch.setattr(zen, "_desktop_focus", lambda: {"workspace": 7, "focus_confirmed": True})
    pending = run_jv("zen", "open", "whatsapp", "--json")[1]
    rc, out, _ = run_jv("zen", "open", "whatsapp", "--source-revision", pending["source_revision"], "--yes", "--json")
    assert rc == 0 and out["focus_confirmed"] and out["workspace"] == 7
    assert out["title"] == title and out["url"] == "https://web.whatsapp.com"
    assert out["tabId"] == 99 and out["windowId"] == 1
    assert out["browser_focus_requested"] and out["browser_focus_observed"]
    assert len([op for op, _ in ops if op == "create"]) == 1
    assert [(op, args) for op, args in ops if op == "activate"] == [("activate", {"tabId": 99, "windowId": 1})]


@pytest.mark.parametrize("verb,query,creation", [("open", "gmail", "create"),
                                                ("search", "cats", "search"),
                                                ("search", "cats on youtube", "create")])
def test_explicit_background_creates_once_inactive_without_focus(bridge, run_jv, monkeypatch, verb, query, creation):
    _, ops = bridge
    monkeypatch.setattr(zen, "_desktop_focus", lambda: pytest.fail("background must keep desktop focus"))
    args = ["zen", verb, query, "--background", "--json"]
    if verb == "open":
        pending = run_jv(*args)[1]
        args += ["--source-revision", pending["source_revision"], "--yes"]
    rc, out, _ = run_jv(*args)
    sent = [payload for op, payload in ops if op == creation]
    assert rc == 0 and len(sent) == 1 and sent[0]["active"] is False
    assert out["background"] and not out["activation_confirmed"] and not out["focus_confirmed"]
    assert not any(op == "activate" for op, _ in ops)


@pytest.mark.parametrize("verb", ["open", "switch"])
def test_background_reuse_never_activates_or_claims(bridge, run_jv, monkeypatch, verb):
    _, ops = bridge
    monkeypatch.setattr(media, "claim_browser", lambda: pytest.fail("background reuse must not claim recovery"))
    rc, out, _ = run_jv("zen", verb, "example.com", "--background", "--json")
    assert rc == 0 and out["tabId"] == 1 and out["background"]
    assert [op for op, _ in ops] == (["list", "list"] if verb == "open" else ["list"])


@pytest.mark.parametrize("original_background", [False, True])
def test_confirmation_revision_binds_background_choice(bridge, run_jv, original_background):
    _, ops = bridge
    initial = ["--background"] if original_background else []
    changed = [] if original_background else ["--background"]
    pending = run_jv("zen", "open", "gmail", *initial, "--json")[1]
    rc, _, _ = run_jv("zen", "open", "gmail", *changed, "--source-revision", pending["source_revision"], "--yes", "--json")
    assert rc == 1 and not any(op in ("create", "search", "activate") for op, _ in ops)


@pytest.mark.parametrize("verb", ["open", "search"])
def test_old_addon_refuses_background_before_mutation(bridge, run_jv, verb):
    reply, ops = bridge
    reply.pop("navigation_focus")
    rc, out, _ = run_jv("zen", verb, "gmail", "--background", "--json")
    assert rc == 1 and out["say"] == zen.ADDON_OLD_SAY
    assert not any(op in ("create", "search", "activate") for op, _ in ops)


def test_old_addon_creates_once_and_reports_focus_limitation(bridge, run_jv):
    reply, ops = bridge
    reply.pop("navigation_focus")
    pending = run_jv("zen", "open", "gmail", "--json")[1]
    rc, out, _ = run_jv("zen", "open", "gmail", "--source-revision", pending["source_revision"], "--yes", "--json")
    assert rc == 0 and not out["focus_confirmed"] and out["focus_limitation"]
    assert "Please bring Zen" in out["say"]
    assert len([op for op, _ in ops if op == "create"]) == 1
    assert not any(op == "activate" for op, _ in ops)


@pytest.mark.parametrize("reply", [{"tabId": 1, "windowId": 8, "target_validated": True},
                                  {"tabId": 7, "windowId": 1, "target_validated": True},
                                  {"tabId": 1, "windowId": 1, "target_validated": False},
                                  {"tabId": 1, "windowId": 1, "target_validated": True, "activation_confirmed": False}])
def test_unvalidated_activation_does_not_focus_or_repeat_navigation(bridge, run_jv, monkeypatch, reply):
    _, ops = bridge
    original = zen.call

    def changed(op, args=None, **kw):
        return {"ok": True, **reply} if op == "activate" else original(op, args, **kw)

    monkeypatch.setattr(zen, "call", changed)
    monkeypatch.setattr(zen, "_desktop_focus", lambda: pytest.fail("identity must be validated first"))
    rc, out, _ = run_jv("zen", "open", "example.com", "--json")
    assert rc == 0 and not out["focus_confirmed"] and not out["activation_confirmed"]
    assert out["say"].startswith("found ")
    assert not any(op in ("create", "search") for op, _ in ops)


@pytest.mark.parametrize("query", ["cats", "cats on youtube"])
def test_foreground_search_focuses_exact_created_target(bridge, run_jv, monkeypatch, query):
    _, ops = bridge
    monkeypatch.setattr(zen, "_desktop_focus", lambda: {"workspace": 4, "focus_confirmed": True})
    rc, out, _ = run_jv("zen", "search", query, "--json")
    assert rc == 0 and out["focus_confirmed"] and out["tabId"] == 99 and out["windowId"] == 1
    assert [(op, args) for op, args in ops if op == "activate"] == [("activate", {"tabId": 99, "windowId": 1})]
    assert len([op for op, _ in ops if op in ("create", "search")]) == 1


@pytest.mark.parametrize("count", [0, 2])
def test_desktop_focus_never_guesses_from_titles(monkeypatch, count):
    clients = [{"class": "zen", "address": f"0x{n+1:x}", "workspace": {"id": n+3}, "title": "Same title"}
               for n in range(count)]
    monkeypatch.setattr(zen.hypr, "clients", lambda: clients)
    monkeypatch.setattr(zen.hypr, "dispatch", lambda *_: pytest.fail("no unique compositor target"))
    assert not zen._desktop_focus()["focus_confirmed"]


@pytest.mark.parametrize("observed", ["0xabc", "0x123"])
def test_desktop_focus_uses_actual_address_once_and_observes(monkeypatch, observed):
    calls, reads = [], iter(["0x123", observed, observed, observed, observed])
    monkeypatch.setattr(zen.hypr, "clients", lambda: [
        {"class": "zen", "address": "0xabc", "workspace": {"id": 9}, "title": ""}])
    monkeypatch.setattr(zen.hypr, "_active", lambda: next(reads))
    monkeypatch.setattr(zen.hypr, "dispatch", calls.append)
    monkeypatch.setattr(zen.hypr, "FOCUS_WINDOW", 'focus address:{addr}')
    monkeypatch.setattr(zen.time, "sleep", lambda *_: None)
    out = zen._desktop_focus()
    assert calls == ["focus address:0xabc"] and out["workspace"] == 9
    assert out["focus_confirmed"] is (observed == "0xabc") and out["desktop_focus_requested"]


def test_compositor_refusal_is_not_confirmation(monkeypatch):
    calls = []
    monkeypatch.setattr(zen.hypr, "clients", lambda: [
        {"class": "zen", "address": "0xabc", "workspace": {"id": 9}, "title": "Loading"}])
    monkeypatch.setattr(zen.hypr, "_active", lambda: "0x123")

    def refuse(command):
        calls.append(command)
        raise JvError(1, "focus refused")

    monkeypatch.setattr(zen.hypr, "dispatch", refuse)
    assert not zen._desktop_focus()["focus_confirmed"] and len(calls) == 1
