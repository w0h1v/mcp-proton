"""Pure time arithmetic: recurrences are computed in local wall-clock time, DST-safe."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from mcp_proton.domain.errors import MailError
from mcp_proton.jobs.schedules import Recurrence, local_to_utc, parse_recurrence, parse_when

BERLIN = ZoneInfo("Europe/Berlin")


def utc(*a: int) -> datetime:
    return datetime(*a, tzinfo=UTC)


def daily(time: str = "09:00", tz: str = "Europe/Berlin") -> Recurrence:
    return Recurrence(freq="daily", time=time, timezone=tz)


def test_daily_keeps_local_time_across_spring_forward():
    rec = daily()
    # Berlin switches to CEST on 2026-03-29: 09:00 local is 08:00Z before, 07:00Z after.
    first = rec.next_after(utc(2026, 3, 28, 0, 0))
    second = rec.next_after(first)
    third = rec.next_after(second)
    assert first == utc(2026, 3, 28, 8, 0)
    assert second == utc(2026, 3, 29, 7, 0)
    assert third == utc(2026, 3, 30, 7, 0)
    for dt in (first, second, third):
        assert dt.astimezone(BERLIN).strftime("%H:%M") == "09:00"


def test_daily_keeps_local_time_across_fall_back():
    rec = daily()
    a = rec.next_after(utc(2026, 10, 24, 12, 0))
    b = rec.next_after(a)
    assert a == utc(2026, 10, 25, 8, 0)  # 09:00 CET (+1) after the switch
    assert a.astimezone(BERLIN).strftime("%H:%M") == "09:00"
    assert b == utc(2026, 10, 26, 8, 0)
    assert b.astimezone(BERLIN).strftime("%H:%M") == "09:00"


def test_nonexistent_local_time_runs_after_the_gap_once():
    rec = daily("02:30")
    # 02:30 does not exist on 2026-03-29 in Berlin; run at the first valid instant after the gap.
    runs = rec.occurrences(utc(2026, 3, 28, 12, 0), utc(2026, 3, 30, 12, 0))
    assert [r.astimezone(BERLIN).strftime("%m-%d %H:%M") for r in runs] == [
        "03-29 03:30", "03-30 02:30"]


def test_ambiguous_local_time_runs_once():
    rec = daily("02:30")
    runs = rec.occurrences(utc(2026, 10, 24, 12, 0), utc(2026, 10, 26, 12, 0))
    local = [r.astimezone(BERLIN).strftime("%m-%d %H:%M") for r in runs]
    assert local == ["10-25 02:30", "10-26 02:30"]  # not twice on 10-25
    assert runs[0] == utc(2026, 10, 25, 0, 30)  # first (CEST) occurrence


def test_strictly_after_and_same_instant():
    rec = daily()
    at = utc(2026, 6, 1, 7, 0)  # exactly 09:00 CEST
    assert rec.next_after(at) == utc(2026, 6, 2, 7, 0)
    assert rec.next_after(utc(2026, 6, 1, 6, 59)) == at


def test_weekly_weekdays_and_timezone():
    rec = Recurrence(freq="weekly", time="18:30", weekdays=[4, 0], timezone="America/New_York")
    # 2026-10-09 is a Friday
    runs = rec.occurrences(utc(2026, 10, 9, 0, 0), utc(2026, 10, 20, 0, 0))
    local = [r.astimezone(ZoneInfo("America/New_York")) for r in runs]
    assert [(d.strftime("%a"), d.strftime("%H:%M")) for d in local] == [
        ("Fri", "18:30"), ("Mon", "18:30"), ("Fri", "18:30"), ("Mon", "18:30")]
    assert local[0].date() == date(2026, 10, 9)


def test_validation():
    with pytest.raises(ValueError):
        Recurrence(freq="weekly", timezone="UTC")  # weekdays missing
    with pytest.raises(ValueError):
        Recurrence(freq="daily", time="25:00", timezone="UTC")
    with pytest.raises(ValueError):
        Recurrence(freq="daily", timezone="Mars/Olympus")
    with pytest.raises(MailError):
        parse_recurrence({"freq": "daily", "time": "9am"}, "UTC")


def test_local_to_utc_and_parse_when():
    assert local_to_utc(date(2026, 7, 1), 12, 0, BERLIN) == utc(2026, 7, 1, 10, 0)
    assert parse_when("2026-07-01T12:00:00+02:00") == utc(2026, 7, 1, 10, 0)
    assert parse_when("2026-07-01T10:00:00Z") == utc(2026, 7, 1, 10, 0)
    assert parse_when("2026-07-01T12:00:00", "Europe/Berlin") == utc(2026, 7, 1, 10, 0)
    with pytest.raises(MailError):
        parse_when("2026-07-01T12:00:00")  # naive without a timezone
    with pytest.raises(MailError):
        parse_when("tomorrow")
