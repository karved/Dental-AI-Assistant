"""LLM #1: intent + entities only (structured JSON). No user-facing prose."""

from __future__ import annotations

import json
import re
from typing import Any

from dental_assistant.domain.models import OrchestratorOutput
from dental_assistant.infrastructure.llm import call_llm

_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)


def _parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    m = _JSON_FENCE.search(text)
    if m:
        text = m.group(1).strip()
    return json.loads(text)


def run_orchestrator(user_message: str, conversation_summary: str = "") -> OrchestratorOutput:
    prompt = f"""You are an orchestrator for a dental practice chatbot.
Analyze the latest user message and recent context. Output a single JSON object only (no markdown, no explanation).
Schema:
{{"intent": string, "entities": object, "confidence": number or null}}
Use intent labels like: book_new, reschedule, cancel, family_book, emergency, faq, general, unknown.
entities may include: name, phone, email, date_preference, time_preference, appointment_id, faq_topic, family_size, notes.

Context (may be empty):
{conversation_summary}

User message:
{user_message}
"""
    raw = call_llm(prompt)
    try:
        data = _parse_json_object(raw)
        return OrchestratorOutput.model_validate(data)
    except (json.JSONDecodeError, ValueError):
        return OrchestratorOutput(intent="unknown", entities={}, confidence=None)
