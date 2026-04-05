"""Consecutive slot blocks for family visits."""

from __future__ import annotations

from typing import Any

from dental_assistant.domain.constants import (
    FAMILY_CONSECUTIVE_SLOT_SQL_LIMIT,
    SLOT_DURATION_MINUTES,
)
from dental_assistant.domain.time_parse import slot_time_prefix
from dental_assistant.infrastructure import queries as q


def _minutes_from_slot_time(time_str: str) -> int:
    h, m = map(int, time_str.split(":")[:2])
    return h * 60 + m


def _is_back_to_back(prev_time: str, curr_time: str) -> bool:
    return _minutes_from_slot_time(curr_time) - _minutes_from_slot_time(prev_time) == SLOT_DURATION_MINUTES


def find_consecutive_block_starting_at(
    conn: Any,
    date_iso: str,
    start_hhmm: str,
    count: int,
    rejected_ids: list[int],
) -> list[dict[str, Any]] | None:
    """Available `count` back-to-back slots on `date_iso` starting at `start_hhmm` (HH:MM)."""
    rows = [
        dict(r)
        for r in q.find_available_slots(
            conn, date_filter=date_iso, limit=FAMILY_CONSECUTIVE_SLOT_SQL_LIMIT,
        )
    ]
    rows = [r for r in rows if r["id"] not in rejected_ids]
    idx = next(
        (i for i, r in enumerate(rows) if slot_time_prefix(r["time"]) == start_hhmm),
        None,
    )
    if idx is None:
        return None
    group = rows[idx : idx + count]
    if len(group) < count:
        return None
    if len(set(s["date"] for s in group)) != 1:
        return None
    for j in range(1, len(group)):
        if not _is_back_to_back(group[j - 1]["time"], group[j]["time"]):
            return None
    return group
