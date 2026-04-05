"""Centralised constants for the dental assistant.

Single source of truth for domain enums, safety rules, readiness criteria,
question templates, and validation sets. Imported by engine, question_selector,
tools, and date_resolver — never duplicated.
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

# ---------------------------------------------------------------------------
# Readiness rules — deterministic Python layer decides when tools can fire
# ---------------------------------------------------------------------------

READINESS_RULES: dict[str, set[str]] = {
    "book_new":    {"name", "phone", "date_preference"},
    "reschedule":  {"phone"},
    "cancel":      {"phone"},
    "family_book": {"name", "phone", "family_size", "date_preference"},
    "emergency":   set(),
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
    "faq_topic":        "Are you asking about our hours, location, insurance, or pricing?",
}

WORKFLOW_FIELDS: dict[str, list[str]] = {
    "book_new":     ["name", "phone", "date_preference", "appointment_type"],
    "reschedule":   ["phone", "date_preference"],
    "cancel":       ["phone"],
    "family_book":  ["name", "phone", "family_size", "date_preference"],
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
