"""LLM #1: intent classification + entity extraction. Outputs strict JSON only.

The orchestrator ONLY classifies and extracts. It does NOT:
- decide which questions to ask
- decide what tools to run
- control flow or pacing
Those are the deterministic Python layer's responsibility.
"""

from __future__ import annotations

import json
import re
from typing import Any

from dental_assistant.domain.models import OrchestratorOutput
from dental_assistant.domain.prompts import ORCHESTRATOR_SYSTEM_PROMPT
from dental_assistant.infrastructure.llm import call_llm

_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)


def _parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    m = _JSON_FENCE.search(text)
    if m:
        text = m.group(1).strip()
    return json.loads(text)


def run_orchestrator(
    user_message: str,
    conversation_summary: str = "",
    prior_structured_state: str = "",
) -> OrchestratorOutput:
    prior = prior_structured_state.strip() or "{}"
    prompt = f"""{ORCHESTRATOR_SYSTEM_PROMPT}

Prior structured state (JSON; system-confirmed carry-over, may be empty {{}}):
{prior}

Recent conversation (may be empty):
{conversation_summary}

User message:
{user_message}"""

    raw = call_llm(prompt)
    try:
        data = _parse_json_object(raw)
        return OrchestratorOutput.model_validate(data)
    except (json.JSONDecodeError, ValueError):
        return OrchestratorOutput()
