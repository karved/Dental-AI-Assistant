"""Deterministic next-question selection. No LLM calls.

Field collection order per workflow:
  book_new:            name -> phone -> dob/insurance (skipped if on file) -> date_preference -> appointment_type
  reschedule:          phone -> date_preference
  cancel:              phone
  appointment_status:  phone (lookup by phone; no DOB required)
  family_book:         name -> phone -> family_size -> date_preference -> appointment_type
  emergency:           symptoms/notes -> phone -> (name if new to practice)
  faq:                 faq_topic
  general:             (open-ended prompt)
"""

from __future__ import annotations

from typing import Any

from dental_assistant.domain.constants import (
    EMERGENCY_QUESTION,
    FIELD_QUESTIONS,
    GENERAL_QUESTION,
    WORKFLOW_FIELDS,
)

_FAMILY_RELATIONSHIP_PLACEHOLDERS = frozenset({
    "wife", "husband", "spouse", "partner",
    "son", "daughter", "kid", "kids", "child", "children",
})


def _needs_specific_family_names(value: Any) -> bool:
    if not value:
        return True
    if isinstance(value, str):
        names = [value]
    elif isinstance(value, list):
        names = [str(v).strip().lower() for v in value if str(v).strip()]
    else:
        return True
    if not names:
        return True
    return all(name in _FAMILY_RELATIONSHIP_PLACEHOLDERS for name in names)


def max_questions_for_workflow(workflow: str, collected: dict[str, Any]) -> int:
    """Prefer one prompt at a time when both name and phone are missing (reduces partial replies)."""
    if workflow in ("book_new", "family_book"):
        if not collected.get("name") and not collected.get("phone"):
            return 1
    return 2


def select_questions(
    workflow: str,
    collected: dict[str, Any],
    max_questions: int = 2,
) -> list[str]:
    """Return up to `max_questions` prompts for missing fields, respecting collection order."""

    if workflow == "emergency":
        has_ctx = "symptoms" in collected or (str(collected.get("notes") or "").strip())
        if not has_ctx:
            return [EMERGENCY_QUESTION]
        if "phone" not in collected:
            return [FIELD_QUESTIONS["phone"]]
        return []

    if workflow in ("general", "unknown"):
        return [GENERAL_QUESTION] if not collected else []

    if workflow == "family_book":
        out: list[str] = []
        if "family_size" not in collected:
            out.append(FIELD_QUESTIONS["family_size"])
        elif _needs_specific_family_names(collected.get("family_member_names")):
            out.append(FIELD_QUESTIONS["family_member_names"])
        if "date_preference" not in collected:
            out.append(FIELD_QUESTIONS["date_preference"])
        if "appointment_type" not in collected:
            out.append(FIELD_QUESTIONS["appointment_type"])
        return out[:max_questions]

    required = WORKFLOW_FIELDS.get(workflow, [])
    out: list[str] = []
    for field in required:
        if field in collected:
            continue
        question = FIELD_QUESTIONS.get(field)
        if question:
            out.append(question)
        if len(out) >= max_questions:
            break

    return out
