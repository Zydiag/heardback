"""`jv ctx`: what "this" is: the clipboard, the selection, the focused window or the current Zen tab.
User 2026-10-31: sent to Claude only when the user asks about "this" / the clipboard / the selection."""
import hashlib
import subprocess

from jv.verb import JvError, Verb

CAP = 8000   # chars handed to Claude
SOURCES = ["clipboard", "selection", "window", "tab"]
EMPTY = {"clipboard": "the clipboard is empty", "selection": "nothing is selected",
         "window": "no window is focused", "tab": "no Zen tab is open"}
TERMINALS = {"alacritty", "foot", "footclient", "gnome-terminal", "gnome-terminal-server", "kgx", "kitty",
             "konsole", "mate-terminal", "st", "terminator", "tilix", "urxvt", "wezterm", "xfce4-terminal",
             "xterm"}
EDITORS = {"nvim", "vim", "vi", "view", "nvim-qt", "gvim"}


def _empty(src: str) -> JvError:
    return JvError(1, EMPTY[src], say=EMPTY[src])


def paste(primary: bool = False) -> str:
    """Clipboard (or primary selection) text; "" when empty or not text. Never raises for empty."""
    argv = ["wl-paste", "--no-newline", "--type", "text", *(["--primary"] if primary else [])]
    try:
        r = subprocess.run(argv, capture_output=True, timeout=3)
    except FileNotFoundError:
        raise JvError(1, "wl-paste not installed")
    except subprocess.TimeoutExpired:
        raise JvError(1, "wl-paste timed out")
    return "" if r.returncode else r.stdout.decode(errors="replace")   # rc 1 = "Nothing is copied" / no text type


def text_of(src: str) -> str:
    """The clipboard or selection text, or JvError with a `say` when there is none."""
    return read(src)[0]


def revision(src: str, text: str) -> str:
    """Opaque, source-bound metadata; the copied text itself is never stored in the token."""
    digest = hashlib.sha256((src + "\0" + text).encode()).hexdigest()
    return f"{src}:{digest}"


def read(src: str) -> tuple[str, str]:
    if src == "selection":
        _selection_guard()
    t = paste(primary=src == "selection")
    if not t.strip():
        raise _empty(src)
    return t, revision(src, t)


def _selection_guard() -> None:
    """A terminal's editor/register selection is not the Wayland primary selection."""
    from jv import hypr, tmux
    try:
        focused = hypr.hyprctl("activewindow", json_out=True) or {}
    except Exception:
        message = "I can't confirm the focused app, so I can't read its selection; copy it to the clipboard first"
        raise JvError(1, message, say=message) from None
    if not focused.get("address"):
        message = "I can't confirm the focused app, so I can't read its selection; copy it to the clipboard first"
        raise JvError(1, message, say=message)
    app_class = str(focused.get("class") or "").casefold()
    if not app_class:
        message = "I can't identify the focused app, so I can't read its selection; copy it to the clipboard first"
        raise JvError(1, message, say=message)
    if app_class in EDITORS:
        message = 'Vim or Neovim selection is not the system selection; copy with "+y" or "+Y", then ask for the clipboard'
        raise JvError(1, message, say=message)
    in_tmux = tmux._tmux_foreground()
    if not in_tmux and app_class not in TERMINALS:
        return
    pane = tmux.resolve("current") if in_tmux else None
    if pane and pane.get("cmd", "").casefold() in EDITORS:
        message = 'Vim or Neovim selection is not the system selection; copy with "+y" or "+Y", then ask for the clipboard'
    else:
        message = "I can't identify a terminal selection reliably; copy it to the system clipboard first"
    raise JvError(1, message, say=message)


def _window() -> dict:
    from jv import hypr
    w = hypr.hyprctl("activewindow", json_out=True) or {}
    if not w.get("address"):
        raise _empty("window")
    return {"text": f"{w.get('class', '')}: {w.get('title', '')}", "class": w.get("class"), "title": w.get("title")}


def _tab() -> dict:
    from jv import zen
    state = zen.inventory()
    t = next((t for t in state["tabs"] if t["current"]), None)
    if not t:
        raise JvError(1, "current Zen tab identity is unknown", say="Zen's current tab is uncertain; please ask again.")
    return {"text": f"{t['title']} ({t['url']})", "title": t["title"], "url": t["url"],
            "tabId": t["id"], "windowId": t["windowId"], **{k: v for k, v in state.items() if k != "tabs"}}


def _ctx(a: dict) -> dict:
    picked = [s for s in SOURCES if a.get(s)]
    if len(picked) > 1:
        raise JvError(2, "pick one of --clipboard, --selection, --window, --tab")
    src = picked[0] if picked else "selection"
    if src == "window":
        res = _window()
    elif src == "tab":
        res = _tab()
    else:
        text, source_revision = read(src)
        res = {"text": text, "source_revision": source_revision}
    text = res["text"]
    if len(text) > CAP:
        res.update(text=text[:CAP], truncated=True)
    return {**res, "source": src, "say": f"got the {src}" + (f", first {CAP} characters" if len(text) > CAP else "")}


B = {"type": "boolean"}
VERBS = [
    Verb("ctx", _ctx, "Read what the user means by \"this\": --clipboard (what they copied), --selection (Wayland "
         "primary selection; the default), --window (focused app + title) or --tab (current Zen tab title + URL). Text is capped at "
         f"{CAP} characters. Only when the user asks about this / the clipboard / the selection."
         " \"Copied\" or \"clipboard\" means the clipboard; \"selected\", \"highlighted\" or a bare \"this\" (not \"this window\") means the selection; if nothing is selected, \"this\" is the current Zen tab, else the focused window. Any explicit clipboard wording means the clipboard, including \"selected text from the clipboard\". If the request clearly names both clipboard and selection as separate sources, or the source is unclear, ask one short question about which source before reading or typing. If ctx_ctx refuses a terminal selection, explain that Neovim can copy to the system clipboard with \"+y\" or \"+Y\", then ask for the clipboard. After reading clipboard or selection text, answer and offer \"Want me to paste it? Which window?\" with ask_user. Reading alone never authorizes insertion or Enter. If nothing is copied or selected, say so and do nothing.",
         {"type": "object", "properties": {s: B for s in SOURCES}, "required": []}, [f"--{s}" for s in SOURCES]),
]
