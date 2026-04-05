"""Deterministic tool functions called by the engine. Never by LLMs directly.

Every public function returns a structured dict with an ``ok`` flag so the
engine can relay success/failure to the conversation agent without exceptions.

All SQL is delegated to the queries module (data access layer).
"""

from __future__ import annotations

import logging
import re
import sqlite3
from difflib import get_close_matches
from typing import Any

from dental_assistant.domain.appointments import visit_summary_for_chat
from dental_assistant.domain.constants import (
    PHONE_DIGIT_LENGTH,
    SLOT_SQL_FETCH_DEFAULT,
    VALID_APPOINTMENT_TYPES,
)
from dental_assistant.infrastructure import queries as q
from dental_assistant.infrastructure.db import load_faq

logger = logging.getLogger(__name__)

# ── result helpers ──────────────────────────────────────────────────────────

_Result = dict[str, Any]


def _appointment_with_visit_summary(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Attach visit_summary for the LLM/UI. Not a DB column—derived from appointment_type + visit_notes."""
    if row is None:
        return None
    d = dict(row)
    d["visit_summary"] = visit_summary_for_chat(
        str(d.get("appointment_type") or "unknown"),
        d.get("visit_notes"),
    )
    return d


def _ok(data: dict[str, Any] | None = None, **extra: Any) -> _Result:
    result: _Result = {"ok": True}
    if data:
        result.update(data)
    result.update(extra)
    return result


def _err(message: str, **extra: Any) -> _Result:
    result: _Result = {"ok": False, "error": message}
    result.update(extra)
    return result

# ═══════════════════════════════════════════════════════════════════════════
# Public tool API
# ═══════════════════════════════════════════════════════════════════════════

# ── phone normalization ─────────────────────────────────────────────────────

def normalize_phone(raw: str) -> tuple[str | None, str | None]:
    """Strip non-digits, validate length, format as XXX-XXX-XXXX.

    Returns (formatted_phone, error_message). Exactly one is None.
    """
    if not raw or not raw.strip():
        return None, "Phone number is required."
    digits = re.sub(r"\D", "", raw.strip())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != PHONE_DIGIT_LENGTH:
        return None, f"Phone number must be {PHONE_DIGIT_LENGTH} digits (got {len(digits)})."
    formatted = f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return formatted, None

# ── patients ────────────────────────────────────────────────────────────────

def lookup_patient(conn: sqlite3.Connection, phone: str) -> _Result:
    normalized, err = normalize_phone(phone)
    if err:
        return _err(err)
    row = q.find_patient_by_phone(conn, normalized)
    if not row:
        return _err("No patient found with that phone number.", phone=normalized)
    return _ok(patient=row)


def register_patient(
    conn: sqlite3.Connection,
    name: str,
    phone: str,
    dob: str | None = None,
    insurance: str | None = None,
) -> _Result:
    if not name or not name.strip():
        return _err("Patient name is required.")
    normalized, err = normalize_phone(phone)
    if err:
        return _err(err)
    existing = q.find_patient_by_phone(conn, normalized)
    if existing:
        return _err("A patient with this phone number already exists.", patient_id=existing["id"])
    pid = q.insert_patient(conn, name.strip(), normalized, dob, insurance)
    patient = q.find_patient_by_id(conn, pid)
    return _ok(patient=patient)

# ── availability ────────────────────────────────────────────────────────────

def check_availability(
    conn: sqlite3.Connection,
    date_filter: str | None = None,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = SLOT_SQL_FETCH_DEFAULT,
) -> _Result:
    rows = q.find_available_slots(conn, date_filter=date_filter, date_from=date_from, date_to=date_to, limit=limit)
    if not rows:
        hint = ""
        if date_filter:
            hint = f" (date={date_filter})"
        elif date_from or date_to:
            hint = f" (range={date_from or '*'}..{date_to or '*'})"
        return _err("No available slots found." + hint)
    return _ok(slots=rows)

# ── appointments ────────────────────────────────────────────────────────────

def book_appointment(
    conn: sqlite3.Connection,
    patient_id: int,
    slot_id: int,
    appointment_type: str = "checkup",
    is_emergency: bool = False,
    emergency_summary: str | None = None,
    visit_notes: str | None = None,
) -> _Result:
    if not q.find_patient_by_id(conn, patient_id):
        return _err("Patient not found.", patient_id=patient_id)
    slot = q.find_slot_by_id(conn, slot_id)
    if not slot:
        return _err("Slot not found.", slot_id=slot_id)
    if not slot["is_available"]:
        return _err("That time slot is already booked.", slot_id=slot_id, date=slot["date"], time=slot["time"])
    if appointment_type not in VALID_APPOINTMENT_TYPES:
        return _err(f"Invalid appointment type: {appointment_type}")
    es = emergency_summary if is_emergency else None
    conn.execute("SAVEPOINT book_appt")
    try:
        appt_id = q.insert_appointment(
            conn, patient_id, slot_id, appointment_type, is_emergency, es, visit_notes,
        )
        q.update_slot_availability(conn, slot_id, available=False)
        conn.execute("RELEASE SAVEPOINT book_appt")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT book_appt")
        raise
    appointment = _appointment_with_visit_summary(q.find_appointment_with_slot(conn, appt_id))
    return _ok(appointment=appointment)


def reschedule_appointment(
    conn: sqlite3.Connection, appointment_id: int, new_slot_id: int
) -> _Result:
    current = q.find_active_appointment(conn, appointment_id)
    if not current:
        return _err("No active appointment found with that ID.", appointment_id=appointment_id)
    new_slot = q.find_slot_by_id(conn, new_slot_id)
    if not new_slot:
        return _err("New slot not found.", slot_id=new_slot_id)
    if not new_slot["is_available"]:
        return _err("New time slot is already booked.", slot_id=new_slot_id, date=new_slot["date"], time=new_slot["time"])
    conn.execute("SAVEPOINT reschedule_appt")
    try:
        q.update_slot_availability(conn, current["slot_id"], available=True)
        q.update_appointment_slot(conn, appointment_id, new_slot_id)
        q.update_slot_availability(conn, new_slot_id, available=False)
        conn.execute("RELEASE SAVEPOINT reschedule_appt")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT reschedule_appt")
        raise
    updated = _appointment_with_visit_summary(q.find_appointment_with_slot(conn, appointment_id))
    patient = q.find_patient_by_id(conn, current["patient_id"])
    out: dict[str, Any] = {
        "appointment": updated,
        "old_date": current["date"],
        "old_time": current["time"],
    }
    if patient:
        out["patient_name"] = patient.get("name")
        ins = patient.get("insurance")
        if ins and str(ins).strip():
            out["insurance_on_file"] = ins
    return _ok(**out)


def cancel_appointment(conn: sqlite3.Connection, appointment_id: int) -> _Result:
    current = q.find_active_appointment(conn, appointment_id)
    if not current:
        return _err("No active appointment found with that ID.", appointment_id=appointment_id)
    conn.execute("SAVEPOINT cancel_appt")
    try:
        q.update_appointment_status(conn, appointment_id, "cancelled")
        q.update_slot_availability(conn, current["slot_id"], available=True)
        conn.execute("RELEASE SAVEPOINT cancel_appt")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT cancel_appt")
        raise
    patient = q.find_patient_by_id(conn, current["patient_id"])
    out: dict[str, Any] = {
        "appointment_id": appointment_id,
        "date": current["date"],
        "time": current["time"],
        "appointment_type": current["appointment_type"],
    }
    if patient:
        out["patient_name"] = patient.get("name")
        ins = patient.get("insurance")
        if ins and str(ins).strip():
            out["insurance_on_file"] = ins
    return _ok(**out)


def get_patient_appointments(
    conn: sqlite3.Connection, patient_id: int, status: str = "confirmed"
) -> _Result:
    patient = q.find_patient_by_id(conn, patient_id)
    if not patient:
        return _err("Patient not found.", patient_id=patient_id)
    raw = q.find_appointments_for_patient(conn, patient_id, status)
    appointments = [_appointment_with_visit_summary(dict(a)) for a in raw]
    out: dict[str, Any] = {"patient_name": patient["name"], "appointments": appointments}
    ins = patient.get("insurance")
    if ins and str(ins).strip():
        out["insurance_on_file"] = ins
    return _ok(**out)

# ── office info (FAQ) ───────────────────────────────────────────────────────
#
# Tiered resolution strategy (deterministic first, LLM fallback optional):
#   Tier 1a: exact key lookup             (free, instant)
#   Tier 1b: substring + prefix-stem match (free, instant)
#   Tier 1c: difflib fuzzy match on keys  (free, instant)
#   Tier 2:  LLM fallback                 (costs tokens, only when tiers 1a-1c fail)


def _faq_keyword_match(faq: dict[str, Any], query: str) -> dict[str, Any] | None:
    """Tier 1b: substring, explicit keywords, and prefix-stem matching against FAQ text."""
    query_lower = query.lower().strip()

    for key, entry in faq.items():
        explicit_keywords = [k.lower() for k in entry.get("keywords", [])]
        if query_lower in explicit_keywords:
            return {"key": key, **entry}

    for key, entry in faq.items():
        blob = " ".join([key, entry.get("title", ""), entry.get("answer", "")] + entry.get("keywords", [])).lower()
        if query_lower in blob:
            return {"key": key, **entry}

    query_words = [w for w in query_lower.split() if len(w) > 2]
    for key, entry in faq.items():
        blob = " ".join([key, entry.get("title", ""), entry.get("answer", "")] + entry.get("keywords", [])).lower()
        blob_words = blob.split()
        for qw in query_words:
            prefix = qw[:4] if len(qw) >= 4 else qw
            if any(bw.startswith(prefix) for bw in blob_words):
                return {"key": key, **entry}
    return None


def _faq_fuzzy_match(faq: dict[str, Any], query: str) -> dict[str, Any] | None:
    """Tier 1c: fuzzy match query against FAQ keys using difflib."""
    keys = list(faq.keys())
    matches = get_close_matches(query.lower().strip(), keys, n=1, cutoff=0.5)
    if matches:
        entry = faq[matches[0]]
        return {"key": matches[0], **entry}
    return None


def _faq_llm_fallback(topic: str, faq: dict[str, Any]) -> _Result | None:
    """Tier 2: optional LLM fallback. Only called when deterministic tiers fail."""
    try:
        from dental_assistant.infrastructure.llm import call_llm
        from dental_assistant.settings import get_settings

        if not get_settings().llm_ready:
            return None

        faq_text = "\n".join(
            f"- {entry.get('title', key)}: {entry.get('answer', '')}"
            for key, entry in faq.items()
        )
        prompt = (
            "You are a dental office assistant. A patient asked about a topic "
            "that didn't match our FAQ exactly. Based on the FAQ below, give a "
            "short 1-2 sentence answer. If the FAQ doesn't cover it, say so.\n\n"
            f"FAQ:\n{faq_text}\n\n"
            f"Patient asked about: {topic}"
        )
        answer = call_llm(prompt).strip()
        return _ok(topic="llm_fallback", title="General inquiry", answer=answer, match_tier="llm")
    except Exception:
        logger.debug("FAQ LLM fallback failed for topic=%s", topic, exc_info=True)
        return None


def get_office_info(topic: str | None = None) -> _Result:
    faq = load_faq()
    if not faq:
        return _err("Office information is currently unavailable.")

    if not topic:
        entries = {k: v.get("answer", "") for k, v in faq.items()}
        return _ok(topics=entries)

    key = topic.lower().replace(" ", "_")
    entry = faq.get(key)
    if entry:
        return _ok(topic=key, title=entry.get("title", key), answer=entry["answer"], match_tier="exact")

    match = _faq_keyword_match(faq, topic)
    if match:
        return _ok(topic=match["key"], title=match.get("title", match["key"]), answer=match["answer"], match_tier="keyword")

    match = _faq_fuzzy_match(faq, topic)
    if match:
        return _ok(topic=match["key"], title=match.get("title", match["key"]), answer=match["answer"], match_tier="fuzzy")

    llm_result = _faq_llm_fallback(topic, faq)
    if llm_result:
        return llm_result

    available = [v.get("title", k) for k, v in faq.items()]
    return _err(f"No info found for '{topic}'.", available_topics=available)

# ═══════════════════════════════════════════════════════════════════════════
# Low-level helpers used by the engine (conversations / messages / feedback)
# ═══════════════════════════════════════════════════════════════════════════

def save_message(
    conn: sqlite3.Connection,
    conversation_id: int,
    role: str,
    content: str,
    metadata_json: str | None = None,
) -> int:
    return q.insert_message(conn, conversation_id, role, content, metadata_json)


def save_feedback(conn: sqlite3.Connection, conversation_id: int, rating: int) -> _Result:
    if rating not in (-1, 1):
        return _err("Rating must be -1 or 1.")
    if not q.find_conversation_by_id(conn, conversation_id):
        return _err("Conversation not found.", conversation_id=conversation_id)
    q.upsert_feedback(conn, conversation_id, rating)
    return _ok(conversation_id=conversation_id, rating=rating)
