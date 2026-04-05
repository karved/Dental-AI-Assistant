"""Deterministic tool functions called by the engine. Never by LLMs directly.

Every public function returns a structured dict with an ``ok`` flag so the
engine can relay success/failure to the conversation agent without exceptions.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from dental_assistant.infrastructure.db import load_faq

# ── result helpers ──────────────────────────────────────────────────────────

_Result = dict[str, Any]


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

# ── slot helpers (internal) ─────────────────────────────────────────────────

def _mark_slot_unavailable(conn: sqlite3.Connection, slot_id: int) -> None:
    conn.execute("UPDATE available_slots SET is_available = 0 WHERE id = ?", (slot_id,))


def _mark_slot_available(conn: sqlite3.Connection, slot_id: int) -> None:
    conn.execute("UPDATE available_slots SET is_available = 1 WHERE id = ?", (slot_id,))

# ═══════════════════════════════════════════════════════════════════════════
# Public tool API — the engine calls these
# ═══════════════════════════════════════════════════════════════════════════

# ── patients ────────────────────────────────────────────────────────────────

def lookup_patient(conn: sqlite3.Connection, phone: str) -> _Result:
    if not phone or not phone.strip():
        return _err("Phone number is required.")
    row = conn.execute("SELECT * FROM patients WHERE phone = ?", (phone.strip(),)).fetchone()
    if not row:
        return _err("No patient found with that phone number.", phone=phone)
    return _ok(patient=dict(row))


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
    existing = conn.execute("SELECT id FROM patients WHERE phone = ?", (phone.strip(),)).fetchone()
    if existing:
        return _err("A patient with this phone number already exists.", patient_id=existing["id"])
    cur = conn.execute(
        "INSERT INTO patients (name, phone, dob, insurance) VALUES (?, ?, ?, ?)",
        (name.strip(), phone.strip(), dob, insurance),
    )
    patient = dict(conn.execute("SELECT * FROM patients WHERE id = ?", (cur.lastrowid,)).fetchone())
    return _ok(patient=patient)

# ── availability ────────────────────────────────────────────────────────────

def check_availability(
    conn: sqlite3.Connection,
    date_filter: str | None = None,
    limit: int = 10,
) -> _Result:
    sql = "SELECT id, date, time, duration_minutes FROM available_slots WHERE is_available = 1"
    params: list[Any] = []
    if date_filter:
        sql += " AND date = ?"
        params.append(date_filter)
    sql += " ORDER BY date, time LIMIT ?"
    params.append(limit)
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
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
    patient = conn.execute("SELECT id FROM patients WHERE id = ?", (patient_id,)).fetchone()
    if not patient:
        return _err("Patient not found.", patient_id=patient_id)
    slot = conn.execute(
        "SELECT id, is_available, date, time FROM available_slots WHERE id = ?", (slot_id,)
    ).fetchone()
    if not slot:
        return _err("Slot not found.", slot_id=slot_id)
    if not slot["is_available"]:
        return _err("That time slot is already booked.", slot_id=slot_id, date=slot["date"], time=slot["time"])
    if appointment_type not in ("cleaning", "checkup", "emergency", "unknown"):
        return _err(f"Invalid appointment type: {appointment_type}")
    conn.execute("SAVEPOINT book_appt")
    try:
        cur = conn.execute(
            """INSERT INTO appointments
               (patient_id, slot_id, appointment_type, is_emergency, emergency_summary)
               VALUES (?, ?, ?, ?, ?)""",
            (patient_id, slot_id, appointment_type, int(is_emergency), emergency_summary),
        )
        _mark_slot_unavailable(conn, slot_id)
        conn.execute("RELEASE SAVEPOINT book_appt")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT book_appt")
        raise
    appointment = dict(conn.execute(
        "SELECT a.*, s.date, s.time FROM appointments a "
        "JOIN available_slots s ON a.slot_id = s.id WHERE a.id = ?",
        (cur.lastrowid,),
    ).fetchone())
    return _ok(appointment=appointment)


def reschedule_appointment(
    conn: sqlite3.Connection, appointment_id: int, new_slot_id: int
) -> _Result:
    row = conn.execute(
        "SELECT a.slot_id, s.date, s.time FROM appointments a "
        "JOIN available_slots s ON a.slot_id = s.id "
        "WHERE a.id = ? AND a.status = 'confirmed'",
        (appointment_id,),
    ).fetchone()
    if not row:
        return _err("No active appointment found with that ID.", appointment_id=appointment_id)
    new_slot = conn.execute(
        "SELECT id, is_available, date, time FROM available_slots WHERE id = ?", (new_slot_id,)
    ).fetchone()
    if not new_slot:
        return _err("New slot not found.", slot_id=new_slot_id)
    if not new_slot["is_available"]:
        return _err("New time slot is already booked.", slot_id=new_slot_id, date=new_slot["date"], time=new_slot["time"])
    conn.execute("SAVEPOINT reschedule_appt")
    try:
        _mark_slot_available(conn, row["slot_id"])
        conn.execute("UPDATE appointments SET slot_id = ? WHERE id = ?", (new_slot_id, appointment_id))
        _mark_slot_unavailable(conn, new_slot_id)
        conn.execute("RELEASE SAVEPOINT reschedule_appt")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT reschedule_appt")
        raise
    updated = dict(conn.execute(
        "SELECT a.*, s.date, s.time FROM appointments a "
        "JOIN available_slots s ON a.slot_id = s.id WHERE a.id = ?",
        (appointment_id,),
    ).fetchone())
    return _ok(
        appointment=updated,
        old_date=row["date"],
        old_time=row["time"],
    )


def cancel_appointment(conn: sqlite3.Connection, appointment_id: int) -> _Result:
    row = conn.execute(
        "SELECT a.id, a.slot_id, s.date, s.time, a.appointment_type "
        "FROM appointments a JOIN available_slots s ON a.slot_id = s.id "
        "WHERE a.id = ? AND a.status = 'confirmed'",
        (appointment_id,),
    ).fetchone()
    if not row:
        return _err("No active appointment found with that ID.", appointment_id=appointment_id)
    conn.execute("SAVEPOINT cancel_appt")
    try:
        conn.execute("UPDATE appointments SET status = 'cancelled' WHERE id = ?", (appointment_id,))
        _mark_slot_available(conn, row["slot_id"])
        conn.execute("RELEASE SAVEPOINT cancel_appt")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT cancel_appt")
        raise
    return _ok(
        appointment_id=appointment_id,
        date=row["date"],
        time=row["time"],
        appointment_type=row["appointment_type"],
    )


def get_patient_appointments(
    conn: sqlite3.Connection, patient_id: int, status: str = "confirmed"
) -> _Result:
    patient = conn.execute("SELECT id, name FROM patients WHERE id = ?", (patient_id,)).fetchone()
    if not patient:
        return _err("Patient not found.", patient_id=patient_id)
    rows = conn.execute(
        """SELECT a.id, a.appointment_type, a.status, a.is_emergency, s.date, s.time
           FROM appointments a
           JOIN available_slots s ON a.slot_id = s.id
           WHERE a.patient_id = ? AND a.status = ?
           ORDER BY s.date, s.time""",
        (patient_id, status),
    ).fetchall()
    appointments = [dict(r) for r in rows]
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
# Low-level helpers used by the engine directly (conversations/messages)
# ═══════════════════════════════════════════════════════════════════════════

def create_conversation(conn: sqlite3.Connection) -> int:
    cur = conn.execute("INSERT INTO conversations DEFAULT VALUES")
    return int(cur.lastrowid)


def save_message(
    conn: sqlite3.Connection,
    conversation_id: int,
    role: str,
    content: str,
    metadata_json: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO messages (conversation_id, role, content, metadata_json) VALUES (?, ?, ?, ?)",
        (conversation_id, role, content, metadata_json),
    )
    return int(cur.lastrowid)


def save_feedback(conn: sqlite3.Connection, conversation_id: int, rating: int) -> _Result:
    if rating not in (-1, 1):
        return _err("Rating must be -1 or 1.")
    row = conn.execute("SELECT id FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    if not row:
        return _err("Conversation not found.", conversation_id=conversation_id)
    conn.execute(
        "INSERT OR REPLACE INTO feedback (conversation_id, rating) VALUES (?, ?)",
        (conversation_id, rating),
    )
    return _ok(conversation_id=conversation_id, rating=rating)
