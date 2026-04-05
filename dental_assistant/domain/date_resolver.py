"""Naive date resolver for vague expressions like 'next week' or 'early next month'.

Returns an ISO date string (YYYY-MM-DD) representing the best start date.
Returns None if the expression cannot be resolved.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from dental_assistant.domain.constants import WEEKDAYS


def resolve_date(expression: str) -> str | None:
    """Convert a vague date expression to an ISO date string, or None."""
    text = expression.strip().lower()
    today = date.today()

    if text in ("today", "now", "asap"):
        return today.isoformat()

    if text == "tomorrow":
        return (today + timedelta(days=1)).isoformat()

    if text == "next week":
        days_until_monday = (7 - today.weekday()) % 7 or 7
        return (today + timedelta(days=days_until_monday)).isoformat()

    if text == "this week":
        return today.isoformat()

    m = re.match(r"next\s+(monday|mon|tuesday|tue|wednesday|wed|thursday|thu|friday|fri|saturday|sat|sunday|sun)$", text)
    if m:
        target_weekday = WEEKDAYS[m.group(1)]
        days_ahead = (target_weekday - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        return (today + timedelta(days=days_ahead)).isoformat()

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
