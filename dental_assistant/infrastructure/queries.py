"""Pure data-access layer. Every SQL statement lives here.

Functions accept a sqlite3.Connection and return raw dicts / primitives.
No business logic, no validation, no result formatting.
"""

from __future__ import annotations

import sqlite3
from typing import Any

_Row = dict[str, Any]

# ---------------------------------------------------------------------------
# Patients
# ---------------------------------------------------------------------------

def find_patient_by_phone(conn: sqlite3.Connection, phone: str) -> _Row | None:
    row = conn.execute("SELECT * FROM patients WHERE phone = ?", (phone,)).fetchone()
    return dict(row) if row else None


def find_patient_by_id(conn: sqlite3.Connection, patient_id: int) -> _Row | None:
    row = conn.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
    return dict(row) if row else None


def insert_patient(conn: sqlite3.Connection, name: str, phone: str, dob: str | None, insurance: str | None) -> int:
    cur = conn.execute(
        "INSERT INTO patients (name, phone, dob, insurance) VALUES (?, ?, ?, ?)",
        (name, phone, dob, insurance),
    )
    return int(cur.lastrowid)

# ---------------------------------------------------------------------------
# Slots
# ---------------------------------------------------------------------------

def find_available_slots(conn: sqlite3.Connection, date_filter: str | None = None, limit: int = 10) -> list[_Row]:
    sql = "SELECT id, date, time, duration_minutes FROM available_slots WHERE is_available = 1"
    params: list[Any] = []
    if date_filter:
        sql += " AND date = ?"
        params.append(date_filter)
    sql += " ORDER BY date, time LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def find_slot_by_id(conn: sqlite3.Connection, slot_id: int) -> _Row | None:
    row = conn.execute("SELECT * FROM available_slots WHERE id = ?", (slot_id,)).fetchone()
    return dict(row) if row else None


def update_slot_availability(conn: sqlite3.Connection, slot_id: int, available: bool) -> None:
    conn.execute("UPDATE available_slots SET is_available = ? WHERE id = ?", (int(available), slot_id))

# ---------------------------------------------------------------------------
# Appointments
# ---------------------------------------------------------------------------

def insert_appointment(
    conn: sqlite3.Connection,
    patient_id: int,
    slot_id: int,
    appointment_type: str,
    is_emergency: bool,
    emergency_summary: str | None,
) -> int:
    cur = conn.execute(
        """INSERT INTO appointments
           (patient_id, slot_id, appointment_type, is_emergency, emergency_summary)
           VALUES (?, ?, ?, ?, ?)""",
        (patient_id, slot_id, appointment_type, int(is_emergency), emergency_summary),
    )
    return int(cur.lastrowid)


def find_appointment_with_slot(conn: sqlite3.Connection, appointment_id: int) -> _Row | None:
    row = conn.execute(
        "SELECT a.*, s.date, s.time "
        "FROM appointments a JOIN available_slots s ON a.slot_id = s.id "
        "WHERE a.id = ?",
        (appointment_id,),
    ).fetchone()
    return dict(row) if row else None


def find_active_appointment(conn: sqlite3.Connection, appointment_id: int) -> _Row | None:
    row = conn.execute(
        "SELECT a.*, s.date, s.time "
        "FROM appointments a JOIN available_slots s ON a.slot_id = s.id "
        "WHERE a.id = ? AND a.status = 'confirmed'",
        (appointment_id,),
    ).fetchone()
    return dict(row) if row else None


def find_appointments_for_patient(conn: sqlite3.Connection, patient_id: int, status: str = "confirmed") -> list[_Row]:
    rows = conn.execute(
        """SELECT a.id, a.appointment_type, a.status, a.is_emergency, s.date, s.time
           FROM appointments a
           JOIN available_slots s ON a.slot_id = s.id
           WHERE a.patient_id = ? AND a.status = ?
           ORDER BY s.date, s.time""",
        (patient_id, status),
    ).fetchall()
    return [dict(r) for r in rows]


def update_appointment_status(conn: sqlite3.Connection, appointment_id: int, status: str) -> None:
    conn.execute("UPDATE appointments SET status = ? WHERE id = ?", (status, appointment_id))


def update_appointment_slot(conn: sqlite3.Connection, appointment_id: int, new_slot_id: int) -> None:
    conn.execute("UPDATE appointments SET slot_id = ? WHERE id = ?", (new_slot_id, appointment_id))

# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

def insert_conversation(conn: sqlite3.Connection) -> int:
    cur = conn.execute("INSERT INTO conversations DEFAULT VALUES")
    return int(cur.lastrowid)


def find_conversation_by_id(conn: sqlite3.Connection, conversation_id: int) -> _Row | None:
    row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    return dict(row) if row else None

# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def insert_message(
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


def find_latest_assistant_metadata(conn: sqlite3.Connection, conversation_id: int) -> str | None:
    row = conn.execute(
        "SELECT metadata_json FROM messages "
        "WHERE conversation_id = ? AND role = 'assistant' AND metadata_json IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (conversation_id,),
    ).fetchone()
    return row["metadata_json"] if row else None


def find_recent_messages(conn: sqlite3.Connection, conversation_id: int, limit: int = 6) -> list[_Row]:
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
        (conversation_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]

# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------

def upsert_feedback(conn: sqlite3.Connection, conversation_id: int, rating: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO feedback (conversation_id, rating) VALUES (?, ?)",
        (conversation_id, rating),
    )
