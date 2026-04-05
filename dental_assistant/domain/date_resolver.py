"""Naive date resolver for vague expressions like 'next week' or 'early next month'.

Returns an ISO date string (YYYY-MM-DD) representing the best start date.
Returns None if the expression cannot be resolved.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from difflib import get_close_matches

from dental_assistant.domain.constants import WEEKDAYS
from dental_assistant.domain.pt_time import pt_today

_MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

_CANONICAL_WEEKDAYS = {
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
}


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


def _start_of_next_week(today: date) -> date:
    days_until_monday = (7 - today.weekday()) % 7 or 7
    return today + timedelta(days=days_until_monday)


def _first_day_of_next_month(today: date) -> date:
    if today.month == 12:
        return date(today.year + 1, 1, 1)
    return date(today.year, today.month + 1, 1)


def _last_day_of_next_month(today: date) -> date:
    first = _first_day_of_next_month(today)
    if first.month == 12:
        return date(first.year + 1, 1, 1) - timedelta(days=1)
    return date(first.year, first.month + 1, 1) - timedelta(days=1)


def _last_business_day(d: date) -> date:
    while d.weekday() > 4:
        d -= timedelta(days=1)
    return d


def _month_window(today: date, month_num: int) -> tuple[date, date]:
    year = today.year
    if month_num < today.month:
        year += 1
    start = date(year, month_num, 1)
    if month_num == 12:
        end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(year, month_num + 1, 1) - timedelta(days=1)
    return start, end


def _named_month(text: str) -> int | None:
    m = re.fullmatch(r"(?:in\s+|this\s+)?([a-z]+)(?:\s+month)?", text.strip().lower())
    if not m:
        return None
    return _MONTHS.get(m.group(1))


def _clean_text(text: str) -> str:
    lowered = text.strip().lower()
    lowered = re.sub(r"[^a-z0-9,\s/-]", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered).strip()
    return lowered


def _fuzzy_weekday_token(text: str) -> str | None:
    cleaned = _clean_text(text)
    token = cleaned.split()[0] if cleaned else ""
    if token in WEEKDAYS:
        return token
    match = get_close_matches(token, sorted(_CANONICAL_WEEKDAYS), n=1, cutoff=0.75)
    return match[0] if match else None


def _month_day_date(text: str, today: date) -> date | None:
    cleaned = _clean_text(text)
    m = re.fullmatch(r"([a-z]+)\s+(\d{1,2})(?:,\s*(\d{4}))?", cleaned)
    if not m:
        return None
    month_num = _MONTHS.get(m.group(1))
    if month_num is None:
        return None
    day_num = int(m.group(2))
    year = int(m.group(3)) if m.group(3) else today.year
    try:
        candidate = date(year, month_num, day_num)
    except ValueError:
        return None
    return candidate


def resolve_date_range(expression: str) -> tuple[str, str] | None:
    """Return a date window (inclusive) for vague phrases, or None if unknown."""
    text = _clean_text(expression)
    today = pt_today()
    if not text:
        return None

    if text == "week after":
        start = _start_of_next_week(today) + timedelta(days=7)
        return start.isoformat(), (start + timedelta(days=5)).isoformat()

    m = re.fullmatch(r"(early|mid|middle|late|later)\s+([a-z]+)", text)
    if m and m.group(2) in _MONTHS:
        qual = m.group(1)
        month_num = _MONTHS[m.group(2)]
        start, end = _month_window(today, month_num)
        if start.month == today.month and start.year == today.year and start < today:
            start = today
        if qual == "early":
            return start.isoformat(), min(end, start + timedelta(days=9)).isoformat()
        if qual in ("mid", "middle"):
            mid_start = start.replace(day=11)
            mid_end = min(end, start.replace(day=20))
            if mid_start < today <= mid_end:
                mid_start = today
            return mid_start.isoformat(), mid_end.isoformat()
        late_start = start.replace(day=21)
        if late_start < today <= end:
            late_start = today
        return late_start.isoformat(), end.isoformat()

    month_day = _month_day_date(text, today)
    if month_day is not None:
        if month_day < today:
            return None
        iso = month_day.isoformat()
        return iso, iso

    named_month = _named_month(text)
    if named_month is not None:
        start, end = _month_window(today, named_month)
        if start.month == today.month and start.year == today.year and start < today:
            start = today
        if "early" in text:
            return start.isoformat(), min(end, start + timedelta(days=9)).isoformat()
        if "mid" in text or "middle" in text:
            mid_start = start.replace(day=11)
            mid_end = min(end, start.replace(day=20))
            if mid_start < today <= mid_end:
                mid_start = today
            return mid_start.isoformat(), mid_end.isoformat()
        if "later" in text or "late" in text:
            late_start = start.replace(day=21)
            if late_start < today <= end:
                late_start = today
            return late_start.isoformat(), end.isoformat()
        return start.isoformat(), end.isoformat()

    m = re.fullmatch(r"(?:end\s+of\s+|late\s+)?([a-z]+)\s+end", text)
    if m and m.group(1) in _MONTHS:
        month_num = _MONTHS[m.group(1)]
        start, end = _month_window(today, month_num)
        late_start = start.replace(day=21)
        if late_start < today <= end:
            late_start = today
        return late_start.isoformat(), end.isoformat()

    m = re.fullmatch(r"(?:end\s+of\s+)([a-z]+)", text)
    if m and m.group(1) in _MONTHS:
        month_num = _MONTHS[m.group(1)]
        start, end = _month_window(today, month_num)
        late_start = start.replace(day=21)
        if late_start < today <= end:
            late_start = today
        return late_start.isoformat(), end.isoformat()

    exact = resolve_date(text)
    if exact and not any(token in text for token in ("week", "month")):
        return exact, exact

    if text == "this week":
        end = today - timedelta(days=today.weekday()) + timedelta(days=5)
        return today.isoformat(), end.isoformat()

    if text == "next week":
        start = _start_of_next_week(today)
        return start.isoformat(), (start + timedelta(days=5)).isoformat()

    if "early next week" in text:
        start = _start_of_next_week(today)
        return start.isoformat(), (start + timedelta(days=2)).isoformat()

    if "mid next week" in text or "middle of next week" in text:
        start = _start_of_next_week(today) + timedelta(days=2)
        return start.isoformat(), (start + timedelta(days=1)).isoformat()

    if ("later" in text or "late" in text) and "next week" in text:
        start = _start_of_next_week(today) + timedelta(days=3)
        return start.isoformat(), (start + timedelta(days=2)).isoformat()

    if "this month" in text:
        end = _last_business_day(_first_day_of_next_month(today) - timedelta(days=1))
        return today.isoformat(), end.isoformat()

    if "next month" in text:
        start = _first_day_of_next_month(today)
        end = _last_day_of_next_month(today)
        if "early" in text:
            return start.isoformat(), (start + timedelta(days=9)).isoformat()
        if "mid" in text or "middle" in text:
            mid = start.replace(day=11)
            return mid.isoformat(), start.replace(day=20).isoformat()
        if "later" in text or "late" in text:
            late = start.replace(day=21)
            return late.isoformat(), end.isoformat()
        return start.isoformat(), end.isoformat()

    return None


def resolve_date(expression: str) -> str | None:
    """Convert a vague date expression to an ISO date string, or None."""
    text = _clean_text(expression)
    today = pt_today()

    if not text:
        return None

    if text == "week after":
        return (_start_of_next_week(today) + timedelta(days=7)).isoformat()

    m = re.fullmatch(r"(early|mid|middle|late|later)\s+([a-z]+)", text)
    if m and m.group(2) in _MONTHS:
        qual = m.group(1)
        month_num = _MONTHS[m.group(2)]
        start, _end = _month_window(today, month_num)
        if start.month == today.month and start.year == today.year and start < today:
            start = today
        if qual == "early":
            return start.isoformat()
        if qual in ("mid", "middle"):
            mid = start.replace(day=15)
            return max(mid, today).isoformat() if start.month == today.month and start.year == today.year else mid.isoformat()
        late = start.replace(day=21)
        return max(late, today).isoformat() if start.month == today.month and start.year == today.year else late.isoformat()

    month_day = _month_day_date(text, today)
    if month_day is not None:
        if month_day < today:
            return None
        return month_day.isoformat()

    named_month = _named_month(text)
    if named_month is not None:
        start, _end = _month_window(today, named_month)
        if start.month == today.month and start.year == today.year and start < today:
            start = today
        if "early" in text:
            return start.isoformat()
        if "mid" in text or "middle" in text:
            mid = start.replace(day=15)
            return max(mid, today).isoformat() if start.month == today.month and start.year == today.year else mid.isoformat()
        if "later" in text or "late" in text:
            late = start.replace(day=21)
            return max(late, today).isoformat() if start.month == today.month and start.year == today.year else late.isoformat()
        return start.isoformat()

    m = re.fullmatch(r"(?:end\s+of\s+|late\s+)?([a-z]+)\s+end", text)
    if m and m.group(1) in _MONTHS:
        month_num = _MONTHS[m.group(1)]
        start, end = _month_window(today, month_num)
        late = start.replace(day=21)
        if late < today <= end:
            late = today
        return late.isoformat()

    m = re.fullmatch(r"(?:end\s+of\s+)([a-z]+)", text)
    if m and m.group(1) in _MONTHS:
        month_num = _MONTHS[m.group(1)]
        start, end = _month_window(today, month_num)
        late = start.replace(day=21)
        if late < today <= end:
            late = today
        return late.isoformat()

    if text in ("today", "now", "asap", "tonight"):
        return today.isoformat()

    if text == "tomorrow":
        return (today + timedelta(days=1)).isoformat()

    if text == "next week":
        return _start_of_next_week(today).isoformat()

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

    fuzzy_weekday = _fuzzy_weekday_token(text)
    if fuzzy_weekday:
        if text.startswith("next "):
            return _next_weekday(today, WEEKDAYS[fuzzy_weekday]).isoformat()
        if text.startswith("this "):
            return _this_weekday(today, WEEKDAYS[fuzzy_weekday]).isoformat()
        return _next_weekday(today, WEEKDAYS[fuzzy_weekday]).isoformat()

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

    if "early next week" in text:
        return (_start_of_next_week(today) + timedelta(days=1)).isoformat()

    if "mid next week" in text or "middle of next week" in text:
        return (_start_of_next_week(today) + timedelta(days=2)).isoformat()

    if ("later" in text or "late" in text) and "next week" in text:
        return (_start_of_next_week(today) + timedelta(days=3)).isoformat()

    if "early next month" in text:
        first = _first_day_of_next_month(today)
        while first.weekday() > 4:
            first += timedelta(days=1)
        return first.isoformat()

    if "mid next month" in text or "middle of next month" in text:
        mid = _first_day_of_next_month(today).replace(day=15)
        while mid.weekday() > 4:
            mid += timedelta(days=1)
        return mid.isoformat()

    if ("later" in text or "late" in text) and "next month" in text:
        late = _last_day_of_next_month(today) - timedelta(days=6)
        while late.weekday() > 4:
            late -= timedelta(days=1)
        return late.isoformat()

    if "next month" in text:
        return _first_day_of_next_month(today).isoformat()

    if "end of" in text and "month" in text:
        if today.month == 12:
            last_day = date(today.year + 1, 1, 1) - timedelta(days=1)
        else:
            last_day = date(today.year, today.month + 1, 1) - timedelta(days=1)
        while last_day.weekday() > 4:
            last_day -= timedelta(days=1)
        return last_day.isoformat()

    try:
        parsed = date.fromisoformat(text)
        if parsed < today:
            return None
        return parsed.isoformat()
    except ValueError:
        pass

    return None
