"""SQLite schema, connection, seed data, and FAQ loader."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Generator

from dental_assistant.domain.constants import SLOT_DURATION_MINUTES, VALID_APPOINTMENT_TYPES
from dental_assistant.settings import get_settings

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

@contextmanager
def connection(path: str | None = None) -> Generator[sqlite3.Connection, None, None]:
    db_path = path if path is not None else get_settings().database_path
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_APPOINTMENT_TYPE_CHECK = ", ".join(f"'{appt_type}'" for appt_type in VALID_APPOINTMENT_TYPES)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS patients (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    phone       TEXT    NOT NULL UNIQUE CHECK(length(replace(replace(replace(phone, '-', ''), '(', ''), ')', '')) = 10),
    dob         TEXT,
    insurance   TEXT,
    created_at  TEXT    DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_patients_phone ON patients(phone);

CREATE TABLE IF NOT EXISTS available_slots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    date             TEXT    NOT NULL,
    time             TEXT    NOT NULL,
    duration_minutes INTEGER NOT NULL DEFAULT {SLOT_DURATION_MINUTES},
    is_available     INTEGER NOT NULL DEFAULT 1,
    UNIQUE(date, time)
);

CREATE INDEX IF NOT EXISTS idx_slots_date ON available_slots(date);

CREATE TABLE IF NOT EXISTS appointments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id        INTEGER NOT NULL,
    slot_id           INTEGER,
    appointment_type  TEXT    NOT NULL DEFAULT 'checkup' CHECK(appointment_type IN ({_APPOINTMENT_TYPE_CHECK})),
    status            TEXT    NOT NULL DEFAULT 'confirmed' CHECK(status IN ('confirmed', 'cancelled')),
    is_emergency      INTEGER NOT NULL DEFAULT 0,
    emergency_summary TEXT,
    visit_notes       TEXT,
    created_at        TEXT    DEFAULT (datetime('now')),
    modified_at       TEXT    DEFAULT (datetime('now')),
    FOREIGN KEY (patient_id) REFERENCES patients(id),
    FOREIGN KEY (slot_id)    REFERENCES available_slots(id)
);

CREATE TABLE IF NOT EXISTS conversations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    role            TEXT    NOT NULL CHECK(role IN ('user', 'assistant')),
    content         TEXT    NOT NULL,
    metadata_json   TEXT,
    created_at      TEXT    DEFAULT (datetime('now')),
    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_id
ON messages(conversation_id, id);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_role_id
ON messages(conversation_id, role, id DESC);

CREATE TABLE IF NOT EXISTS feedback (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL UNIQUE,
    rating          INTEGER NOT NULL CHECK(rating IN (-1, 1)),
    created_at      TEXT    DEFAULT (datetime('now')),
    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
);
"""

# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

_SEED_PATIENTS = [
    ("Sarah Johnson",  "555-010-1001", "1990-03-15", "Delta Dental PPO"),
    ("Mike Chen",      "555-010-1002", "1985-08-22", "Cigna DHMO"),
    ("Emily Davis",    "555-010-1003", "1978-12-01", None),
    ("James Wilson",   "555-010-1004", "2000-06-10", "Aetna PPO"),
]

def _next_monday(today: date) -> date:
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def _generate_slot_rows(weeks: int = 8, *, start_date: date | None = None) -> list[tuple[str, str, int]]:
    """Mon–Sat, 8 AM – 6 PM in 30-min intervals for `weeks` weeks starting next Monday by default."""
    today = date.today()
    monday = start_date or _next_monday(today)
    rows: list[tuple[str, str, int]] = []
    for week in range(weeks):
        for day_offset in range(6):  # Mon–Sat
            d = monday + timedelta(weeks=week, days=day_offset)
            d_str = d.isoformat()
            for half_hour in range(20):  # 8:00 → 17:30  (20 slots)
                hour = 8 + half_hour // 2
                minute = 30 * (half_hour % 2)
                t_str = f"{hour:02d}:{minute:02d}"
                rows.append((d_str, t_str, 1))
    return rows


def _seed_existing_appointments(conn: sqlite3.Connection) -> None:
    """Book 3 sample appointments for seeded patients."""
    slots = conn.execute(
        "SELECT id, date, time FROM available_slots WHERE is_available = 1 ORDER BY date, time LIMIT 20"
    ).fetchall()
    if len(slots) < 12:
        return
    bookings = [
        (1, slots[2]["id"], "cleaning", "Six-month recall; prefers morning appointments."),
        (2, slots[5]["id"], "checkup", "Follow-up from prior exam; noted sensitivity lower left."),
        (3, slots[10]["id"], "cleaning", "Stain concern on anterior teeth; chart photos if needed."),
    ]
    for patient_id, slot_id, atype, vnotes in bookings:
        conn.execute(
            "INSERT INTO appointments (patient_id, slot_id, appointment_type, visit_notes) VALUES (?, ?, ?, ?)",
            (patient_id, slot_id, atype, vnotes),
        )
        conn.execute("UPDATE available_slots SET is_available = 0 WHERE id = ?", (slot_id,))


def seed_db(conn: sqlite3.Connection) -> None:
    """Insert seed data only if the tables are empty."""
    count = conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0]
    if count > 0:
        return

    conn.executemany(
        "INSERT INTO patients (name, phone, dob, insurance) VALUES (?, ?, ?, ?)",
        _SEED_PATIENTS,
    )
    conn.executemany(
        "INSERT INTO available_slots (date, time, is_available) VALUES (?, ?, ?)",
        _generate_slot_rows(weeks=8),
    )
    _seed_existing_appointments(conn)


def _ensure_slot_horizon(conn: sqlite3.Connection, minimum_weeks: int = 8) -> None:
    """Top up future availability so vague dates like 'next month' stay within the mock DB horizon."""
    row = conn.execute("SELECT MAX(date) AS max_date FROM available_slots").fetchone()
    max_date_raw = row["max_date"] if row else None
    start = _next_monday(date.today())
    target_max = start + timedelta(weeks=minimum_weeks, days=5)
    if not max_date_raw:
        rows = _generate_slot_rows(weeks=minimum_weeks, start_date=start)
    else:
        max_date = date.fromisoformat(str(max_date_raw))
        if max_date >= target_max:
            return
        next_start = max_date + timedelta(days=1)
        while next_start.weekday() != 0:
            next_start += timedelta(days=1)
        weeks = max(1, ((target_max - next_start).days // 7) + 1)
        rows = _generate_slot_rows(weeks=weeks, start_date=next_start)
    conn.executemany(
        "INSERT OR IGNORE INTO available_slots (date, time, is_available) VALUES (?, ?, ?)",
        rows,
    )

# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Apply additive migrations for existing SQLite files."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(appointments)").fetchall()}
    if not cols:
        return
    if "visit_notes" not in cols:
        conn.execute("ALTER TABLE appointments ADD COLUMN visit_notes TEXT")
    if "modified_at" not in cols:
        conn.execute("ALTER TABLE appointments ADD COLUMN modified_at TEXT")
        conn.execute(
            "UPDATE appointments SET modified_at = COALESCE(created_at, datetime('now')) "
            "WHERE modified_at IS NULL OR trim(modified_at) = ''"
        )


def init_db(path: str | None = None) -> None:
    with connection(path) as conn:
        conn.executescript(_SCHEMA)
        _migrate_schema(conn)
        seed_db(conn)
        _ensure_slot_horizon(conn)

# ---------------------------------------------------------------------------
# FAQ loader
# ---------------------------------------------------------------------------

def default_faq_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "faq.json"


def load_faq(path: str | Path | None = None) -> dict[str, Any]:
    if path is not None:
        faq_path = Path(path)
    else:
        configured = get_settings().faq_path
        faq_path = Path(configured) if configured else default_faq_path()
    try:
        with open(faq_path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
