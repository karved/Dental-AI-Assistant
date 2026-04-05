"""Shared time parsing for slots and appointment matching."""

from __future__ import annotations

import re
from typing import Any

_TIME_24 = re.compile(r"^(\d{1,2}):(\d{2})$")


def normalize_time_token(text: str) -> str | None:
    """Return 'HH:MM' comparable to slot['time'], or None."""
    t = text.strip().lower().replace(" ", "")
    m = _TIME_24.match(t)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}:{mi:02d}"
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?(am|pm)$", t)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2) or 0)
        ap = m.group(3)
        if ap == "pm" and h != 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        if 0 <= h <= 23:
            return f"{h:02d}:{mi:02d}"
    return None


def slot_time_prefix(slot_time: str) -> str:
    parts = slot_time.split(":")
    return f"{int(parts[0]):02d}:{int(parts[1]):02d}"


_TIME_WITH_AMPM = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.IGNORECASE)


def normalized_time_from_fields_or_message(fields: dict[str, Any], user_message: str) -> str | None:
    st = fields.get("selected_time")
    if st:
        raw = str(st).strip().lower().replace(" ", "")
        n = normalize_time_token(raw)
        if n:
            return n
        n = normalize_time_token(str(st).strip())
        if n:
            return n
    m = _TIME_WITH_AMPM.search(user_message)
    if m:
        frag = m.group(0).lower().replace(" ", "")
        n = normalize_time_token(frag)
        if n:
            return n
    for token in user_message.replace(",", " ").split():
        n = normalize_time_token(token)
        if n:
            return n
    return normalize_time_token(user_message.strip().lower().replace(" ", ""))
