"""Coerce LLM appointment_type values and build visit_summary for API/tool payloads.

visit_summary is computed here and in tools._appointment_with_visit_summary—it is not stored in SQLite;
only visit_notes (and type) are persisted.
"""

from __future__ import annotations

from typing import Any

from dental_assistant.domain.constants import VALID_APPOINTMENT_TYPES

_TYPE_LABELS: dict[str, str] = {
    "cleaning": "Cleaning",
    "checkup": "Checkup",
    "emergency": "Emergency visit",
    "unknown": "Scheduled visit",
}


def coerce_appointment_type(value: Any) -> str:
    if value is None:
        return "checkup"
    s = str(value).strip().lower()
    if not s:
        return "checkup"
    return s if s in VALID_APPOINTMENT_TYPES else "unknown"


def _type_label(appointment_type: str) -> str:
    return _TYPE_LABELS.get(appointment_type, "Scheduled visit")


def visit_summary_for_chat(appointment_type: str, visit_notes: str | None) -> str:
    label = _type_label(appointment_type or "unknown")
    if not visit_notes:
        return label
    t = " ".join(str(visit_notes).split()).strip()
    if not t:
        return label
    low = t.lower()
    if low.startswith("patient plans to use insurance that is not on file"):
        note = "Different insurance than on file — bring your card to check-in"
    elif len(t) <= 120:
        note = t
    else:
        cut = t[:120].rsplit(" ", 1)[0]
        note = f"{cut}…"
    return f"{label} — {note}"
