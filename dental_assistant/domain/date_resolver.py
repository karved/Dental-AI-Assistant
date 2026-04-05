"""Naive date resolver for vague expressions like 'next week' or 'early next month'.

Returns an ISO date string (YYYY-MM-DD) representing the best start date.
Returns None if the expression cannot be resolved.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from dental_assistant.domain.constants import WEEKDAYS
from dental_assistant.domain.pt_time import pt_today


def _next_weekday(today: date, target: int) -> date:
    """Return the next occurrence of a weekday (0=Mon). If today IS that day, returns next week's."""
    days_ahead = (target - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return today + timedelta(days=days_ahead)


def _this_weekday(today: date, target: int) -> date:
    """Return this week's occurrence. If already past, return next week's."""
    days_ahead = (target - today.weekday()) % 7
    if days_ahead == 0:
        return today
    return today + timedelta(days=days_ahead)


def resolve_date(expression: str) -> str | None:
    """Convert a vague date expression to an ISO date string, or None."""
    text = expression.strip().lower()
    today = pt_today()

    if not text:
        return None

    if text in ("today", "now", "asap", "tonight"):
        return today.isoformat()

    if text == "tomorrow":
        return (today + timedelta(days=1)).isoformat()

    if text == "next week":
        days_until_monday = (7 - today.weekday()) % 7 or 7
        return (today + timedelta(days=days_until_monday)).isoformat()

    if text == "this week":
        return today.isoformat()

    # "next monday", "next wed", etc.
    m = re.match(
        r"next\s+(monday|mon|tuesday|tue|wednesday|wed|thursday|thu|friday|fri|saturday|sat|sunday|sun)$",
        text,
    )
    if m:
        return _next_weekday(today, WEEKDAYS[m.group(1)]).isoformat()

    # "this monday", "this wed", etc.
    m = re.match(
        r"this\s+(monday|mon|tuesday|tue|wednesday|wed|thursday|thu|friday|fri|saturday|sat|sunday|sun)$",
        text,
    )
    if m:
        return _this_weekday(today, WEEKDAYS[m.group(1)]).isoformat()

    # bare weekday: "wednesday", "fri" -> next occurrence
    if text in WEEKDAYS:
        return _next_weekday(today, WEEKDAYS[text]).isoformat()

    # "early this week" -> Tuesday or today (whichever is later)
    if "early" in text and "this week" in text:
        tuesday = today - timedelta(days=today.weekday()) + timedelta(days=1)
        return max(tuesday, today).isoformat()

    # "later this week" / "late this week" -> Thursday of current week
    if ("later" in text or "late" in text) and "this week" in text:
        thursday = today - timedelta(days=today.weekday()) + timedelta(days=3)
        if thursday <= today:
            thursday = today + timedelta(days=1)
        return thursday.isoformat()

    if "early next month" in text:
        if today.month == 12:
            first = date(today.year + 1, 1, 1)
        else:
            first = date(today.year, today.month + 1, 1)
        while first.weekday() > 4:
            first += timedelta(days=1)
        return first.isoformat()

    if "next month" in text:
        if today.month == 12:
            return date(today.year + 1, 1, 1).isoformat()
        return date(today.year, today.month + 1, 1).isoformat()

    if "end of" in text and "month" in text:
        if today.month == 12:
            last_day = date(today.year + 1, 1, 1) - timedelta(days=1)
        else:
            last_day = date(today.year, today.month + 1, 1) - timedelta(days=1)
        while last_day.weekday() > 4:
            last_day -= timedelta(days=1)
        return last_day.isoformat()

    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        pass

    return None
