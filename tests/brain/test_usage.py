import json

from heardback.brain import usage


def test_bump_counts_and_rolls_over(env):
    assert usage.bump("haiku", "2026-10-29") == 1
    assert usage.bump("sonnet", "2026-10-29") == 2
    assert json.loads((env / "state/heardback/usage.json").read_text())["by_model"] == {"haiku": 1, "sonnet": 1}
    assert usage.bump("haiku", "2026-10-30") == 1


def test_bump_survives_bad_file(env):
    (env / "state/heardback").mkdir(parents=True, exist_ok=True)
    (env / "state/heardback/usage.json").write_text("[1,")
    assert usage.bump("haiku", "2026-10-29") == 1


# --- Task 22: rate-limit guard ----------------------------------------------------------------------------------
import asyncio
import time
from datetime import date, datetime

from brainutil import FAKE, run_brain, says, serving, talk, user_messages, utt
from heardback.brain import brain as brain_mod
from heardback.brain.usage import RateGuard

WARN = "Heads up, you've used 80 percent of your five-hour Claude limit."


def epoch_today(h: int, m: int) -> float:
    return datetime.now().replace(hour=h, minute=m, second=0, microsecond=0).timestamp()


def info(util: float, at: float, status: str = "allowed") -> dict:
    return {"status": status, "resetsAt": at, "unifiedWindows": {"five_hour": {"utilization": util, "resetsAt": at}}}


def test_warn_once():
    g, t = RateGuard(), epoch_today(18, 0)
    assert g.update(info(0.81, t)) == WARN
    assert g.update(info(0.85, t)) is None
    assert g.update(info(0.81, t + 18000)) == WARN   # a new window warns again
    assert g.update(info(0.5, t)) is None


def test_stop_at_95():
    g = RateGuard()
    assert g.update(info(0.96, epoch_today(18, 0))) == "Claude limit reached until 6 pm."
    assert not g.claude_allowed(epoch_today(17, 0))
    assert g.claude_allowed(epoch_today(18, 1))


def test_status_not_allowed_and_iso_and_minutes():
    g = RateGuard()
    at = datetime.now().replace(hour=6, minute=30, second=0, microsecond=0)
    assert g.update(info(0.5, at.isoformat(), "rejected")) == "Claude limit reached until 6:30 am."
    assert g.off_until == at.timestamp()


def test_brain_blocks_claude_not_fast(env):
    at = int(time.time() + 3600)
    limit = f"Claude limit reached until {usage.spoken(at)}."

    async def go():
        path = brain_mod.sock_path()
        async with serving(brain_mod.Brain(claude_cmd=FAKE), path):
            first = await talk(path, [utt(f"cue:limit 0.97 allowed {at}")])
            return first, await talk(path, [utt("hi")]), await talk(path, [utt("next wallpaper")])
    first, hi, wall = asyncio.run(go())
    assert says(first)[0] == limit
    assert says(hi) == [limit] and hi[-1] == {"type": "done"}
    assert user_messages(env) == [f"cue:limit 0.97 allowed {at}"]   # "hi" never reached Claude
    assert says(wall) == ["Done."]


def test_brain_warns_once(env):
    at = int(time.time() + 3600)
    out = run_brain(env, [utt(f"cue:limit 0.85 allowed {at}"), utt(f"cue:limit 0.86 allowed {at}")],
                    claude_cmd=FAKE, gap=0.3)
    assert says(out).count(WARN) == 1 and says(out)[0] == WARN
    assert len(user_messages(env)) == 2   # a warning doesn't block


def test_day_turn_warning(env):
    (env / "state/heardback").mkdir(mode=0o700, parents=True, exist_ok=True)
    (env / "state/heardback/usage.json").write_text(json.dumps(
        {"date": date.today().isoformat(), "turns": 149, "by_model": {"haiku": 149}}))
    out = run_brain(env, [utt("hello"), utt("again")], claude_cmd=FAKE, gap=0.3)
    assert says(out) == ["You said: hello.", "You've used 150 Claude turns today.", "You said: again."]
    assert usage.today_turns() == 151


def test_allowed_warning_is_not_a_limit():   # live 2026-10-30: seven_day at 51 % blocked every Claude turn
    info = {"status": "allowed_warning", "resetsAt": 1793872800, "rateLimitType": "seven_day", "utilization": 0.51,
            "overageStatus": "rejected", "overageDisabledReason": "org_level_disabled", "isUsingOverage": False,
            "unifiedWindows": {"five_hour": {"utilization": 0.63, "resetsAt": 1793392800, "status": "allowed"},
                               "seven_day": {"utilization": 0.51, "resetsAt": 1793872800, "status": "allowed"}},
            "overageEnabled": False}
    g = RateGuard()
    assert g.update(info) is None and g.claude_allowed(1793000000)
    assert g.update({**info, "status": "rejected"}) and not g.claude_allowed(1793000000)


def test_stop_survives_restart_and_daily_rollover(env):
    at = time.time() + 3600
    g = RateGuard()
    g.update(info(0.96, at))
    assert not RateGuard().claude_allowed(at - 1)
    assert RateGuard().claude_allowed(at)
    usage.bump("haiku", "2026-10-29")
    usage.bump("sonnet", "2026-10-30")
    assert not RateGuard().claude_allowed(at - 1)
    assert usage.today_turns() == 0
    saved = json.loads((env / "state/heardback/usage.json").read_text())
    assert saved["off_until"] == at and saved["by_model"] == {"sonnet": 1}


import pytest


@pytest.mark.parametrize("payload", [None, "broken", "[]", '{}', '{"off_until":1}',
                                     '{"off_until":"bad"}', '{"off_until":null}', '{"off_until":Infinity}', '{"off_until":1e100}'])
def test_missing_corrupt_or_past_stop_allows(env, payload):
    path = env / "state/heardback/usage.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if payload is not None:
        path.write_text(payload)
    assert RateGuard().claude_allowed(time.time())


def test_arbitrary_window_uses_haiku_until_latest_reset(env):
    at = time.time() + 3600
    g = RateGuard()
    data = info(0.2, at)
    data["unifiedWindows"]["another_window"] = {"utilization": 0.96, "resetsAt": at + 7200}
    assert g.update(data) == f"One of your Claude limits nearly used. Using Haiku until {usage.spoken(at + 7200)}."
    assert RateGuard().claude_allowed(at + 1)
    assert RateGuard().haiku_until == at + 7200
    g.update(info(0.97, at))
    assert g.off_until == at and g.haiku_until == at + 7200


def test_absent_windows_and_low_arbitrary_window_allow(env):
    g = RateGuard()
    assert g.update({"status": "allowed"}) is None
    assert g.update({"status": "allowed", "unifiedWindows": {
        "other": {"utilization": 0.2, "resetsAt": time.time() + 3600}}}) is None
    assert g.claude_allowed(time.time())


def test_rate_stop_preserves_daily_counter_and_write_error_is_not_fatal(env, monkeypatch):
    usage.bump("haiku")
    g = RateGuard()
    at = time.time() + 3600
    g.update(info(0.96, at))
    assert usage.today_turns() == 1

    def denied(value):
        raise PermissionError("fixture")

    monkeypatch.setattr(usage, "_write", denied)
    assert g.update(info(0.97, at + 1))
    assert not g.claude_allowed(at)


def secondary(at, name="seven_day", util=0.95, status="allowed"):
    return {"status": "allowed", "rateLimitType": name, "resetsAt": at, "utilization": util,
            "unifiedWindows": {name: {"utilization": util, "status": status, "resetsAt": at}}}


@pytest.mark.parametrize("name,label", [("five_hour", "your five-hour Claude limit"),
                                        ("seven_day", "your weekly Claude limit"),
                                        ("unknown", "one of your Claude limits")])
def test_warning_names_window_once_per_reset(env, name, label):
    g, at = RateGuard(), time.time() + 3600
    data = secondary(at, name, 0.8)
    expected = f"Heads up, you've used 80 percent of {label}."
    assert g.update(data) == expected
    assert g.update(data) is None
    assert g.update(secondary(at + 3600, name, 0.8)) == expected


def test_haiku_persists_rollover_latest_reset_and_write_error(env, monkeypatch, caplog):
    at = time.time() + 3600
    g = RateGuard()
    line = f"Weekly Claude limit nearly used. Using Haiku until {usage.spoken(at)}."
    assert g.update(secondary(at)) == line
    assert g.update(secondary(at)) is None
    assert g.claude_allowed(time.time())
    usage.bump("haiku", "2026-10-29")
    usage.bump("haiku", "2026-10-30")
    assert RateGuard().haiku_until == at
    assert RateGuard().update(secondary(at)) is None

    def denied(value):
        raise PermissionError("fixture")

    monkeypatch.setattr(usage, "_write", denied)
    assert g.update(secondary(at + 3600, "seven_day_sonnet", status="rejected"))
    assert g.haiku_until == at + 3600 and g.claude_allowed(time.time())
    assert "cannot persist" in caplog.text
    assert g.update(secondary(at + 1800)) is None
    assert g.haiku_until == at + 3600


@pytest.mark.parametrize("value", [None, "bad", 0, 1, float("inf"), float("nan"), 1e100])
def test_invalid_haiku_state_means_normal_choice(env, value):
    path = env / "state/heardback/usage.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"haiku_until": value}))
    assert RateGuard().haiku_until == 0


def test_per_model_rejection_top_rejection_and_full_stop_precedence(env):
    at = time.time() + 3600
    g = RateGuard()
    data = secondary(at, "seven_day_sonnet", 0.1, "rejected")
    assert "Using Haiku" in g.update(data)
    assert g.claude_allowed(time.time())
    data["status"] = "rejected"
    assert g.update(data) == g.limit()
    assert not g.claude_allowed(time.time())
    g = RateGuard()
    data = secondary(at + 3600)
    data["unifiedWindows"]["five_hour"] = {"utilization": 0.95, "resetsAt": at}
    assert g.update(data) == g.limit()
    assert not g.claude_allowed(time.time()) and g.haiku_until == at + 3600
