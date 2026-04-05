"""Deterministic next-question selection. No LLM calls."""

from __future__ import annotations

from dental_assistant.domain.models import OrchestratorOutput


def select_questions(
    orch: OrchestratorOutput,
    state: dict,
    max_questions: int = 2,
) -> list[str]:
    """
    Return up to `max_questions` short prompts to ask this turn, based on intent and filled slots in `state`.
    Keys in state are arbitrary; extend as the booking FSM grows.
    """
    intent = orch.intent
    entities = orch.entities or {}
    out: list[str] = []

    def need(key: str) -> bool:
        return key not in state and key not in entities

    if intent == "book_new":
        if need("full_name"):
            out.append("What name should we put the appointment under?")
        if need("phone") and len(out) < max_questions:
            out.append("What is the best phone number to reach you?")
        if need("date_preference") and len(out) < max_questions:
            out.append("What day or week works best for you?")
    elif intent in ("reschedule", "cancel"):
        if need("phone"):
            out.append("What phone number is on your chart so we can find your appointment?")
        elif need("appointment_hint") and len(out) < max_questions:
            out.append("Do you remember roughly when your current appointment is?")
    elif intent == "family_book":
        if need("family_size"):
            out.append("How many family members need visits?")
        elif need("date_preference") and len(out) < max_questions:
            out.append("What day works for back-to-back visits?")
    elif intent == "emergency":
        out.append("Are you in severe pain, swelling, or bleeding right now?")
    elif intent == "faq":
        topic = entities.get("faq_topic")
        if not topic:
            out.append("Are you asking about hours, location, insurance, or self-pay pricing?")
    elif intent == "general":
        out.append("What can I help you with today—booking, changing an appointment, or a quick question?")

    return out[:max_questions]
