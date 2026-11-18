"""Working-day arithmetic shared by the policy engine and analytics."""
from __future__ import annotations

from datetime import date, timedelta


def public_holidays(year: int) -> set[date]:
    # Simplified global holiday calendar for the PoC. In production this is a
    # per-country calendar service keyed by the employee's work location.
    return {date(year, 1, 1), date(year, 12, 25), date(year, 12, 26)}


def is_working_day(d: date) -> bool:
    return d.weekday() < 5 and d not in public_holidays(d.year)


def working_days_between(start: date, end: date) -> list[date]:
    """Inclusive list of working days in [start, end]."""
    if end < start:
        return []
    days, cur = [], start
    while cur <= end:
        if is_working_day(cur):
            days.append(cur)
        cur += timedelta(days=1)
    return days


def add_working_days(start: date, n: int) -> date:
    """Return the n-th working day counting `start` as day 1 (start is rolled forward if needed)."""
    cur = start
    while not is_working_day(cur):
        cur += timedelta(days=1)
    count = 1
    while count < n:
        cur += timedelta(days=1)
        if is_working_day(cur):
            count += 1
    return cur


def next_working_day(d: date) -> date:
    cur = d + timedelta(days=1)
    while not is_working_day(cur):
        cur += timedelta(days=1)
    return cur


def first_day_next_month(today: date) -> date:
    return date(today.year + (today.month == 12), today.month % 12 + 1, 1)


def nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d + timedelta(weeks=n - 1)


def week_bounds(d: date) -> tuple[date, date]:
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=4)


def overlaps(a_start: date, a_end: date, b_start: date, b_end: date) -> bool:
    return a_start <= b_end and b_start <= a_end
