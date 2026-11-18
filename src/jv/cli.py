"""jv command line: registry, argparse, catalogue, confirm gate, exit codes."""
import argparse
import importlib
import json
import os
import sys
import time

from jv.verb import EFFECTS, JvError, Verb, observation_result, validate
from jv import desktop

MODULES: list[str] = ["jv.tmux", "jv.hypr", "jv.wall", "jv.zen", "jv.say", "jv.status", "jv.user", "jv.ctx", "jv.media", "jv.sysctl", "jv.settings", "jv.system_facts", "jv.calculator", "jv.local", "jv.reminder", "jv.inbox", "jv.facts", "jv.catchup", "jv.shot"]


def all_verbs() -> list[Verb]:
    # No try/except: a listed module that fails to import must fail loudly.
    return [v for m in MODULES for v in importlib.import_module(m).VERBS]


def _choices(spec: dict) -> dict:
    spec = dict(spec)
    c = spec.get("choices")
    if callable(c):
        try:
            spec["choices"] = list(c())
        except Exception:
            spec["choices"] = []
    elif c is not None:
        spec["choices"] = list(c)
    return spec


def catalogue(verbs: list[Verb]) -> dict:
    return {"version": 1, "verbs": [{
        "name": v.name, "domain": v.domain, "verb": v.verb, "description": v.description,
        "confirm": v.confirm, "effect": EFFECTS.get(v.name, "unknown"), "voice": v.voice, "personal": v.personal, "claude": v.claude, "observes": v.observes, "params": v.params, "argv": v.argv,
        "args": {k: _choices(s) for k, s in v.args.items()},
        "phrases": v.phrases, "phrase_args": v.phrase_args, "examples": v.examples,
    } for v in verbs]}


def _globals(p):
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument("--yes", action="store_true", help="confirm a gated action")
    p.add_argument("--dry-run", action="store_true", help="validate and check, never act")


class _PersonalParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's normal error repeats raw private arguments, and exit 2 hands off to Claude.
        raise JvError(1, "Please check the reminder or note command.", say="Please check the reminder or note command.")


def _parser(verbs: list[Verb], *, personal: bool = False) -> argparse.ArgumentParser:
    top = (_PersonalParser if personal else argparse.ArgumentParser)(prog="jv")
    doms = top.add_subparsers(dest="_domain", metavar="domain", required=True)
    _globals(doms.add_parser("list", help="list verbs"))
    subs, top_names = {}, {"list"}
    for v in verbs:
        if "." not in v.name:   # top-level command, e.g. `jv say`
            if v.name in top_names or v.name in subs:
                raise ValueError(f"top-level command {v.name!r} collides")
            top_names.add(v.name)
            p = doms.add_parser(v.name, help=v.description)
        else:
            if v.domain in top_names:
                raise ValueError(f"domain {v.domain!r} collides with a top-level command")
            if v.domain not in subs:
                subs[v.domain] = doms.add_parser(v.domain).add_subparsers(dest="_verb_name", metavar="verb", required=True)
            p = subs[v.domain].add_parser(v.verb, help=v.description)
        p.set_defaults(_verb=v)
        props, req = v.params.get("properties", {}), v.params.get("required", [])
        for a in v.argv:
            if a.startswith("--"):
                key = a[2:].replace("-", "_")
                if props.get(key, {}).get("type") == "boolean":
                    p.add_argument(a, dest=key, action="store_true", default=None)
                else:
                    p.add_argument(a, dest=key)
            else:
                key = a.replace("-", "_")
                if v.args.get(key, {}).get("type") == "text":
                    p.add_argument(key, metavar=a, nargs="+" if key in req else "*")
                else:
                    p.add_argument(key, metavar=a, nargs=None if key in req else "?")
        _globals(p)
    return top


def _emit(ns, data: dict, plain: str):
    checked = observation_result(data, getattr(ns, "_verb", None) is not None and ns._verb.observes)
    if checked is not data:
        data, plain = checked, checked["say"]
    print(json.dumps(data) if ns.json else plain)


def run(argv: list[str], verbs: list[Verb] | None = None) -> int:
    if argv[:1] == ["ask"]:   # typed client for heardbackd, not a catalogue verb
        from jv import ask
        return ask.main(argv[1:])
    if argv[:1] == ["memory"]:   # typed-only file commands; never appears in the brain catalogue
        from jv import memory
        return memory.main(argv[1:])
    verbs = all_verbs() if verbs is None else verbs
    private = argv[:1] in (["reminder"], ["inbox"], ["fact"])
    _trace.clear()
    try:
        ns = _parser(verbs, personal=private).parse_args(argv)
    except JvError as e:
        print(e.msg, file=sys.stderr)
        if "--json" in argv:
            print(json.dumps({"ok": False, "error": e.msg, **e.extra}))
        return e.code
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 2)
    if ns._domain == "list":
        cat = catalogue([v for v in verbs if not desktop.unavailable(v.name)])
        _emit(ns, cat, "\n".join(f"{v['name']}: {v['description']}" for v in cat["verbs"]))
        return 0
    verb = ns._verb
    try:
        raw = {k: " ".join(v) if isinstance(v, list) else v for k, v in vars(ns).items()
               if k in verb.params.get("properties", {}) and v not in (None, [])}
        args = validate(verb, raw)
        desktop.require(verb.name)
        granted = os.environ.get("HEARDBACK_REQUIRE_GRANT") == "1" and EFFECTS.get(verb.name, "unknown") != "read"
        if granted:
            from jv.grants import consume
            consume(os.environ.get("HEARDBACK_GRANT", ""), verb.name, args, os.environ.get("HEARDBACK_TURN", ""))
        try:
            sentence = verb.check(args) if verb.check else None
        except JvError:
            if verb.name == "media" and not ns.dry_run and args.get("action") != "status":
                from jv.media import claim
                claim()   # a terminal media preflight failure must not resume a ducked player
            raise
        source_revision = args.get("_source_revision")
        if sentence and (ns.dry_run or not (ns.yes or granted)):
            data = {"ok": False, "confirm": sentence}
            plain = sentence
            if source_revision:
                data["source_revision"] = source_revision
                if not ns.json:
                    plain += f" Pass --source-revision {source_revision} with --yes to confirm."
            _emit(ns, data, plain)
            return 3
        if sentence and (ns.yes or granted) and source_revision and not args.get("source_revision"):
            data = {"ok": False, "confirm": sentence, "source_revision": source_revision}
            plain = sentence if ns.json else sentence + f" Pass --source-revision {source_revision} with --yes to confirm."
            _emit(ns, data, plain)
            return 3
        if ns.dry_run:
            say = f"would run {verb.name}"
            data = {"ok": True, "dry_run": True, "say": say}
            if source_revision:
                data["source_revision"] = source_revision
            _emit(ns, data, say)
            return 0
        res = verb.fn(args) or {}
        _trace.update(res.pop("trace", None) or {})   # verb notes for the trace event, not the output
        _emit(ns, {**res, "ok": True}, res["say"] if "say" in res else json.dumps(res, indent=2))
        return 0
    except JvError as e:
        code, msg, extra = e.code, e.msg, e.extra
        if verb.personal:
            code = 1 if code == 2 else code
            extra.setdefault("say", msg)
    except Exception as e:   # a bug, not a refusal: say what blew up (`KeyError: 'active'`) and trace it
        if verb.personal:
            msg = "I couldn't complete that reminder or note command."
            print(msg, file=sys.stderr)
            if ns.json:
                print(json.dumps({"ok": False, "error": msg, "say": msg}))
            return 1
        code, msg, extra = 1, f"{type(e).__name__}: {e}" if str(e) else type(e).__name__, {}
        from jv import trace
        trace.error("jv", os.environ.get("HEARDBACK_TURN") or None, " ".join(argv), e)
    msg = " ".join(str(msg).split())
    global _error
    _error = msg
    print(msg, file=sys.stderr)
    if ns.json:
        print(json.dumps({**extra, "ok": False, "error": msg}))
    return code


_error: str | None = None   # the last run's failure message, for the trace
_trace: dict = {}   # extra fields a verb returned under "trace" (e.g. zen.play's autoplay reason)


def main() -> None:
    argv, t0 = sys.argv[1:], time.monotonic()
    rc = run(argv)
    if argv[:1] not in (["list"], ["task"], ["reminder"], ["inbox"], ["fact"]):   # private bodies stay out of argv telemetry
        from jv import trace
        trace.emit("jv", "run", os.environ.get("HEARDBACK_TURN") or None, argv=argv, rc=rc,
                   ms=round((time.monotonic() - t0) * 1000), error=_error,
                   dry_run="--dry-run" in argv, yes="--yes" in argv, **_trace)
    sys.exit(rc)
