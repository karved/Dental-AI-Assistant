"""Deterministic pipeline: safety -> orchestrator -> router -> tools/questions -> conversation agent.

Flow per turn:
  1. Safety filter (keyword blocklist)
  2. Load persisted state from last assistant message metadata
  3. LLM #1 (orchestrator) -> intent + extracted_fields + tone (ONLY)
  4. Merge extracted fields into state; resolve vague dates
  5. Deterministic router (Python decides readiness from collected_fields, NOT orchestrator):
     a. enough fields? -> execute tools
     b. missing fields? -> question selector picks next 1-2
     c. slots rejected? -> re-offer with exclusions
  6. LLM #2 (conversation agent) -> natural language reply
  7. Persist user message, assistant message + full metadata

This module contains zero SQL. The orchestrator does NOT influence question
selection or action dispatch -- those are purely deterministic.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from dental_assistant.application.conversation import generate_reply
from dental_assistant.application.orchestrator import run_orchestrator
from dental_assistant.domain.constants import (
    BLOCKED_PHRASES,
    READINESS_RULES,
    SAFETY_RESPONSE,
    SLOT_DURATION_MINUTES,
)
from dental_assistant.domain.date_resolver import resolve_date
from dental_assistant.domain.models import (
    ConversationAgentInput,
    OrchestratorOutput,
    TurnState,
)
from dental_assistant.domain.question_selector import select_questions
from dental_assistant.infrastructure import db as db_mod
from dental_assistant.infrastructure import queries as q
from dental_assistant.infrastructure import tools
from dental_assistant.infrastructure.tools import save_message
from dental_assistant.settings import get_settings

logger = logging.getLogger(__name__)

_SENSITIVE_PATTERNS = re.compile(r"(key|token|secret|password)=\S+", re.IGNORECASE)


def _sanitize_error(msg: str) -> str:
    """Strip API keys and tokens from error messages before persisting."""
    return _SENSITIVE_PATTERNS.sub(r"\1=***", msg)

_emergency_logger = logging.getLogger("dental_assistant.emergency")
if not _emergency_logger.handlers:
    _eh = logging.FileHandler("emergency.log", encoding="utf-8")
    _eh.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
    _emergency_logger.addHandler(_eh)
    _emergency_logger.setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Safety layer
# ---------------------------------------------------------------------------


def _keyword_safety_check(text: str) -> str | None:
    lowered = text.lower()
    if any(phrase in lowered for phrase in BLOCKED_PHRASES):
        return SAFETY_RESPONSE
    return None

# ---------------------------------------------------------------------------
# State persistence (via messages.metadata_json)
# ---------------------------------------------------------------------------

def _load_persisted_state(conn: Any, conversation_id: int) -> dict[str, Any]:
    raw = q.find_latest_assistant_metadata(conn, conversation_id)
    if not raw:
        return {}
    try:
        return json.loads(raw).get("turn_state", {})
    except (json.JSONDecodeError, TypeError):
        return {}


def _recent_summary(conn: Any, conversation_id: int, limit: int = 6) -> str:
    rows = q.find_recent_messages(conn, conversation_id, limit)
    lines = [f"{r['role']}: {r['content']}" for r in reversed(rows)]
    return "\n".join(lines)


def _ensure_conversation(conn: Any, conversation_id: int | None) -> int:
    if conversation_id and q.find_conversation_by_id(conn, conversation_id):
        return conversation_id
    return q.insert_conversation(conn)

# ---------------------------------------------------------------------------
# Field merging + date resolution
# ---------------------------------------------------------------------------

def _merge_fields(collected: dict[str, Any], new_fields: dict[str, Any]) -> None:
    for k, v in new_fields.items():
        if v is not None and v != "":
            if k == "slots_rejected":
                continue
            collected[k] = v

    if "date_preference" in collected:
        raw_date = collected["date_preference"]
        resolved = resolve_date(str(raw_date))
        if resolved:
            collected["date_resolved"] = resolved

# ---------------------------------------------------------------------------
# Deterministic readiness checks (Python decides, NOT the orchestrator)
# ---------------------------------------------------------------------------


def _is_ready(workflow: str, collected: dict[str, Any]) -> bool:
    required = READINESS_RULES.get(workflow)
    if required is None:
        return False
    if workflow == "faq":
        return "faq_topic" in collected
    if workflow == "emergency":
        return True
    return required.issubset(collected.keys())

# ---------------------------------------------------------------------------
# Slot filtering (exclude rejected slots)
# ---------------------------------------------------------------------------

def _check_availability_excluding(
    conn: Any, state: TurnState, limit: int = 5,
) -> dict[str, Any]:
    date_filter = state.collected_fields.get("date_resolved") or state.collected_fields.get("date_preference")
    avail = tools.check_availability(conn, date_filter=date_filter, limit=limit + len(state.rejected_slots))
    if not avail["ok"]:
        return avail
    filtered = [s for s in avail["slots"] if s["id"] not in state.rejected_slots]
    if not filtered:
        return {"ok": False, "error": "No more available slots. Try a different date?"}
    avail["slots"] = filtered[:limit]
    return avail

# ---------------------------------------------------------------------------
# Consecutive slot finder for family booking
# ---------------------------------------------------------------------------

def _find_consecutive_slots(slots: list[dict[str, Any]], count: int) -> list[dict[str, Any]] | None:
    """Find `count` back-to-back slots on the same date."""
    for i in range(len(slots) - count + 1):
        group = slots[i:i + count]
        if len(set(s["date"] for s in group)) != 1:
            continue
        consecutive = True
        for j in range(1, len(group)):
            prev_h, prev_m = map(int, group[j - 1]["time"].split(":"))
            curr_h, curr_m = map(int, group[j]["time"].split(":"))
            prev_total = prev_h * 60 + prev_m
            curr_total = curr_h * 60 + curr_m
            if curr_total - prev_total != SLOT_DURATION_MINUTES:
                consecutive = False
                break
        if consecutive:
            return group
    return None

# ---------------------------------------------------------------------------
# Deterministic router
# ---------------------------------------------------------------------------

def _route_faq(state: TurnState) -> None:
    topic = state.collected_fields.get("faq_topic")
    result = tools.get_office_info(topic)
    state.tool_results.append(result)
    state.is_complete = True


def _route_book_new(state: TurnState, conn: Any) -> None:
    fields = state.collected_fields
    phone = fields.get("phone")
    if not phone:
        return

    patient_result = tools.lookup_patient(conn, phone)
    if patient_result["ok"]:
        state.patient = patient_result["patient"]
    else:
        name = fields.get("name", "")
        if name:
            reg = tools.register_patient(conn, name, phone, fields.get("dob"), fields.get("insurance"))
            state.tool_results.append(reg)
            if reg["ok"]:
                state.patient = reg["patient"]
            else:
                return

    if not state.patient:
        return

    avail = _check_availability_excluding(conn, state)
    state.tool_results.append(avail)
    if not avail["ok"]:
        return

    slot = avail["slots"][0]
    appt_type = fields.get("appointment_type", "checkup")
    result = tools.book_appointment(
        conn, state.patient["id"], slot["id"], appt_type,
        is_emergency=state.is_emergency,
        emergency_summary=fields.get("notes"),
    )
    state.tool_results.append(result)
    if result["ok"]:
        state.is_complete = True


def _route_cancel(state: TurnState, conn: Any) -> None:
    phone = state.collected_fields.get("phone")
    if not phone:
        return

    patient_result = tools.lookup_patient(conn, phone)
    if not patient_result["ok"]:
        state.tool_results.append(patient_result)
        return
    state.patient = patient_result["patient"]

    appts = tools.get_patient_appointments(conn, state.patient["id"])
    state.tool_results.append(appts)
    if not appts["ok"] or not appts["appointments"]:
        return

    result = tools.cancel_appointment(conn, appts["appointments"][0]["id"])
    state.tool_results.append(result)
    if result["ok"]:
        state.is_complete = True


def _route_reschedule(state: TurnState, conn: Any) -> None:
    phone = state.collected_fields.get("phone")
    if not phone:
        return

    patient_result = tools.lookup_patient(conn, phone)
    if not patient_result["ok"]:
        state.tool_results.append(patient_result)
        return
    state.patient = patient_result["patient"]

    appts = tools.get_patient_appointments(conn, state.patient["id"])
    state.tool_results.append(appts)
    if not appts["ok"] or not appts["appointments"]:
        return

    avail = _check_availability_excluding(conn, state)
    state.tool_results.append(avail)
    if not avail["ok"]:
        return

    result = tools.reschedule_appointment(conn, appts["appointments"][0]["id"], avail["slots"][0]["id"])
    state.tool_results.append(result)
    if result["ok"]:
        state.is_complete = True


def _route_emergency(state: TurnState, conn: Any) -> None:
    state.is_emergency = True
    state.tone = "emergency"
    fields = state.collected_fields
    phone = fields.get("phone")
    if phone:
        patient_result = tools.lookup_patient(conn, phone)
        if patient_result["ok"]:
            state.patient = patient_result["patient"]

    avail = tools.check_availability(conn, limit=3)
    state.tool_results.append(avail)
    if avail["ok"] and state.patient:
        slot = avail["slots"][0]
        result = tools.book_appointment(
            conn, state.patient["id"], slot["id"], "emergency",
            is_emergency=True,
            emergency_summary=fields.get("symptoms") or fields.get("notes") or "Emergency",
        )
        state.tool_results.append(result)
        if result["ok"]:
            state.is_complete = True


def _route_family_book(state: TurnState, conn: Any) -> None:
    fields = state.collected_fields
    phone = fields.get("phone")
    if not phone:
        return

    patient_result = tools.lookup_patient(conn, phone)
    if patient_result["ok"]:
        state.patient = patient_result["patient"]
    else:
        name = fields.get("name", "")
        if name:
            reg = tools.register_patient(conn, name, phone, fields.get("dob"), fields.get("insurance"))
            state.tool_results.append(reg)
            if reg["ok"]:
                state.patient = reg["patient"]
            else:
                return

    if not state.patient:
        return

    family_size = int(fields.get("family_size", 1))
    avail = _check_availability_excluding(conn, state, limit=family_size * 3)
    state.tool_results.append(avail)
    if not avail["ok"]:
        return

    consecutive = _find_consecutive_slots(avail["slots"], family_size)
    if not consecutive:
        state.tool_results.append({"ok": False, "error": f"Could not find {family_size} back-to-back slots. Try a different date?"})
        return

    for slot in consecutive:
        result = tools.book_appointment(conn, state.patient["id"], slot["id"], "checkup")
        state.tool_results.append(result)
        state.family_members.append({"slot": slot, "booked": result["ok"]})

    state.is_complete = all(m["booked"] for m in state.family_members)


_ROUTERS = {
    "faq":         lambda state, conn: _route_faq(state),
    "book_new":    _route_book_new,
    "cancel":      _route_cancel,
    "reschedule":  _route_reschedule,
    "emergency":   _route_emergency,
    "family_book": _route_family_book,
}


def _run_router(state: TurnState, conn: Any) -> None:
    """Deterministic routing based on collected_fields, not orchestrator decisions."""
    workflow = state.workflow

    if state.collected_fields.get("slots_rejected"):
        prev_results = state.tool_results
        slot_ids = [
            s["id"]
            for r in prev_results if r.get("ok") and "slots" in r
            for s in r["slots"]
        ]
        state.rejected_slots.extend(slot_ids)
        del state.collected_fields["slots_rejected"]

    if _is_ready(workflow, state.collected_fields):
        router_fn = _ROUTERS.get(workflow)
        if router_fn:
            router_fn(state, conn)

    if not state.is_complete and not state.tool_results:
        state.questions_to_ask = select_questions(workflow, state.collected_fields)

# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def process_message(
    user_message: str,
    conversation_id: int | None = None,
    *,
    db_path: str | None = None,
) -> tuple[int, str, dict[str, Any]]:
    """Run one user turn. Returns (conversation_id, assistant_reply, debug_metadata)."""
    settings = get_settings()
    db_mod.init_db(db_path)

    blocked = _keyword_safety_check(user_message)

    with db_mod.connection(db_path) as conn:
        cid = _ensure_conversation(conn, conversation_id)
        save_message(conn, cid, "user", user_message)

        if blocked:
            meta = {"safety": "keyword_block"}
            save_message(conn, cid, "assistant", blocked, json.dumps(meta))
            return cid, blocked, meta

        persisted = _load_persisted_state(conn, cid)
        state = TurnState(
            conversation_id=cid,
            user_message=user_message,
            collected_fields=persisted.get("collected_fields", {}),
            patient=persisted.get("patient", {}),
            family_members=persisted.get("family_members", []),
            rejected_slots=persisted.get("rejected_slots", []),
            workflow=persisted.get("workflow", "unknown"),
            is_emergency=persisted.get("is_emergency", False),
            emergency_logged=persisted.get("emergency_logged", False),
        )

        if not settings.llm_ready:
            reply = (
                "Set LLM_API_KEY (and LLM_PROVIDER / LLM_MODEL) in your environment "
                "to enable AI responses. See .env.template."
            )
            meta: dict[str, Any] = {"turn_state": state.model_dump()}
            save_message(conn, cid, "assistant", reply, json.dumps(meta))
            return cid, reply, meta

        summary = _recent_summary(conn, cid)
        try:
            orch = run_orchestrator(user_message, summary)
        except Exception as exc:
            logger.exception("Orchestrator failed")
            orch = OrchestratorOutput()
            state.orchestrator_output["error"] = _sanitize_error(str(exc))

        state.orchestrator_output = orch.model_dump()
        if state.workflow == "unknown" or orch.intent != "unknown":
            state.workflow = orch.intent
        state.tone = orch.tone
        state.is_emergency = orch.intent == "emergency" or state.is_emergency
        _merge_fields(state.collected_fields, orch.extracted_fields)

        _run_router(state, conn)

        if state.is_emergency and state.is_complete and not state.emergency_logged:
            _emergency_logger.warning(
                "conversation_id=%s | name=%s | phone=%s | summary=%s",
                state.conversation_id,
                state.patient.get("name", state.collected_fields.get("name", "unknown")),
                state.patient.get("phone", state.collected_fields.get("phone", "unknown")),
                state.collected_fields.get("symptoms")
                or state.collected_fields.get("notes")
                or "No details provided",
            )
            state.emergency_logged = True

        payload = ConversationAgentInput(
            tone=state.tone,
            workflow=state.workflow,
            patient=state.patient,
            collected_fields=state.collected_fields,
            tool_results=state.tool_results,
            questions_to_ask=state.questions_to_ask,
            is_complete=state.is_complete,
            is_emergency=state.is_emergency,
            user_message=user_message,
        )
        try:
            reply = generate_reply(payload)
        except Exception as exc:
            logger.exception("Conversation agent failed")
            reply = "Sorry, something went wrong generating a reply. Please try again."
            state.orchestrator_output["conversation_error"] = _sanitize_error(str(exc))

        meta = {"turn_state": state.model_dump()}
        save_message(conn, cid, "assistant", reply, json.dumps(meta))

    return cid, reply, meta
