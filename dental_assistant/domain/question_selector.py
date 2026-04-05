"""Deterministic next-question selection. No LLM calls.

Field collection order per workflow:
  book_new:     name -> phone -> date_preference -> appointment_type
  reschedule:   phone -> date_preference
  cancel:       phone
  family_book:  name -> phone -> family_size -> date_preference
  emergency:    (symptoms clarification)
  faq:          faq_topic
  general:      (open-ended prompt)
"""

from __future__ import annotations

from typing import Any

from dental_assistant.domain.constants import (
    EMERGENCY_QUESTION,
    FIELD_QUESTIONS,
    GENERAL_QUESTION,
    WORKFLOW_FIELDS,
)


def select_questions(
    workflow: str,
    collected: dict[str, Any],
    max_questions: int = 2,
) -> list[str]:
    """Return up to `max_questions` prompts for missing fields, respecting collection order."""

    if workflow == "emergency":
        if "symptoms" not in collected:
            return [EMERGENCY_QUESTION]
        return []

    if workflow in ("general", "unknown"):
        return [GENERAL_QUESTION] if not collected else []

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
