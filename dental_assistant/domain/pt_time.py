"""Office calendar and clock in a single display timezone (default US Pacific).

All vague dates ("today", "tonight", "tomorrow") and business-hours checks use
this zone so the agent stays consistent regardless of server location.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

_DISPLAY_TZ_NAME = os.getenv("DISPLAY_TIMEZONE", "America/Los_Angeles")


def display_tz() -> ZoneInfo:
    return ZoneInfo(_DISPLAY_TZ_NAME)


def pt_now() -> datetime:
    return datetime.now(display_tz())


def pt_today() -> date:
    return pt_now().date()


def pt_today_iso() -> str:
    return pt_today().isoformat()


def is_same_calendar_day_pt(iso_date: str) -> bool:
    """True if `iso_date` (YYYY-MM-DD) is today's date in Pacific."""
    try:
        d = date.fromisoformat(iso_date.strip()[:10])
    except ValueError:
        return False
    return d == pt_today()


def is_past_office_close_pt() -> bool:
    """True when local Pacific time is 6:00 PM or later (office closed)."""
    now = pt_now()
    return now.time() >= time(18, 0)


def office_hours_hint() -> str:
    return (
        "Office hours are 8:00 AM - 6:00 PM Pacific Time, Monday-Saturday "
        f"(timezone: {_DISPLAY_TZ_NAME})."
    )


def same_day_booking_closed_result() -> dict[str, Any]:
    """Structured tool-style error when same-day booking is not allowed (after close PT)."""
    return {
        "ok": False,
        "error": (
            "It's after 6:00 PM Pacific Time, so we're closed for today — there are no same-day slots left. "
            f"{office_hours_hint()} Would you like to choose another day?"
        ),
        "after_hours_pt": True,
    }
