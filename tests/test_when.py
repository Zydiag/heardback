from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from heardback.brain.router import normalise
from jv.when import parse_when


@pytest.mark.parametrize("phrase, now, expected", [
    ("in 5 minutes", datetime(2026, 1, 5, 10), datetime(2026, 1, 5, 10, 5)),
    ("in 2 hours", datetime(2026, 1, 5, 10), datetime(2026, 1, 5, 12)),
    ("in 3 days", datetime(2026, 1, 5, 10), datetime(2026, 1, 8, 10)),
    ("in half an hour", datetime(2026, 1, 5, 10), datetime(2026, 1, 5, 10, 30)),
    ("at 6", datetime(2026, 1, 5, 5, 59), datetime(2026, 1, 5, 6)),
    ("at 6", datetime(2026, 1, 5, 6, 1), datetime(2026, 1, 5, 18)),
    ("at 6", datetime(2026, 1, 5, 6), datetime(2026, 1, 5, 18)),
    ("at 6 am", datetime(2026, 1, 5, 10), datetime(2026, 1, 6, 6)),
    ("at 6 pm", datetime(2026, 1, 5, 10), datetime(2026, 1, 5, 18)),
    ("at 1140", datetime(2026, 1, 5, 10, 47), datetime(2026, 1, 5, 11, 40)),
    ("at 930 pm", datetime(2026, 1, 5, 10), datetime(2026, 1, 5, 21, 30)),
    ("at 1830", datetime(2026, 1, 5, 10), datetime(2026, 1, 5, 18, 30)),
    ("noon", datetime(2026, 1, 5, 13), datetime(2026, 1, 6, 12)),
    ("midnight", datetime(2026, 1, 5, 10), datetime(2026, 1, 6)),
    ("tonight", datetime(2026, 1, 5, 10), datetime(2026, 1, 5, 20)),
    ("tomorrow", datetime(2026, 1, 5, 10), datetime(2026, 1, 6, 9)),
    ("monday", datetime(2026, 1, 5, 10), datetime(2026, 1, 12, 9)),
    ("next monday", datetime(2026, 1, 7, 10), datetime(2026, 1, 12, 9)),
    ("next friday", datetime(2026, 1, 7, 10), datetime(2026, 1, 16, 9)),
    ("1 jan 2027", datetime(2026, 1, 1), datetime(2027, 1, 1, 9)),
    ("29 feb 2028", datetime(2028, 1, 1), datetime(2028, 2, 29, 9)),
    ("half past 1 pm", datetime(2026, 1, 5, 10), datetime(2026, 1, 5, 13, 30)),
    ("half past 10 am", datetime(2026, 1, 5, 10), datetime(2026, 1, 5, 10, 30)),
    ("half past 11 am", datetime(2026, 1, 5, 10), datetime(2026, 1, 5, 11, 30)),
    ("half past 12 pm", datetime(2026, 1, 5, 10), datetime(2026, 1, 5, 12, 30)),
    ("quarter past 10", datetime(2026, 1, 5, 10), datetime(2026, 1, 5, 10, 15)),
    ("quarter to 11", datetime(2026, 1, 5, 10), datetime(2026, 1, 5, 10, 45)),
    ("6.30", datetime(2026, 1, 5, 5), datetime(2026, 1, 5, 6, 30)),
    ("18:30", datetime(2026, 1, 5, 5), datetime(2026, 1, 5, 18, 30)),
    ("06:30", datetime(2026, 1, 5), datetime(2026, 1, 5, 6, 30)),
])
def test_whole_time_forms(phrase, now, expected):
    status, due, rest, _, _ = parse_when(phrase, now, whole=True)
    assert (status, due, rest) == ("ok", expected, "")


@pytest.mark.parametrize("phrase, expected_rest", [
    ("6 p.m.", ""), ("6 p.m", ""), ("6.30", ""), ("18:30", ""),
    ("at 7", ""), ("call at 6", "call"),
    ("buy 6 eggs at 7", "buy 6 eggs"),
    ("call that at 6", "call"), ("call to at 6", "call"), ("call about at 6", "call"),
])
def test_text_time_extraction(phrase, expected_rest):
    status, due, rest, _, _ = parse_when(phrase, datetime(2026, 1, 5, 10))
    assert status == "ok" and due and rest == expected_rest


def test_router_normalized_meridiem():
    status, due, _, _, _ = parse_when(normalise("6 p.m."), datetime(2026, 1, 5, 10))
    assert (status, due) == ("ok", datetime(2026, 1, 5, 18))


@pytest.mark.parametrize("phrase, expected", [
    ("tomorrow at 6 pm", ("", datetime(2026, 1, 6, 18))),
    ("call mum tomorrow at 6 pm", ("call mum", datetime(2026, 1, 6, 18))),
    ("monday at 5 pm", ("", datetime(2026, 1, 12, 17))),
    ("tonight at 8", ("", datetime(2026, 1, 5, 20))),
    ("tomorrow at 18:30", ("", datetime(2026, 1, 6, 18, 30))),
])
def test_date_and_clock_are_one_time(phrase, expected):
    now = datetime(2026, 1, 5, 10)
    status, due, remaining, _, _ = parse_when(phrase, now, whole=not expected[0])
    assert (status, due, remaining) == ("ok", expected[1], expected[0])


@pytest.mark.parametrize("phrase", ["tomorrow at 6", "today at 6", "monday at 6", "1 feb at 6"])
def test_date_with_bare_hour_requires_am_pm(phrase):
    result = parse_when(phrase, datetime(2026, 1, 5, 10), whole=True)
    assert result[0] == "refuse" and "am or pm" in result[3].lower()


@pytest.mark.parametrize("phrase, now", [
    ("buy 6 eggs", datetime(2026, 1, 5, 10)), ("about 6", datetime(2026, 1, 5, 10)),
    ("call about 6", datetime(2026, 1, 5, 10)),
    ("later", datetime(2026, 1, 5, 10)),
])
def test_no_marked_time_hands_off(phrase, now):
    status, due, remaining, say, _ = parse_when(phrase, now)
    assert (status, due, remaining, say) == ("handoff", None, phrase, "")


@pytest.mark.parametrize("phrase", ["every day at 6", "daily at 6", "weekly at 6"])
def test_recurring_is_refused(phrase):
    assert parse_when(phrase, datetime(2026, 1, 5), whole=True)[0] == "refuse"


@pytest.mark.parametrize("phrase, now", [
    ("at 25", datetime(2026, 1, 5)), ("at 6 and at 7", datetime(2026, 1, 5)),
    ("tomorrow afternoon", datetime(2026, 1, 5)), ("31 feb 2028", datetime(2028, 1, 1)),
    ("29 feb 2027", datetime(2027, 1, 1)),
])
def test_invalid_or_ambiguous_time_refused(phrase, now):
    assert parse_when(phrase, now, whole=True)[0] == "refuse"


@pytest.mark.parametrize("now, phrase, expected", [
    (datetime(2026, 3, 7, 12, tzinfo=ZoneInfo("America/New_York")), "tomorrow", datetime(2026, 3, 8, 9, tzinfo=ZoneInfo("America/New_York"))),
    (datetime(2026, 3, 8, 1, 30, tzinfo=ZoneInfo("America/New_York")), "in 1 hour", datetime(2026, 3, 8, 3, 30, tzinfo=ZoneInfo("America/New_York"))),
    (datetime(2026, 3, 8, 3, 30, tzinfo=ZoneInfo("America/New_York")), "in 1 hour", datetime(2026, 3, 8, 4, 30, tzinfo=ZoneInfo("America/New_York"))),
    (datetime(2026, 1, 5, 10, tzinfo=ZoneInfo("Asia/Kolkata")), "in 2 hours", datetime(2026, 1, 5, 12, tzinfo=ZoneInfo("Asia/Kolkata"))),
])
def test_zoned_times(now, phrase, expected):
    assert parse_when(phrase, now, whole=True)[1] == expected


@pytest.mark.parametrize("phrase, expected", [("tomorrow", "9 am"), ("6", "6 pm"), ("540", "5:40 pm")])
def test_assumption_is_returned(phrase, expected):
    result = parse_when(phrase, datetime(2026, 1, 5, 10), whole=True)
    assert result[0] == "ok" and result[4] == expected


@pytest.mark.parametrize("phrase, now", [
    ("today", datetime(2026, 1, 5, 10)), ("tonight at 8", datetime(2026, 1, 5, 21)),
    ("today at 6 am", datetime(2026, 1, 5, 10)),
    ("1 jan 2028", datetime(2026, 1, 1)), ("1 jan 2024", datetime(2026, 1, 1)),
    ("in 367 days", datetime(2026, 1, 1)),
])
def test_past_or_out_of_range_refused(phrase, now):
    assert parse_when(phrase, now, whole=True)[0] == "refuse"


def test_one_year_limit_in_range():
    assert parse_when("in 365 days", datetime(2026, 1, 1), whole=True)[0] == "ok"


@pytest.mark.parametrize("phrase, now, status, due", [
    ("31 jan", datetime(2026, 12, 1), "ok", datetime(2027, 1, 31, 9)),
    ("31 apr 2027", datetime(2026, 1, 1), "refuse", None),
])
def test_year_rollover_and_short_month(phrase, now, status, due):
    assert parse_when(phrase, now, True)[:2] == (status, due)


@pytest.mark.parametrize("phrase", ["tomorrow afternoon", "call at 6 please"])
def test_whole_string_rejects_extra_words(phrase):
    assert parse_when(phrase, datetime(2026, 1, 5, 10), whole=True)[0] == "refuse"


@pytest.mark.parametrize("phrase, due, remaining", [
    ("call at half past 10 pm", datetime(2026, 1, 5, 22, 30), "call"),
    ("call at quarter to 11 pm", datetime(2026, 1, 5, 22, 45), "call"),
    ("call at 06:30", datetime(2026, 1, 6, 6, 30), "call"),
    ("tomorrow at 06:30", datetime(2026, 1, 6, 6, 30), ""),
    ("at 6 pm tomorrow", datetime(2026, 1, 6, 18), ""),
    ("call at 18:30 tomorrow", datetime(2026, 1, 6, 18, 30), "call"),
    ("call tomorrow at noon", datetime(2026, 1, 6, 12), "call"),
    ("tomorrow at midnight", datetime(2026, 1, 7), ""),
    ("call noon", datetime(2026, 1, 5, 12), "call"),
    ("call midnight", datetime(2026, 1, 6), "call"),
    ("call half past 10 am", datetime(2026, 1, 5, 10, 30), "call"),
    ("call quarter to 11", datetime(2026, 1, 5, 10, 45), "call"),
    ("call on monday at 5 pm", datetime(2026, 1, 12, 17), "call"),
    ("call at 6 pm about the trip", datetime(2026, 1, 5, 18), "call about the trip"),
    ("at 6 pm call mum", datetime(2026, 1, 5, 18), "call mum"),
    ("call at 6 pm to ask mum", datetime(2026, 1, 5, 18), "call to ask mum"),
    ("in 1 hour call mum", datetime(2026, 1, 5, 11), "call mum"),
    ("to call mum at 6 pm", datetime(2026, 1, 5, 18), "call mum"),
    ("call at 18 30", datetime(2026, 1, 5, 18, 30), "call"),
])
def test_review_exact_times_and_remaining(phrase, due, remaining):
    assert parse_when(phrase, datetime(2026, 1, 5, 10))[:3] == ("ok", due, remaining)


@pytest.mark.parametrize("phrase", [
    "call in 1 hour and 30 minutes", "call tomorrow evening", "call tomorrow after lunch",
    "call tomorrow afternoon", "call at 6:3", "call at 6:300", "call at 6:30:45",
    "call at 6.3", "call at 6.300", "call at 6:30 pmish", "call at 6 pm UTC",
    "call in 2 hours 30 minutes", "call in 2 hours and 1 day",
    "call in 1 hour tomorrow", "in half an hour tomorrow", "in half a hour",
    "call in half a hour", "call in 1 day monday",
    "call tomorrow, evening", "call after lunch tomorrow", "call at 6:",
    "call at 6..30", "call at 6 pm that evening", "call at 6 p",
    "call at 6/30", "call at 6-30", "call at 6 3", "call in 1.50 hours",
    "call in -1 hour tomorrow",
])
@pytest.mark.parametrize("whole", [False, True])
def test_unsupported_time_is_never_partially_set(phrase, whole):
    result = parse_when(phrase, datetime(2026, 1, 5, 10), whole)
    assert result[0] == ("refuse" if whole else "handoff")
    assert result[1] is None and result[2] == phrase
    if whole:
        assert result[3]


@pytest.mark.parametrize("phrase", [
    "call at 6 pm or midnight", "call at 6 pm and noon",
    "call at 6 pm and half past 10", "call at 6 and 7",
])
def test_extra_time_never_becomes_reminder_text(phrase):
    result = parse_when(phrase, datetime(2026, 1, 5, 10))
    assert result[0] in ("refuse", "handoff") and result[1] is None


@pytest.mark.parametrize("phrase", [
    "quarter to 25", "quarter to 99", "quarter to 0", "call at 24:30",
    "at 24:00", "half past 25", "quarter past 99", "at 6:99",
])
@pytest.mark.parametrize("whole", [False, True])
def test_invalid_clock_is_refused(phrase, whole):
    result = parse_when(phrase, datetime(2026, 1, 5, 10), whole)
    assert result[0] == "refuse" and result[1] is None and result[3]


@pytest.mark.parametrize("phrase", ["about 6", "to tomorrow", "that tomorrow", "tomorrow about", "tomorrow to"])
def test_whole_validation_precedes_filler_cleanup(phrase):
    result = parse_when(phrase, datetime(2026, 1, 5, 10), True)
    assert result[0] == "refuse" and result[1] is None and result[3]


@pytest.mark.parametrize("phrase, expected", [
    ("half past 1 pm", datetime(2026, 1, 5, 13, 30)),
    ("half past 10 am", datetime(2026, 1, 5, 10, 30)),
    ("half past 11 am", datetime(2026, 1, 5, 11, 30)),
    ("half past 12 pm", datetime(2026, 1, 5, 12, 30)),
    ("quarter past 1", datetime(2026, 1, 5, 13, 15)),
    ("quarter past 10", datetime(2026, 1, 5, 10, 15)),
    ("quarter past 11", datetime(2026, 1, 5, 11, 15)),
    ("quarter past 12", datetime(2026, 1, 5, 12, 15)),
    ("quarter to 1", datetime(2026, 1, 5, 12, 45)),
    ("quarter to 10", datetime(2026, 1, 5, 21, 45)),
    ("quarter to 11", datetime(2026, 1, 5, 10, 45)),
    ("quarter to 12", datetime(2026, 1, 5, 11, 45)),
])
@pytest.mark.parametrize("whole", [False, True])
def test_complete_fractional_clock_matrix(phrase, expected, whole):
    assert parse_when(phrase, datetime(2026, 1, 5, 10), whole)[:3] == ("ok", expected, "")


@pytest.mark.parametrize("phrase", ["tomorrow half past 10", "tomorrow quarter past 10", "tomorrow quarter to 11"])
def test_date_fractional_clock_requires_meridiem(phrase):
    result = parse_when(phrase, datetime(2026, 1, 5, 10), True)
    assert result[0] == "refuse" and "am or pm" in result[3]


@pytest.mark.parametrize("phrase, expected", [
    ("6.30", datetime(2026, 1, 5, 18, 30)),
    ("6:30", datetime(2026, 1, 5, 18, 30)),
    ("18:30", datetime(2026, 1, 5, 18, 30)),
    ("06:30", datetime(2026, 1, 6, 6, 30)),
])
@pytest.mark.parametrize("normalized", [False, True])
@pytest.mark.parametrize("whole", [False, True])
def test_raw_and_normalized_clock_minutes(phrase, expected, normalized, whole):
    text = phrase if whole else f"call at {phrase}"
    if normalized:
        text = normalise(text)
    result = parse_when(text, datetime(2026, 1, 5, 10), whole)
    assert result[:3] == ("ok", expected, "" if whole else "call")


@pytest.mark.parametrize("phrase", ["6 30", "buy 6 eggs", "about 6"])
def test_bare_numbers_still_handoff(phrase):
    assert parse_when(phrase, datetime(2026, 1, 5, 10)) == ("handoff", None, phrase, "", "")


@pytest.mark.parametrize("phrase, now, expected", [
    ("at 6", datetime(2026, 1, 5, 18), datetime(2026, 1, 6, 6)),
    ("at 6", datetime(2026, 1, 5, 20), datetime(2026, 1, 6, 6)),
    ("at 6", datetime(2026, 1, 5, 23), datetime(2026, 1, 6, 6)),
    ("at 12:30", datetime(2026, 1, 5), datetime(2026, 1, 5, 0, 30)),
    ("at 06:30", datetime(2026, 1, 5, 20), datetime(2026, 1, 6, 6, 30)),
    ("31 jan", datetime(2026, 12, 1), datetime(2027, 1, 31, 9)),
    ("1 jan 2027", datetime(2026, 1, 1), datetime(2027, 1, 1, 9)),
    ("28 feb 2029 at 11 pm", datetime(2028, 2, 29, 10), datetime(2029, 2, 28, 23)),
    ("29 feb", datetime(2027, 12, 1), datetime(2028, 2, 29, 9)),
])
def test_rollovers_and_calendar_limit_allowed(phrase, now, expected):
    assert parse_when(phrase, now, True)[:3] == ("ok", expected, "")


@pytest.mark.parametrize("phrase, now", [
    ("in 366 days", datetime(2026, 1, 1)),
    ("2 jan 2027", datetime(2026, 1, 1, 10)),
    ("1 mar 2029", datetime(2028, 2, 29, 10)),
    ("31 apr 2027", datetime(2026, 1, 1)),
])
def test_calendar_limit_and_invalid_dates_refused(phrase, now):
    assert parse_when(phrase, now, True)[0] == "refuse"


@pytest.mark.parametrize("phrase, expected", [
    ("tomorrow at midnight", datetime(2026, 1, 7)),
    ("at midnight tomorrow", datetime(2026, 1, 7)),
    ("midnight tomorrow", datetime(2026, 1, 7)),
])
def test_date_midnight_explains_ending_night(phrase, expected):
    result = parse_when(phrase, datetime(2026, 1, 5, 10), True)
    assert result[:3] == ("ok", expected, "")
    assert "2026-01-07" in result[4] and "00:00" in result[4]


@pytest.mark.parametrize("phrase, now, expected, fold", [
    ("in 1 hour", datetime(2026, 11, 1, 1, 30), datetime(2026, 11, 1, 1, 30), 1),
    ("in 30 minutes", datetime(2026, 11, 1, 1, 30), datetime(2026, 11, 1, 1), 1),
    ("in 90 minutes", datetime(2026, 11, 1, 1, 30), datetime(2026, 11, 1, 2), 0),
    ("tomorrow at 1:30 am", datetime(2026, 10, 31, 10), datetime(2026, 11, 1, 1, 30), 0),
    ("in 1 day", datetime(2026, 3, 7, 10), datetime(2026, 3, 8, 10), 0),
    ("at 2:30", datetime(2026, 3, 8, 3, 15), datetime(2026, 3, 8, 14, 30), 0),
    ("at 2:30 am", datetime(2026, 3, 8, 3, 15), datetime(2026, 3, 9, 2, 30), 0),
])
def test_dst_exact_instant_and_fold(phrase, now, expected, fold):
    zone = ZoneInfo("America/New_York")
    result = parse_when(phrase, now.replace(tzinfo=zone), True)
    target = expected.replace(tzinfo=zone, fold=fold)
    assert result[0] == "ok" and result[1].isoformat() == target.isoformat()
    assert result[1].timestamp() == target.timestamp() and result[1].fold == fold


@pytest.mark.parametrize("phrase, now", [
    ("tomorrow at 2:30 am", datetime(2026, 3, 7, 10)),
    ("at 2:30 am", datetime(2026, 3, 8, 1)),
    ("in 1 day", datetime(2026, 3, 7, 2, 30)),
])
def test_nonexistent_wall_clock_is_refused(phrase, now):
    result = parse_when(phrase, now.replace(tzinfo=ZoneInfo("America/New_York")), True)
    assert result[0] == "refuse" and result[1] is None and result[3]


@pytest.mark.parametrize("phrase, expected", [
    ("at noon tomorrow", datetime(2026, 1, 6, 12)),
    ("noon tomorrow", datetime(2026, 1, 6, 12)),
    ("at 18:30 tomorrow", datetime(2026, 1, 6, 18, 30)),
    ("6 pm tomorrow", datetime(2026, 1, 6, 18)),
    ("tomorrow at half past 10 pm", datetime(2026, 1, 6, 22, 30)),
    ("at quarter to 11 pm tomorrow", datetime(2026, 1, 6, 22, 45)),
    ("quarter to 12 am tomorrow", datetime(2026, 1, 6, 23, 45)),
])
@pytest.mark.parametrize("whole", [False, True])
def test_date_named_and_fractional_clock_orders(phrase, expected, whole):
    assert parse_when(phrase, datetime(2026, 1, 5, 10), whole)[:3] == ("ok", expected, "")


def test_invalid_end_of_day_cannot_wrap_to_this_morning():
    result = parse_when("at 24:30", datetime(2026, 1, 5, 0, 10), True)
    assert result[0] == "refuse" and result[1] is None


NOW = datetime(2026, 1, 5, 10)


@pytest.mark.parametrize("phrase, rest, minutes", [
    ("in one minute to close the laptop", "close the laptop", 1),
    ("to stretch in ten minutes", "stretch", 10),
    ("in a minute to stretch", "stretch", 1),
    ("in an hour to stretch", "stretch", 60),
    ("stretch in two hours", "stretch", 120),
    ("in twenty five minutes stretch", "stretch", 25),
    ("for 2 minutes to drink water", "drink water", 2),
    ("for one hour drink water", "drink water", 60),
    ("after 10 minutes to call mum", "call mum", 10),
    ("call mum after ten minutes", "call mum", 10),
])
def test_spoken_durations(phrase, rest, minutes):
    status, due, left, _, _ = parse_when(phrase, NOW)
    assert (status, left) == ("ok", rest) and (due - NOW).total_seconds() == minutes * 60


@pytest.mark.parametrize("phrase, minutes", [
    ("2 minutes", 2), ("one minute", 1), ("an hour", 60), ("half an hour", 30),
    ("for 2 minutes", 2), ("after 5 minutes", 5), ("in one minute", 1),
])
def test_claude_when_durations(phrase, minutes):
    status, due, _, _, _ = parse_when(phrase, NOW, whole=True)
    assert status == "ok" and (due - NOW).total_seconds() == minutes * 60


@pytest.mark.parametrize("phrase", [
    "in two hours and thirty minutes to stretch",   # never set the first half of a time
    "in an hour and a half to stretch",
    "in one hour and 30 minutes to stretch",
    "walk for 20 minutes",                          # a length, not when
    "remind the team in a while",
    "in a few minutes to stretch",
    "in one to stretch",
])
def test_half_understood_durations_are_not_set(phrase):
    assert parse_when(phrase, NOW)[0] != "ok"


def test_set_a_reminder_with_no_task_hands_off():
    assert parse_when("for 2 minutes", NOW)[:3:2] == ("ok", "")


@pytest.mark.parametrize('phrase, now, status, expected', [
    ('this evening', datetime(2026, 1, 5, 10), 'ok', datetime(2026, 1, 5, 19)),
    ('this evening', datetime(2026, 1, 5, 20), 'refuse', None),
    ('this morning', datetime(2026, 1, 5, 8), 'ok', datetime(2026, 1, 5, 9)),
    ('this morning', datetime(2026, 1, 5, 10), 'refuse', None),
    ('tomorrow morning', datetime(2026, 1, 5, 10), 'ok', datetime(2026, 1, 6, 9)),
    ('half past six', datetime(2026, 1, 5, 10), 'refuse', None),
    ('half past 6', datetime(2026, 1, 5, 10), 'refuse', None),
    ('half past six pm', datetime(2026, 1, 5, 10), 'ok', datetime(2026, 1, 5, 18, 30)),
    ('half past six tonight', datetime(2026, 1, 5, 10), 'ok', datetime(2026, 1, 5, 18, 30)),
    ('half past six this evening', datetime(2026, 1, 5, 10), 'ok', datetime(2026, 1, 5, 18, 30)),
    ('in 90 seconds', datetime(2026, 1, 5, 10), 'ok', datetime(2026, 1, 5, 10, 1, 30)),
    ('in 0 seconds', datetime(2026, 1, 5, 10), 'refuse', None),
])
@pytest.mark.parametrize('whole', [False, True])
def test_quick_time_forms(phrase, now, status, expected, whole):
    got = parse_when(phrase, now, whole)
    assert got[:2] == (status, expected)
    if phrase in ('half past six', 'half past 6'):
        assert got[3] == 'Please say am or pm.'


def test_supported_time_refusal_examples():
    assert parse_when('after lunch', datetime(2026, 1, 5, 10), True)[3] == 'Please give a clock time like 6:30 pm, or a duration like in 20 minutes.'


def test_two_times_hand_off_to_claude_but_refuse_inside_one_call():
    text = "to drink water after 5 minutes and change to fable at 11"
    assert parse_when(text, datetime(2026, 1, 5, 10))[0] == "handoff"
    assert parse_when("in 5 minutes and at 11 pm", datetime(2026, 1, 5, 10), whole=True)[0] == "refuse"
