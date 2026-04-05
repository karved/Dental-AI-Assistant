"""LLM #2: natural language from structured facts only. No routing decisions."""

from __future__ import annotations

import json

from dental_assistant.domain.models import ConversationAgentInput
from dental_assistant.infrastructure.llm import call_llm


def generate_reply(payload: ConversationAgentInput) -> str:
    blob = {
        "tone": payload.tone,
        "facts": payload.facts,
        "assistant_goal": payload.assistant_goal,
    }
    prompt = f"""You are the voice of a dental practice front desk.
Write a short reply (1-3 sentences). Match the tone hint. Use only the facts given; do not invent policies, times, or prices.
If facts are empty, ask one clarifying question or give a minimal acknowledgment.

Tone: {payload.tone}
Structured context (JSON):
{json.dumps(blob, ensure_ascii=False)}

Latest user message:
{payload.user_message}
"""
    return call_llm(prompt).strip()
