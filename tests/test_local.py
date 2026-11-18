"""Local clock/date and the removed screen-lock command through the catalogue and parser."""
import os
import time
from copy import deepcopy
from datetime import datetime, timezone

from heardback.brain import catalogue as brain_catalogue, router
from jv import local
from jv.cli import all_verbs, catalogue as jv_catalogue


def _live_catalogue(monkeypatch):
    verbs = deepcopy(all_verbs())
    next(v for v in verbs if v.name == "tmux.select").args["window"]["choices"] = lambda: []
    cat = brain_catalogue._index(jv_catalogue(verbs))
    return cat, router.Router(cat)


def _run_fast(run_jv, cat, route):
    entry = cat[route.verb]
    argv = brain_catalogue.build_argv(entry, route.args, "--json")
    return argv, run_jv(*argv[1:])


def test_time_and_date_fast_path_use_local_timezone_and_real_parser(monkeypatch, run_jv):
    class FixedClock:
        @classmethod
        def now(cls):
            # 18:35 UTC is already January 1 in Kolkata.
            return datetime(2025, 12, 31, 18, 35, tzinfo=timezone.utc)

    old_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "Asia/Kolkata"
        time.tzset()
        monkeypatch.setattr(local, "datetime", FixedClock)
        cat, fast = _live_catalogue(monkeypatch)

        route = fast.route("what time is it")
        assert (route.verb, route.args) == ("local.time", {})
        argv, (code, out, _) = _run_fast(run_jv, cat, route)
        assert argv == ["jv", "local", "time", "--json"]
        assert code == 0 and out["say"] == "It's 12:05 AM."

        route = fast.route("What's today's date?")
        assert (route.verb, route.args) == ("local.date", {})
        argv, (code, out, _) = _run_fast(run_jv, cat, route)
        assert argv == ["jv", "local", "date", "--json"]
        assert code == 0 and out["say"] == "Today is Thursday, January 1, 2026."
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()


def test_removed_lock_command_refuses_through_fast_path_and_cli(monkeypatch, run_jv):
    cat, fast = _live_catalogue(monkeypatch)
    for phrase in ("lock the screen", "lock screen"):
        route = fast.route(phrase)
        assert route.verb == "local.lock"
        argv, (code, out, _) = _run_fast(run_jv, cat, route)
        assert argv == ["jv", "local", "lock", "--json"]
        assert code == 1 and out["ok"] is False
        assert out["say"] == (
            "Screen locking is disabled in heardback. Please use your desktop lock shortcut."
        )
