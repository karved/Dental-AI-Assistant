"""Deterministic tool functions called by the engine. Never by LLMs directly.

Every public function returns a structured dict with an ``ok`` flag so the
engine can relay success/failure to the conversation agent without exceptions.

All SQL is delegated to the queries module (data access layer).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from dental_assistant.infrastructure import queries as q
from dental_assistant.infrastructure.db import load_faq

# ── result helpers ──────────────────────────────────────────────────────────

_Result = dict[str, Any]

_VALID_APPOINTMENT_TYPES = ("cleaning", "checkup", "emergency", "unknown")


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

# ── patients ────────────────────────────────────────────────────────────────

def lookup_patient(conn: sqlite3.Connection, phone: str) -> _Result:
    if not phone or not phone.strip():
        return _err("Phone number is required.")
    row = q.find_patient_by_phone(conn, phone.strip())
    if not row:
        return _err("No patient found with that phone number.", phone=phone)
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
    if not phone or not phone.strip():
        return _err("Phone number is required.")
    existing = q.find_patient_by_phone(conn, phone.strip())
    if existing:
        return _err("A patient with this phone number already exists.", patient_id=existing["id"])
    pid = q.insert_patient(conn, name.strip(), phone.strip(), dob, insurance)
    patient = q.find_patient_by_id(conn, pid)
    return _ok(patient=patient)

# ── availability ────────────────────────────────────────────────────────────

def check_availability(
    conn: sqlite3.Connection,
    date_filter: str | None = None,
    limit: int = 10,
) -> _Result:
    rows = q.find_available_slots(conn, date_filter=date_filter, limit=limit)
    if not rows:
        return _err("No available slots found." + (f" (date={date_filter})" if date_filter else ""))
    return _ok(slots=rows)

# ── appointments ────────────────────────────────────────────────────────────

def book_appointment(
    conn: sqlite3.Connection,
    patient_id: int,
    slot_id: int,
    appointment_type: str = "checkup",
    is_emergency: bool = False,
    emergency_summary: str | None = None,
) -> _Result:
    if not q.find_patient_by_id(conn, patient_id):
        return _err("Patient not found.", patient_id=patient_id)
    slot = q.find_slot_by_id(conn, slot_id)
    if not slot:
        return _err("Slot not found.", slot_id=slot_id)
    if not slot["is_available"]:
        return _err("That time slot is already booked.", slot_id=slot_id, date=slot["date"], time=slot["time"])
    if appointment_type not in _VALID_APPOINTMENT_TYPES:
        return _err(f"Invalid appointment type: {appointment_type}")
    conn.execute("SAVEPOINT book_appt")
    try:
        appt_id = q.insert_appointment(conn, patient_id, slot_id, appointment_type, is_emergency, emergency_summary)
        q.update_slot_availability(conn, slot_id, available=False)
        conn.execute("RELEASE SAVEPOINT book_appt")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT book_appt")
        raise
    appointment = q.find_appointment_with_slot(conn, appt_id)
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
    updated = q.find_appointment_with_slot(conn, appointment_id)
    return _ok(
        appointment=updated,
        old_date=current["date"],
        old_time=current["time"],
    )


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
    return _ok(
        appointment_id=appointment_id,
        date=current["date"],
        time=current["time"],
        appointment_type=current["appointment_type"],
    )


def get_patient_appointments(
    conn: sqlite3.Connection, patient_id: int, status: str = "confirmed"
) -> _Result:
    patient = q.find_patient_by_id(conn, patient_id)
    if not patient:
        return _err("Patient not found.", patient_id=patient_id)
    appointments = q.find_appointments_for_patient(conn, patient_id, status)
    return _ok(patient_name=patient["name"], appointments=appointments)

# ── office info (FAQ) ───────────────────────────────────────────────────────

def get_office_info(topic: str | None = None) -> _Result:
    faq = load_faq()
    if not faq:
        return _err("Office information is currently unavailable.")
    if topic:
        key = topic.lower().replace(" ", "_")
        entry = faq.get(key)
        if not entry:
            available = [v.get("title", k) for k, v in faq.items()]
            return _err(f"No info found for '{topic}'.", available_topics=available)
        return _ok(topic=key, title=entry.get("title", key), answer=entry["answer"])
    entries = {k: v.get("answer", "") for k, v in faq.items()}
    return _ok(topics=entries)

# ═══════════════════════════════════════════════════════════════════════════
# Low-level helpers used by the engine (conversations / messages / feedback)
# ═══════════════════════════════════════════════════════════════════════════

def create_conversation(conn: sqlite3.Connection) -> int:
    return q.insert_conversation(conn)


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
