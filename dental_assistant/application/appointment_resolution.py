"""Resolve which appointment the user means using full schedule + natural hints."""

from __future__ import annotations

import re
from typing import Any

from dental_assistant.domain.constants import VALID_APPOINTMENT_TYPES
from dental_assistant.domain.time_parse import slot_time_prefix
from dental_assistant.domain.utterances import infer_offered_list_ordinal


def _parse_appt_id(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _appointment_type_hint(fields: dict[str, Any], user_message: str) -> str | None:
    raw = fields.get("appointment_type")
    if isinstance(raw, str) and raw.strip().lower() in VALID_APPOINTMENT_TYPES:
        return raw.strip().lower()
    m = user_message.lower()
    if re.search(r"\b(emergency|urgent)\b", m):
        return "emergency"
    if re.search(r"\b(cleaning|prophy|prophylaxis)\b", m):
        return "cleaning"
    if re.search(r"\b(checkup|check-up|exam)\b", m):
        return "checkup"
    return None


def _date_hints_from_message_and_fields(message: str, fields: dict[str, Any]) -> list[str]:
    hints: list[str] = []
    dr = fields.get("date_resolved")
    if dr:
        hints.append(str(dr).strip()[:10])
    for m in re.finditer(r"\b(\d{4}-\d{2}-\d{2})\b", message):
        hints.append(m.group(1))
    return list(dict.fromkeys(hints))


def resolve_appointment_selection_full(
    appt_list: list[dict[str, Any]],
    fields: dict[str, Any],
    user_message: str,
    persisted_offered_ids: list[int],
    normalized_time_fn,
) -> tuple[int | None, str | None]:
    """Pick appointment id from full list. Returns (id, err) where err is 'not_found' or None."""
    if not appt_list:
        return None, None

    ids = [a["id"] for a in appt_list]
    id_set = set(ids)

    for key in ("selected_appointment_id", "appointment_id"):
        aid = _parse_appt_id(fields.get(key))
        if aid is not None and aid in id_set:
            return aid, None

    type_hint = _appointment_type_hint(fields, user_message)
    narrowed = [
        a
        for a in appt_list
        if not type_hint or str(a.get("appointment_type") or "").lower() == type_hint
    ]
    work = narrowed if narrowed else list(appt_list)
    work_ids = {a["id"] for a in work}

    if persisted_offered_ids:
        offered_set = set(persisted_offered_ids)
        po = [i for i in persisted_offered_ids if i in work_ids]
        if not po:
            po = list(persisted_offered_ids)
        aid = infer_offered_list_ordinal(user_message, po, fields, normalized_time_fn)
        if aid is not None and aid in offered_set and aid in id_set:
            return aid, None

    time_h = normalized_time_fn(fields, user_message)
    date_hints = _date_hints_from_message_and_fields(user_message, fields)
    user_specified = bool(time_h or date_hints)

    if not user_specified:
        return None, None

    matches = []
    for a in work:
        if date_hints and a["date"] not in date_hints:
            continue
        if time_h:
            if slot_time_prefix(a["time"]) != time_h:
                continue
        matches.append(a)

    if len(matches) == 1:
        return matches[0]["id"], None
    if len(matches) == 0 and user_specified:
        return None, "not_found"
    return None, None
