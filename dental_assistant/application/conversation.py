"""LLM #2: natural language generation from structured state only.

This module does NOT make routing decisions, call tools, or modify state.
It receives a ConversationAgentInput and returns a human-readable reply.
"""

from __future__ import annotations

import json
from datetime import date, time

from dental_assistant.domain.models import ConversationAgentInput
from dental_assistant.domain.prompts import CONVERSATION_SYSTEM_PROMPT
from dental_assistant.infrastructure.llm import call_llm


def _friendly_date(iso_date: str) -> str:
    d = date.fromisoformat(str(iso_date).strip()[:10])
    return f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}"


def _friendly_time(hhmm: str) -> str:
    parts = str(hhmm).strip().split(":")
    t = time(int(parts[0]), int(parts[1]))
    label = t.strftime("%I:%M %p")
    return label[1:] if label.startswith("0") else label


def _visit_note_summary(text: str) -> str:
    cleaned = " ".join(str(text).split()).strip()
    if not cleaned:
        return ""
    sentence = cleaned.split(".")[0].strip()
    return sentence if sentence.endswith(".") else f"{sentence}."


def _decorate_display(value):
    if isinstance(value, list):
        return [_decorate_display(v) for v in value]
    if not isinstance(value, dict):
        return value

    out = {k: _decorate_display(v) for k, v in value.items()}

    raw_date = value.get("date")
    raw_time = value.get("time")
    if isinstance(raw_date, str):
        try:
            out["friendly_date"] = _friendly_date(raw_date)
        except ValueError:
            pass
    if isinstance(raw_time, str):
        try:
            out["friendly_time"] = _friendly_time(raw_time)
        except ValueError:
            pass
    if out.get("friendly_date") and out.get("friendly_time"):
        out["friendly_datetime_pt"] = f"{out['friendly_date']} at {out['friendly_time']} PT"

    raw_old_date = value.get("old_date")
    raw_old_time = value.get("old_time")
    if isinstance(raw_old_date, str):
        try:
            out["friendly_old_date"] = _friendly_date(raw_old_date)
        except ValueError:
            pass
    if isinstance(raw_old_time, str):
        try:
            out["friendly_old_time"] = _friendly_time(raw_old_time)
        except ValueError:
            pass
    if out.get("friendly_old_date") and out.get("friendly_old_time"):
        out["friendly_old_datetime_pt"] = f"{out['friendly_old_date']} at {out['friendly_old_time']} PT"

    raw_notes = value.get("visit_notes")
    if isinstance(raw_notes, str) and raw_notes.strip():
        out["visit_notes_summary"] = _visit_note_summary(raw_notes)

    if "slots" in out and isinstance(out["slots"], list):
        out["slot_options_pt"] = [s["friendly_datetime_pt"] for s in out["slots"] if isinstance(s, dict) and s.get("friendly_datetime_pt")]
    if "appointments" in out and isinstance(out["appointments"], list):
        out["appointment_options_pt"] = [a["friendly_datetime_pt"] for a in out["appointments"] if isinstance(a, dict) and a.get("friendly_datetime_pt")]
    if out.get("family_block_pt"):
        start = out["family_block_pt"].get("start")
        members = out["family_block_pt"].get("family_size")
        times = out["family_block_pt"].get("times") or []
        if start and members and times:
            out["family_block_summary"] = (
                f"{members} back-to-back appointments starting {start}; "
                f"times: {', '.join(times)}."
            )
    return out


def generate_reply(payload: ConversationAgentInput) -> str:
    context = {
        "tone": payload.tone,
        "workflow": payload.workflow,
        "patient": _decorate_display(payload.patient),
        "collected_fields": _decorate_display(payload.collected_fields),
        "tool_results": _decorate_display(payload.tool_results),
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
