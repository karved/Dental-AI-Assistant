"""Centralised constants for the dental assistant.

Single source of truth for domain enums, safety rules, readiness criteria,
question templates, validation sets, orchestrator context limits, and
availability SQL/pipeline caps. Imported by engine, queries, tools,
question_selector, and date_resolver — avoid duplicating magic numbers.
Shared phrase lists and ordinal parsing live in ``utterances.py``.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Safety layer
# ---------------------------------------------------------------------------

BLOCKED_PHRASES: tuple[str, ...] = (
    "suicide", "kill myself", "self-harm", "self harm",
    "bomb", "gun", "shoot", "murder", "attack",
    "hurt myself", "end my life",
)

SAFETY_RESPONSE: str = (
    "I'm not able to help with that. If you're in crisis, "
    "please contact local emergency services or call 988 (Suicide & Crisis Lifeline)."
)

# ---------------------------------------------------------------------------
# Appointment types (DB CHECK constraint must match)
# ---------------------------------------------------------------------------

VALID_APPOINTMENT_TYPES: tuple[str, ...] = ("cleaning", "checkup", "emergency", "unknown")

# Orchestrator intents that imply a routine (non-emergency) task — clears a stuck is_emergency flag
# from a prior completed emergency in the same conversation.
ORCHESTRATOR_INTENTS_ROUTINE: frozenset[str] = frozenset({
    "book_new",
    "family_book",
    "reschedule",
    "cancel",
    "appointment_status",
    "faq",
})

# ---------------------------------------------------------------------------
# Readiness rules — deterministic Python layer decides when tools can fire
# ---------------------------------------------------------------------------

READINESS_RULES: dict[str, set[str]] = {
    "book_new":    {"name", "phone", "date_preference"},
    "reschedule":  {"phone"},
    "cancel":      {"phone"},
    "appointment_status": {"phone"},
    "family_book": {"name", "phone", "family_size", "date_preference", "appointment_type"},
    "emergency":   {"phone"},
    "faq":         set(),
}

# ---------------------------------------------------------------------------
# Question selector — field ordering per workflow
# ---------------------------------------------------------------------------

FIELD_QUESTIONS: dict[str, str] = {
    "name":             "What name should we put the appointment under?",
    "phone":            "What's the best phone number to reach you?",
    "dob":              "What is your date of birth?",
    "insurance":        "Do you have dental insurance? If so, which provider?",
    "date_preference":  "What day or week works best for you?",
    "time_preference":  "Do you have a preferred time of day -- morning or afternoon?",
    "appointment_type": "What type of visit do you need -- a cleaning, checkup, or something else?",
    "family_size":      "How many family members need appointments?",
    "family_member_names": "What are the names of the family members who need appointments?",
    "faq_topic":        "Are you asking about our hours, location, insurance, or pricing?",
    "slot_choice":      "Which of the available times would you like? You can say the time (e.g. 2 PM) or pick from the list.",
    "appointment_choice": (
        "Which appointment do you mean? You can give the appointment ID from the list, say first or second, "
        "or describe the date and time (all times are US Pacific)."
    ),
    "identity_confirm": "Please confirm whether the name on file matches you (yes or no).",
}

WORKFLOW_FIELDS: dict[str, list[str]] = {
    "book_new":     ["name", "phone", "dob", "insurance", "date_preference", "appointment_type"],
    "reschedule":   ["phone", "date_preference"],
    "cancel":       ["phone"],
    "appointment_status": ["phone"],
    "family_book":  ["name", "phone", "family_size", "family_member_names", "date_preference", "appointment_type"],
    "emergency":    [],
    "faq":          ["faq_topic"],
    "general":      [],
    "unknown":      [],
}

EMERGENCY_QUESTION: str = (
    "Can you describe what's happening -- are you in severe pain, swelling, or bleeding?"
)

GENERAL_QUESTION: str = (
    "What can I help you with today -- booking, changing an appointment, or a quick question?"
)

# ---------------------------------------------------------------------------
# Date resolver — weekday name -> weekday index
# ---------------------------------------------------------------------------

WEEKDAYS: dict[str, int] = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

# ---------------------------------------------------------------------------
# Slot duration (minutes) — single source for DB default and logic
# ---------------------------------------------------------------------------

SLOT_DURATION_MINUTES: int = 30

# ---------------------------------------------------------------------------
# Phone validation
# ---------------------------------------------------------------------------

PHONE_DIGIT_LENGTH: int = 10

# ---------------------------------------------------------------------------
# Conversation / LLM #1 (orchestrator) context
#
# Tune here instead of hunting literals in engine.py / queries.py.
# ---------------------------------------------------------------------------

ORCHESTRATOR_RECENT_MESSAGES: int = 4
"""Latest prior user/assistant *text* lines (role: content) passed to the orchestrator."""

ORCHESTRATOR_PRIOR_STATE_KEYS: frozenset[str] = frozenset({
    "name",
    "phone",
    "dob",
    "insurance",
    "identity_verified",
    "date_preference",
    "date_resolved",
    "time_preference",
    "appointment_type",
    "selected_time",
    "selected_slot_id",
    "selected_appointment_id",
    "symptoms",
    "notes",
    "faq_topic",
    "family_size",
    "pending_family_size",
    "use_different_insurance",
})
"""Prior-turn collected_fields subset echoed to the orchestrator (truth from Python, not prose)."""

# ---------------------------------------------------------------------------
# Availability: SQL fetch caps vs in-memory pipeline caps
#
# find_available_slots uses ORDER BY date, time LIMIT N. For one calendar day,
# N must cover the whole office schedule (e.g. 8:00–17:30 half-hours) or PM
# slots never enter Python. Multi-day “browse” queries can use smaller N.
# ---------------------------------------------------------------------------

SLOT_SQL_FETCH_CAP_MIN_SINGLE_DAY: int = 48
"""Minimum SQL LIMIT when the filter resolves to one YYYY-MM-DD day."""

SLOT_PIPELINE_RETURN_CAP_SINGLE_DAY: int = 32
"""Max rows kept after time-preference filtering for a single known day (≥ slots/day)."""

MAX_SLOTS_OFFERED_TO_USER: int = 5
"""Slots listed in one assistant turn before explicit selection."""

AVAILABILITY_PIPELINE_DEFAULT_LIMIT: int = 15
"""Default max slots after filters for book_new / reschedule (raised for single-day)."""

EMERGENCY_AVAILABILITY_SQL_LIMIT: int = 48
"""Wide fetch before dropping same-day rows after office close (PT)."""

FAMILY_CONSECUTIVE_SLOT_SQL_LIMIT: int = 80
"""Headroom for finding back-to-back blocks on one calendar day."""

SLOT_SQL_FETCH_DEFAULT: int = 10
"""Default SQL LIMIT when no single-day filter (first N rows by date, time)."""
