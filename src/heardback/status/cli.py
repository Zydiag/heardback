"""`jv status ...` verbs: read the session store and answer by template."""
import os
import re
import textwrap
import time
from pathlib import Path

from heardback.status import speak, store, transcript, watch, who
from jv.verb import JvError, Verb

WHO = {"type": "string", "maxLength": 200}
# Lua Hyprland config: `hyprctl dispatch exec hypridle` fails and leaves hypridle down. Printed, never run.
HYPRIDLE_RESTART = """pkill -x hypridle; hyprctl dispatch 'hl.dsp.exec_cmd("hypridle")'"""


def _obj(props: dict, required: list[str] = []) -> dict:
    return {"type": "object", "properties": props, "required": required}


def _find(q: str, sessions: list[dict]) -> dict:
    try:
        return who.resolve(q, sessions)
    except who.Ambiguous as e:
        names = [who.name(s, sessions) for s in e.matches]
        raise JvError(1, f"{q} matches: {', '.join(names)}", candidates=names)
    except who.NotFound as e:
        target = str(e)
        if re.fullmatch(r"\d+", target):
            raise JvError(1, f"no Claude session in window {target}")
        raise JvError(1, f"no session matches {target}")


def _list(a: dict) -> dict:
    ss = store.user_sessions(store.list_sessions())
    return {"scope": "local user Claude Code sessions", "count": len(ss), "sessions": ss,
            "say": speak.overview(ss, time.time())}


def _get(a: dict) -> dict:
    ss = store.user_sessions(store.list_sessions(), include_ended=True)
    s = _find(a["who"], ss)
    return {"session": s, "say": speak.one(s, ss, time.time())}


def _watch(a: dict) -> dict:
    from heardback.status import watch
    ss = store.user_sessions(store.list_sessions(), include_ended=True)
    try:
        s = _find(a['who'], ss)
    except JvError as e:
        if e.code == 1 and not e.extra.get('candidates'):
            raise JvError(1, f"no Claude Code session in {a['who'] if not a['who'].isdigit() else 'window ' + a['who']}") from None
        raise
    if not s.get('tmux_pane'):
        raise JvError(1, 'no Claude Code session in that tmux window')
    if s.get('state') not in ('working', 'waiting'):
        return {'say': speak.one(s, ss, time.time())}
    if watch.arm(s['tmux_pane']):
        return {'say': f"I'll tell you when window {who.window(s)} is done."}
    return {'say': 'No watch was armed; I could not save the completion watch.'}


def _waiting(a: dict) -> dict:
    ss = store.user_sessions(store.list_sessions())
    return {"sessions": [s for s in ss if s["state"] == "waiting"], "say": speak.waiting(ss)}


def _summary(a: dict) -> dict:
    history, now = store.user_sessions(store.list_sessions(), include_ended=True), time.time()
    ss = history if a.get("who") else store.user_sessions(history)
    text = speak.one(_find(a["who"], ss), ss, now) if a.get("who") else speak.overview(ss, now)
    from heardback.brain import usage  # lazy, like install below
    if (n := usage.today_turns()) >= usage.DAY_TURN_WARN:
        text += " " + usage.day_line(n)
    return {"text": text, "say": text}


def _detail(a: dict) -> dict:
    ss = store.user_sessions(store.list_sessions(), include_ended=True)
    s = _find(a["who"], ss)
    turns = transcript.last_turns(s.get("transcript_path"), a.get("n", 3))
    n = who.name(s, ss)
    if not turns:
        return {"turns": turns, "say": f"No detail for {n} yet: no transcript to read."}
    # fast path speaks this as is: the last request, then the first sentence of the last reply
    ask = next((t["text"] for t in reversed(turns) if t["role"] == "user"), "")
    reply = next((t["text"] for t in reversed(turns) if t["role"] == "assistant"), "")
    say = f"{speak._cap(n)}: you asked {textwrap.shorten(ask, 80, placeholder='…')!r}." if ask else f"{speak._cap(n)}:"
    if reply:
        say += " It said: " + speak._end(textwrap.shorten(re.split(r"(?<=[.!?])\s", reply, maxsplit=1)[0], 160, placeholder="…"))
    return {"turns": turns, "say": say}


def _install(a: dict) -> dict:
    from heardback.status import install  # lazy: shutil costs ~2 ms on every jv call
    p, r = store.claude_dir() / "settings.json", {}
    if a.get("idle"):  # additive: the listener plus the settings.json hooks
        install.check_hooks(p)  # fail before writing either file
        c = _hypridle_conf()
        b = install.install_idle(c)
        r = {"hypridle": str(c), "hypridle_backup": b and str(b)}
    b = install.install_hooks(p)
    say = "Session hooks installed." + (f" Away listener installed; restart hypridle: {HYPRIDLE_RESTART}" if r else "")
    return {"settings": str(p), "backup": b and str(b), **r, "say": say}


def _uninstall(a: dict) -> dict:
    from heardback.status import install
    p, c = store.claude_dir() / "settings.json", _hypridle_conf()
    install.check_idle(c)  # fail before touching settings.json
    b = install.uninstall_hooks(p)
    ib = install.uninstall_idle(c)
    return {"settings": str(p), "backup": b and str(b), "hypridle": str(c), "hypridle_backup": ib and str(ib),
            "say": "Session hooks removed." + (f" Away listener removed; restart hypridle: {HYPRIDLE_RESTART}" if ib else "")}


def _hypridle_conf() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "hypr" / "hypridle.conf"


def _mute(a: dict) -> dict:
    from heardback.status import away  # lazy: subprocess on every jv call
    until = a.get("until")
    if until == "off":
        away.unmute()
        return {"muted_until": None, "say": "Unmuted."}
    m = int(until) if until else None
    return {"muted_until": away.mute(m, time.time()), "say": f"Muted for {m} minute{'s' * (m != 1)}." if m else "Muted."}


def _gc(a: dict) -> dict:
    r = store.gc()
    return {**r, "say": f"Removed {r['deleted']} old session{'s' * (r['deleted'] != 1)}."}


def _internal(a: dict) -> dict:
    """Typed diagnostics for heardback's own Claude process; excluded from user-session counts."""
    ss = [s for s in store.list_sessions() if store.is_internal(s)]
    return {"scope": "internal heardback sessions", "count": len(ss), "sessions": ss,
            "say": f"{len(ss)} internal heardback session{'s' * (len(ss) != 1)}."}


STATUS_Q = ("", "whats ", "what is ")   # fix3: "what's the status of third window"
STATUS_OF = [f"{q}status of {p}" for q in STATUS_Q
             for p in ("window {who:str}", "windows {who:str}", "{who:str} window",
                       "{who:str} windows")]
ASK_STATUS_PHRASES = [*STATUS_OF, "ask window {who:str} for its status",
                      "ask window {who:str} for a status", "ask window {who:str} how it's going",
                      "full status of window {who:str}"]
WATCH_PHRASES = ["tell me when window {who:str} is done",
                 "tell me when window {who:str} finishes", "tell me when window {who:str} has finished",
                 "let me know when window {who:str} is done", "let me know when window {who:str} finishes",
                 "let me know when window {who:str} has finished"]
VERBS = [
    Verb('status.ask', watch.ask, 'Ask one Claude Code session for its own status ("status of window 3"). heardback types a fixed question and reads the answer aloud itself when the session stops; you only get \'asking\'. A busy session is never typed into: the user is offered an ask when it\'s free. full true types plain status for the user to read.',
         _obj({'who': WHO, 'full': {'type': 'boolean'},
               'source_revision': {'type': 'string', 'pattern': r'^status:[0-9a-f]{64}$'},
               'expect_session': {'type': 'string', 'maxLength': 200,
                                  'description': 'Internal: expected session id for a deferred ask; refuse if it changed.'}},
              ['who']), ['who', '--full', '--expect-session', '--source-revision'],
         args={'who': {'type': 'str'}}, confirm='auto', check=watch.ask_check,
         phrases=ASK_STATUS_PHRASES,
         phrase_args={"full status of window {who:str}": {"full": True}}),
    Verb("status.watch", _watch, "Tell the user by voice when a Claude Code session finishes its current job (\"tell me when window 3 is done\").",
         _obj({'who': WHO}, ['who']), ['who'], args={'who': {'type': 'str'}}, confirm=False,
         phrases=WATCH_PHRASES),
    Verb("status.list", _list, "List live local user Claude Code sessions visible to heardback, not remote cloud sessions.", _obj({}), [],
         phrases=["status", "what are my sessions doing", *[f"{q}status of my sessions" for q in STATUS_Q]]),
    Verb("status.get", _get, "Say what one Claude Code session is doing from its hooks.", _obj({"who": WHO}, ["who"]), ["who"],
         args={"who": {"type": "str"}}, phrases=[*[f"{q}status of {{who:str}}" for q in STATUS_Q],
                                                       "is window {who:str} done", "what is {who:str} doing"]),
    Verb("status.waiting", _waiting, "Say which sessions are waiting on you.", _obj({}), [],
         phrases=["anything waiting on me"]),
    Verb("status.summary", _summary, "One-line status of all sessions, or of one.", _obj({"who": WHO}), ["who"],
         args={"who": {"type": "str"}}, phrases=["summarise my sessions"]),
    Verb("status.detail", _detail, "Read the last request and first sentence of the reply from a session transcript.",
         _obj({"who": WHO, "n": {"type": "integer", "minimum": 1, "maximum": 10}}, ["who"]), ["who", "--n"],
         args={"who": {"type": "str"}, "n": {"type": "int"}},
         phrases=["what did window {who:str} say", "what did window {who:str} do", "what did windows {who:str} do",
                  "what did {who:str} window do", "what did {who:str} windows do", "what did {who:str} do"]),
    Verb("status.install-hooks", _install, "Add heardback's hooks to Claude Code's settings.json; --idle also adds the hypridle away listener.",
         _obj({"idle": {"type": "boolean"}}), ["--idle"], voice=False),
    Verb("status.uninstall-hooks", _uninstall, "Remove heardback's hooks from Claude Code's settings.json and the hypridle away listener.",
         _obj({}), [], voice=False),
    Verb("status.mute", _mute, "Silence session announcements: for some minutes, until unmuted (no value), or 'off'.",
         _obj({"until": {"type": "string", "pattern": r"^([1-9]\d{0,4}|off)$"}}), ["until"],
         args={"until": {"type": "int"}}, phrases=["mute status", "mute announcements for {until:int} minutes"]),
    # A slotless phrase can't pass "off", so unmute gets its own verb for the fast path.
    Verb("status.unmute", lambda a: _mute({"until": "off"}), "Turn session announcements back on.", _obj({}), [],
         phrases=["unmute announcements"]),
    Verb("status.gc", _gc, "Delete week-old ended sessions and rotate the events log.", _obj({}), [], voice=False),
    Verb("status.internal", _internal, "Typed-only diagnostics for heardback's own internal Claude sessions.",
         _obj({}), [], voice=False),
]
