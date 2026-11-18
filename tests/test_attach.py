"""Task 1 attachments: fake all desktop reads and tmux dispatch, including dry-runs."""
import hashlib
import json
import re

import pytest

from jv import ctx, hypr, tmux
from jv.verb import READBACK_MAX, validate


def pane(n, cmd="zsh"):
    return dict(id=f"%{n}", target=f"0:{n}.0", window_index=str(n), window_name="w", active=True,
                window_active=n == 3, cmd=cmd, cwd="/x", title="", in_mode=False, alternate=False,
                left=0, top=0, right=10, bottom=10)


@pytest.fixture
def io(monkeypatch):
    data = dict(tab=dict(tabId=7, title="Private title", url="https://example.com/private"),
                clipboard="secret clipboard\nsecond line", selection="secret selection",
                panes=[pane(3), pane(12), pane(4)], capture="private output\nlast line",
                prompt="─" * 30 + "\n❯ \n" + "─" * 30 + "\n", sent=[], captures=[])
    monkeypatch.delenv(tmux.TAINT_ENV, raising=False)
    monkeypatch.setattr(tmux, "panes", lambda: data["panes"])
    sessions = [dict(tmux_pane="%3", state="idle", session_id="fake")]
    monkeypatch.setattr(tmux, "load_sessions", lambda include_ended=False: sessions)
    monkeypatch.setattr(tmux, "_session_for_pane", lambda *a, **kw: sessions[0])
    monkeypatch.setattr(tmux, "trusting", lambda p: False)
    monkeypatch.setattr(tmux, "starting", lambda *a: False)
    monkeypatch.setattr(tmux, "showing_prompt", lambda *a: False)
    monkeypatch.setattr(tmux, "_stale_ended_claude", lambda *a: False)
    monkeypatch.setattr(tmux.time, "sleep", lambda *a: None)
    def dispatch(*args, **kw):
        if args[0] == "capture-pane":
            return data["prompt"]
        data["sent"].append((args, kw))
        return ""
    monkeypatch.setattr(tmux, "tmux", dispatch)
    def screen(pid, lines=15):
        data["captures"].append((pid, lines))
        return data["capture"]
    monkeypatch.setattr(tmux, "screen", screen)
    monkeypatch.setattr(ctx, "_tab", lambda: data["tab"].copy())
    monkeypatch.setattr(ctx, "paste", lambda primary=False: data["selection" if primary else "clipboard"])
    monkeypatch.setattr(hypr, "hyprctl", lambda *a, **kw: {"address": "0x1", "class": "gedit"})
    monkeypatch.setattr(tmux, "_tmux_foreground", lambda: False)
    return data


def send(run_jv_with, kind="tab", text="Explain it", target="3", flags=()):
    return run_jv_with(tmux.VERBS, "tmux", "type", "--attach", kind, "--json", *flags,
                       "--", target, *([] if text is None else [text]))


def test_attach_tab_line(io, run_jv_with):
    assert send(run_jv_with)[0] == 0
    assert io["sent"] == [(("send-keys", "-t", "%3", "-l", "--",
                            'Explain it (from heardback: Zen tab "Private title" https://example.com/private)'), {}),
                           (("send-keys", "-t", "%3", "Enter"), {})]


@pytest.mark.parametrize("title,want", [('  A\x00\x1b\n\t"quoted"  title\x7f', "A 'quoted' title"),
                                       ("x" * 300, "x" * 100)])
def test_attach_tab_title_sanitised(io, run_jv_with, title, want):
    io["tab"]["title"] = title
    assert send(run_jv_with)[0] == 0
    assert f'Zen tab "{want}" ' in io["sent"][0][0][-1]


@pytest.mark.parametrize("url", ["about:blank", "moz-extension://abc", "file:///tmp/x"])
def test_attach_tab_not_web(io, run_jv_with, url):
    io["tab"]["url"] = url
    code, out, _ = send(run_jv_with)
    assert code == 4 and out["error"] == "the current tab has no web link"
    assert not io["sent"]


@pytest.mark.parametrize("field,value", [("tabId", 8), ("url", "https://example.com/changed")])
def test_attach_tab_changed(io, run_jv_with, field, value):
    _, out, _ = send(run_jv_with, flags=("--dry-run",))
    assert out["source_revision"] == "tab:" + hashlib.sha256(
        b"tab\x007\x00https://example.com/private").hexdigest()
    io["tab"][field] = value
    code, out, _ = send(run_jv_with, flags=("--yes", "--source-revision", out["source_revision"]))
    assert code == 4 and out["error"] == "the tab changed after read-back; ask again"
    assert not io["sent"]


def test_attach_tab_title_change_ok(io, run_jv_with):
    _, out, _ = send(run_jv_with, flags=("--dry-run",))
    io["tab"]["title"] = "New title"
    assert send(run_jv_with, flags=("--source-revision", out["source_revision"]))[0] == 0
    assert '"New title"' in io["sent"][0][0][-1]


def test_attach_clipboard_paste(io, run_jv_with):
    assert send(run_jv_with, "clipboard")[0] == 0
    content = io["clipboard"]
    assert io["sent"] == [(("load-buffer", "-b", "jv", "-"),
                            {"input": f"Explain it\n\n(from heardback: your clipboard, {len(content)} characters)\n{content}"}),
                           (("paste-buffer", "-b", "jv", "-d", "-p", "-t", "%3"), {}),
                           (("send-keys", "-t", "%3", "Enter"), {})]


def test_attach_clipboard_changed(io, run_jv_with):
    _, out, _ = send(run_jv_with, "clipboard", flags=("--dry-run",))
    io["clipboard"] = "changed"
    code, out, _ = send(run_jv_with, "clipboard", flags=("--source-revision", out["source_revision"]))
    assert code == 4 and "clipboard changed after read-back" in out["error"]
    assert not io["sent"]


def test_attach_selection_vim_guard(io, monkeypatch, run_jv_with):
    monkeypatch.setattr(hypr, "hyprctl", lambda *a, **kw: {"address": "0x1", "class": "nvim"})
    code, out, _ = send(run_jv_with, "selection")
    assert code == 1 and "Vim or Neovim selection" in out["error"]
    assert not io["sent"]


def test_attach_selection_paste(io, run_jv_with):
    assert send(run_jv_with, "selection")[0] == 0
    assert io["sent"][0] == (("load-buffer", "-b", "jv", "-"),
                             {"input": "Explain it\n\n(from heardback: your selection, 16 characters)\nsecret selection"})


def test_attach_pane_capture(io, run_jv_with):
    assert send(run_jv_with, "pane", flags=("--attach-target", "12"))[0] == 0
    assert io["captures"] == [("%12", 200), ("%12", 200)]
    assert io["sent"][0][1]["input"] == \
        "Explain it\n\n(from heardback: the last 200 lines of window 12)\nprivate output\nlast line"


def test_attach_pane_self(io, run_jv_with):
    code, _, _ = send(run_jv_with, "pane", flags=("--attach-target", "%3"))
    assert code == 4 and not io["sent"]


def test_attach_pane_empty(io, run_jv_with):
    io["capture"] = " \n\t"
    code, _, _ = send(run_jv_with, "pane", flags=("--attach-target", "12"))
    assert code == 4 and not io["sent"]


@pytest.mark.parametrize("identity_changes", [True, False])
def test_attach_pane_identity(io, run_jv_with, identity_changes):
    flags = ("--attach-target", "12")
    _, out, _ = send(run_jv_with, "pane", flags=(*flags, "--dry-run"))
    assert out["source_revision"] == ctx.revision("pane", "%12")
    io["capture"] = "new output"
    if identity_changes:
        io["panes"][1]["id"] = "%99"
    code, out, _ = send(run_jv_with, "pane", flags=(*flags, "--source-revision", out["source_revision"]))
    assert code == (4 if identity_changes else 0)
    if identity_changes:
        assert not io["sent"]
    else:
        assert io["sent"][0][1]["input"].endswith("\nnew output")


@pytest.mark.parametrize("text", [None, ""])
def test_attach_default_text(io, run_jv_with, text):
    assert send(run_jv_with, text=text)[0] == 0
    assert io["sent"][0][0][-1].startswith("Look at this. (from heardback:")


def test_attach_not_claude(io, run_jv_with):
    code, out, _ = send(run_jv_with, target="4")
    assert code == 4 and out["error"] == "window 4 isn't a Claude Code session"
    assert not io["sent"]


@pytest.mark.parametrize("flag", ["--clipboard", "--selection"])
def test_attach_with_clipboard_flag(io, run_jv_with, flag):
    assert send(run_jv_with, flags=(flag,))[0] == 2
    assert not io["sent"]


@pytest.mark.parametrize("kind", [None, "tab", "clipboard", "selection"])
def test_attach_target_without_pane(io, run_jv_with, kind):
    flags = ["--attach", kind] if kind else []
    code, _, _ = run_jv_with(tmux.VERBS, "tmux", "type", *flags, "--attach-target", "12", "--json",
                             "--", "3", "Explain it")
    assert code == 2 and not io["sent"]


def test_attach_pane_needs_target(io, run_jv_with):
    assert send(run_jv_with, "pane")[0] == 2
    assert not io["sent"]


@pytest.mark.parametrize("text", ["!ls", "  !ls"])
def test_attach_leading_bang(io, run_jv_with, text):
    assert send(run_jv_with, text=text)[0] == 4
    assert not io["sent"]


@pytest.mark.parametrize("text", ["/clear", "  /clear"])
def test_attach_leading_slash(io, run_jv_with, text):
    assert send(run_jv_with, text=text)[0] == 4
    assert not io["sent"]


@pytest.mark.parametrize("kind,label", [("tab", "the current tab's link"),
    ("clipboard", "your clipboard, 28 characters"), ("selection", "your selection, 16 characters"),
    ("pane", "the last 200 lines of window 12")])
def test_readback_tainted_attach(io, monkeypatch, run_jv_with, kind, label):
    monkeypatch.setenv(tmux.TAINT_ENV, "1")
    flags = ("--attach-target", "12") if kind == "pane" else ()
    code, out, _ = send(run_jv_with, kind, flags=flags)
    assert code == 3 and out["confirm"] == f"Send 'Explain it' with {label} to the Claude session in window 3?"
    assert not io["sent"]
    assert send(run_jv_with, kind, flags=(*flags, "--yes"))[0] == 3
    assert not io["sent"]
    assert send(run_jv_with, kind, flags=(*flags, "--yes", "--source-revision", out["source_revision"]))[0] == 0


def test_readback_clean_attach(io, run_jv_with):
    code, out, _ = send(run_jv_with, flags=("--dry-run",))
    assert code == 0 and "confirm" not in out and not io["sent"]


@pytest.mark.parametrize("draft", ["delete the files", "!", None])
def test_attach_keeps_existing_or_unreadable_prompt_gated(io, run_jv_with, draft):
    rule = "─" * 30
    io["prompt"] = "unknown input box" if draft is None else f"{rule}\n❯ {draft}\n{rule}\n"
    code, out, _ = send(run_jv_with)
    assert code == 3 and "prompt" in out["confirm"] and "shell mode" in out["confirm"]
    assert not io["sent"]


def test_readback_cap_attach(io, monkeypatch, run_jv_with):
    monkeypatch.setenv(tmux.TAINT_ENV, "1")
    io["panes"][0]["window_index"] = "33"
    for size, expected in [(100, 3), (150, 4)]:
        code, out, _ = send(run_jv_with, "pane", "x" * size, target="33", flags=("--attach-target", "12"))
        assert code == expected
        if size == 100:
            assert len(out["confirm"]) <= READBACK_MAX
        else:
            assert "characters, the most is 200" in out["error"]
    assert not io["sent"]


@pytest.mark.parametrize("tainted", [False, True])
def test_bang_readback(io, monkeypatch, run_jv_with, tainted):
    if tainted:
        monkeypatch.setenv(tmux.TAINT_ENV, "1")
    code, out, _ = run_jv_with(tmux.VERBS, "tmux", "type", "--enter", "--json", "--", "3", "!ls")
    assert code == 3 and out["confirm"] == "Run shell command '!ls' in the Claude session in window 3?"
    assert not io["sent"]


def test_bang_leading_whitespace(io, run_jv_with):
    code, out, _ = run_jv_with(tmux.VERBS, "tmux", "type", "--enter", "--json", "--", "3", "  !ls")
    assert code == 3 and out["confirm"] == "Run shell command '!ls' in the Claude session in window 3?"
    assert not io["sent"]


def test_bang_without_enter(io, run_jv_with):
    assert run_jv_with(tmux.VERBS, "tmux", "type", "--json", "--", "3", "!ls")[0] == 0
    assert io["sent"] == [(("send-keys", "-t", "%3", "-l", "--", "!ls"), {})]


def test_bang_non_claude(io, run_jv_with):
    code, out, _ = run_jv_with(tmux.VERBS, "tmux", "type", "--enter", "--json", "--", "4", "!ls")
    assert code == 3 and out["confirm"] == "Run '!ls' in window 4?"
    assert not io["sent"]


@pytest.mark.parametrize("kind", ["tab", "clipboard", "selection", "pane"])
def test_attach_no_content_in_result(io, run_jv_with, kind):
    flags = ("--attach-target", "12") if kind == "pane" else ()
    for dry in [("--dry-run",), ()]:
        code, out, _ = send(run_jv_with, kind, flags=(*flags, *dry))
        assert code == 0
        result = json.dumps(out)
        for secret in [*io["tab"].values(), io["clipboard"], io["selection"], io["capture"]]:
            if isinstance(secret, str):
                assert json.dumps(secret)[1:-1] not in result


def test_attach_dry_run_types_nothing(io, run_jv_with):
    code, out, _ = send(run_jv_with, flags=("--dry-run",))
    assert code == 0 and not io["sent"]
    assert set(out) == {"ok", "dry_run", "say", "source_revision"}
    assert all(s not in json.dumps(out) for s in [io["tab"]["title"], io["tab"]["url"]])


def test_attach_url_quotes_spaces(io, run_jv_with):
    io["tab"]["url"] = 'https://example.com/a b?q="quoted"'
    assert send(run_jv_with)[0] == 0
    assert io["sent"][0][0][-1].endswith(io["tab"]["url"] + ")")


def test_attach_schema(io, run_jv_with):
    code, cat, _ = run_jv_with(tmux.VERBS, "list", "--json")
    assert code == 0
    v = next(v for v in cat["verbs"] if v["name"] == "tmux.type")
    props = v["params"]["properties"]
    assert props["attach"]["enum"] == ["tab", "clipboard", "selection", "pane", "last_heard"]
    assert props["attach_target"]["type"] == "string"
    assert "--attach" in v["argv"] and "--attach-target" in v["argv"]
    assert props["source_revision"]["maxLength"] == 74
    for kind in ["clipboard", "selection", "tab", "pane", "heard"]:
        assert re.fullmatch(props["source_revision"]["pattern"], kind + ":" + "a" * 64)
        validate(next(v for v in tmux.VERBS if v.name == "tmux.type"),
                 dict(target="3", source_revision=kind + ":" + "a" * 64))


@pytest.mark.parametrize("kind", ["tab", "clipboard", "selection", "pane"])
def test_attach_binds_preflight_to_dispatch(io, monkeypatch, run_jv_with, kind):
    if kind == "tab":
        reads = iter([io["tab"].copy(), {**io["tab"], "tabId": 99}])
        monkeypatch.setattr(ctx, "_tab", lambda: next(reads))
    elif kind == "pane":
        def screen(pid, lines=15):
            io["panes"][1] = {**io["panes"][1], "id": "%99"}
            return io["capture"]
        monkeypatch.setattr(tmux, "screen", screen)
    else:
        reads = iter(["first", "changed"])
        monkeypatch.setattr(ctx, "paste", lambda primary=False: next(reads))
    flags = ("--attach-target", "12") if kind == "pane" else ()
    code, out, _ = send(run_jv_with, kind, flags=flags)
    assert code == 4 and "changed after read-back" in out["error"]
    assert not io["sent"]


@pytest.mark.parametrize("guard", ["trusting", "starting", "showing_prompt", "_stale_ended_claude"])
def test_attach_preserves_destination_guards(io, monkeypatch, run_jv_with, guard):
    monkeypatch.setattr(tmux, guard, lambda *a: True)
    assert send(run_jv_with)[0] == 4
    assert not io["sent"]


@pytest.mark.parametrize("kind", ["clipboard", "selection"])
def test_attach_empty_source(io, run_jv_with, kind):
    io[kind] = " \n"
    assert send(run_jv_with, kind)[0] == 1
    assert not io["sent"]


def test_attach_instruction_enter_tail_is_literal(io, run_jv_with):
    text = "Explain why it says press enter"
    assert send(run_jv_with, text=text)[0] == 0
    assert io["sent"][0][0][-1].startswith(text + " (from heardback:")


def test_bang_clipboard_keeps_content_private(io, run_jv_with):
    io["clipboard"] = "!secret"
    code, out, _ = run_jv_with(tmux.VERBS, "tmux", "type", "--clipboard", "--enter", "--json", "--", "3")
    assert code == 3 and out["confirm"] == "Send the clipboard text, 7 characters, to the Claude session in window 3?"
    assert "!secret" not in json.dumps(out) and not io["sent"]


def test_bang_readback_cap(io, run_jv_with):
    code, out, _ = run_jv_with(tmux.VERBS, "tmux", "type", "--enter", "--json", "--", "3", "!" + "x" * 200)
    assert code == 4 and "characters, the most is 200" in out["error"]
    assert not io["sent"]


@pytest.mark.parametrize("draft,asks", [("does claude.", True), ("", False), (None, False)])
def test_plain_type_into_draft_asks_first(io, run_jv_with, draft, asks):
    # typing without Enter appends to an unsent draft; an unreadable box stays unasked (nothing runs)
    rule = "─" * 30
    io["prompt"] = "unknown input box" if draft is None else f"{rule}\n❯ {draft}\n{rule}\n"
    code, out, _ = run_jv_with(tmux.VERBS, "tmux", "type", "--json", "--", "3", "hello")
    assert (code == 3) is asks
    if asks:
        assert out["confirm"] == "Window 3's prompt already has text. Add 'hello' after it?" and not io["sent"]
