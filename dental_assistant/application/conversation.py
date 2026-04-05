"""LLM #2: natural language generation from structured state only.

This module does NOT make routing decisions, call tools, or modify state.
It receives a ConversationAgentInput and returns a human-readable reply.
"""

from __future__ import annotations

import json

from dental_assistant.domain.models import ConversationAgentInput
from dental_assistant.domain.prompts import CONVERSATION_SYSTEM_PROMPT
from dental_assistant.infrastructure.llm import call_llm


def generate_reply(payload: ConversationAgentInput) -> str:
    context = {
        "tone": payload.tone,
        "workflow": payload.workflow,
        "patient": payload.patient,
        "collected_fields": payload.collected_fields,
        "tool_results": payload.tool_results,
        "questions_to_ask": payload.questions_to_ask,
        "is_complete": payload.is_complete,
        "is_emergency": payload.is_emergency,
    }
    prompt = f"""{CONVERSATION_SYSTEM_PROMPT}

Structured context:
{json.dumps(context, ensure_ascii=False, indent=2)}

User message:
{payload.user_message}"""

    return call_llm(prompt).strip()
