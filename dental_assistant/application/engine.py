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

Database access (production note):
  One SQLite connection wraps each ``process_message`` turn. The router and tools
  issue multiple small queries (messages, patient, slots, appointments) rather
  than one mega-select. That keeps locking short, matches how tools are tested,
  and is usually negligible vs LLM latency. At higher scale (e.g. Postgres),
  optionally add a repository layer with batched reads for hot paths
  (patient + open appointments in one round-trip).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from dental_assistant.application.conversation import generate_reply
from dental_assistant.application.orchestrator import run_orchestrator
from dental_assistant.domain.appointments import coerce_appointment_type
from dental_assistant.domain.constants import (
    AVAILABILITY_PIPELINE_DEFAULT_LIMIT,
    BLOCKED_PHRASES,
    EMERGENCY_AVAILABILITY_SQL_LIMIT,
    FIELD_QUESTIONS,
    MAX_SLOTS_OFFERED_TO_USER,
    ORCHESTRATOR_INTENTS_ROUTINE,
    ORCHESTRATOR_PRIOR_STATE_KEYS,
    ORCHESTRATOR_RECENT_MESSAGES,
    READINESS_RULES,
    SAFETY_RESPONSE,
    SLOT_DURATION_MINUTES,
    SLOT_PIPELINE_RETURN_CAP_SINGLE_DAY,
    SLOT_SQL_FETCH_CAP_MIN_SINGLE_DAY,
)
from dental_assistant.domain.date_resolver import resolve_date, resolve_date_range
from dental_assistant.domain.pt_time import (
    is_past_office_close_pt,
    is_same_calendar_day_pt,
    pt_today_iso,
    same_day_booking_closed_result,
)
from dental_assistant.domain.models import (
    ConversationAgentInput,
    OrchestratorOutput,
    TurnState,
)
from dental_assistant.domain.question_selector import max_questions_for_workflow, select_questions
from dental_assistant.domain.utterances import SHORT_AFFIRMATIVE, infer_offered_list_ordinal
from dental_assistant.domain.time_parse import (
    normalize_time_token as _normalize_time_token,
    normalized_time_from_fields_or_message as _normalized_time_from_fields_or_message,
    slot_time_prefix as _slot_time_prefix,
)
from dental_assistant.application.appointment_resolution import resolve_appointment_selection_full
from dental_assistant.application.family_booking import find_consecutive_block_starting_at
from dental_assistant.application.patient_gate import book_new_or_family_preflight
from dental_assistant.application.visit_notes import build_visit_notes_from_fields
from dental_assistant.infrastructure import db as db_mod
from dental_assistant.infrastructure import queries as q
from dental_assistant.infrastructure import tools
from dental_assistant.infrastructure.tools import save_message
from dental_assistant.settings import get_settings

logger = logging.getLogger(__name__)

_SENSITIVE_PATTERNS = re.compile(r"(key|token|secret|password)=\S+", re.IGNORECASE)
_FLOW_RESET_RE = re.compile(
    r"\b(start over|restart|never mind|nevermind|forget it|different question|new question)\b",
    re.IGNORECASE,
)


def _sanitize_error(msg: str) -> str:
    """Strip API keys and tokens from error messages before persisting."""
    return _SENSITIVE_PATTERNS.sub(r"\1=***", msg)


class _EmergencyUTCFormatter(logging.Formatter):
    """Always format log time as UTC (Formatter.converter is easy to get wrong across versions)."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        return dt.strftime(datefmt or "%Y-%m-%dT%H:%M:%S")


_emergency_logger = logging.getLogger("dental_assistant.emergency")
_emergency_logger.propagate = False
if not _emergency_logger.handlers:
    _eh = logging.FileHandler("emergency.log", encoding="utf-8")
    _eh.setFormatter(_EmergencyUTCFormatter("%(asctime)sZ | %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"))
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


def _recent_summary(conn: Any, conversation_id: int, limit: int = ORCHESTRATOR_RECENT_MESSAGES) -> str:
    """Recent prior transcript only; the current user message is passed separately."""
    rows = q.find_recent_messages(conn, conversation_id, limit)
    lines = [f"{r['role']}: {r['content']}" for r in reversed(rows)]
    return "\n".join(lines)


def _pending_interaction_state(persisted: dict[str, Any]) -> dict[str, Any]:
    fields = persisted.get("collected_fields") or {}
    offered_slot_ids = list(persisted.get("offered_slot_ids") or [])
    offered_appointment_ids = list(persisted.get("offered_appointment_ids") or [])
    snapshot: dict[str, Any] = {
        "awaiting_slot_selection": bool(offered_slot_ids),
        "awaiting_appointment_selection": bool(offered_appointment_ids),
        "awaiting_identity_confirmation": bool(fields.get("_pending_identity_name")),
    }
    if offered_slot_ids:
        snapshot["offered_slot_count"] = len(offered_slot_ids)
    if offered_appointment_ids:
        snapshot["offered_appointment_count"] = len(offered_appointment_ids)
    if persisted.get("slots_offered_for_date"):
        snapshot["slots_offered_for_date"] = persisted["slots_offered_for_date"]
    if persisted.get("pending_family_size"):
        snapshot["pending_family_size"] = persisted["pending_family_size"]
    return snapshot


def _orchestrator_prior_context(persisted: dict[str, Any]) -> str:
    """Compact JSON of system-known fields for LLM1 (see ORCHESTRATOR_PRIOR_STATE_KEYS)."""
    fields = persisted.get("collected_fields") or {}
    subset: dict[str, Any] = {}
    for key in ORCHESTRATOR_PRIOR_STATE_KEYS:
        if key not in fields:
            continue
        val = fields[key]
        if val is None or val == "":
            continue
        subset[key] = val
    payload = {
        "prior_workflow": persisted.get("workflow", "unknown"),
        "prior_is_emergency": bool(persisted.get("is_emergency", False)),
        "collected_fields": subset,
        "interaction_state": _pending_interaction_state(persisted),
    }
    return json.dumps(payload, ensure_ascii=False)


def _should_preserve_active_workflow(
    persisted: dict[str, Any],
    orch_intent: str,
    user_message: str,
) -> bool:
    """Keep the current workflow when the user is mid-selection and LLM1 returns only general/unknown."""
    if persisted.get("is_complete"):
        return False
    if _FLOW_RESET_RE.search(user_message):
        return False
    prior_workflow = persisted.get("workflow", "unknown")
    interaction = _pending_interaction_state(persisted)
    has_pending_step = any(
        (
            interaction.get("awaiting_slot_selection"),
            interaction.get("awaiting_appointment_selection"),
            interaction.get("awaiting_identity_confirmation"),
            bool(interaction.get("pending_family_size")),
        )
    )
    if not has_pending_step:
        return False
    if prior_workflow == "family_book" and orch_intent in ("book_new", "family_book", "unknown", "general"):
        return True
    if interaction.get("awaiting_slot_selection") and orch_intent in ("book_new", "unknown", "general"):
        return True
    return orch_intent in ("unknown", "general")


def _persistable_turn_state(state: TurnState) -> dict[str, Any]:
    """Small DB snapshot for the next turn; avoid persisting bulky tool payloads."""
    return {
        "workflow": state.workflow,
        "patient": dict(state.patient),
        "family_members": list(state.family_members),
        "collected_fields": dict(state.collected_fields),
        "rejected_slots": list(state.rejected_slots),
        "offered_slot_ids": list(state.offered_slot_ids),
        "slots_offered_for_date": state.slots_offered_for_date,
        "pending_family_size": state.pending_family_size,
        "offered_appointment_ids": list(state.offered_appointment_ids),
        "is_complete": state.is_complete,
        "is_emergency": state.is_emergency,
        "emergency_logged": state.emergency_logged,
    }


def _identity_carry_fields(state: TurnState) -> dict[str, Any]:
    carry: dict[str, Any] = {}
    patient = state.patient
    if patient:
        if patient.get("name"):
            carry["name"] = patient["name"]
        if patient.get("phone"):
            carry["phone"] = patient["phone"]
        carry["identity_verified"] = True
        return carry
    fields = state.collected_fields
    for key in ("name", "phone", "identity_verified"):
        val = fields.get(key)
        if val not in (None, ""):
            carry[key] = val
    return carry


def _ensure_conversation(conn: Any, conversation_id: int | None) -> int:
    if conversation_id and q.find_conversation_by_id(conn, conversation_id):
        return conversation_id
    return q.insert_conversation(conn)

# ---------------------------------------------------------------------------
# Field merging + date resolution
# ---------------------------------------------------------------------------

def _merge_fields(collected: dict[str, Any], new_fields: dict[str, Any]) -> None:
    date_pref_updated = False
    for k, v in new_fields.items():
        if v is not None and v != "":
            if k == "slots_rejected":
                continue
            collected[k] = v
            if k == "date_preference":
                date_pref_updated = True

    if date_pref_updated and "date_preference" in collected:
        raw_date = collected["date_preference"]
        resolved = resolve_date(str(raw_date))
        if resolved:
            collected["date_resolved"] = resolved
        else:
            collected.pop("date_resolved", None)

    family_names = collected.get("family_member_names")
    if family_names and "family_size" not in collected:
        if isinstance(family_names, list):
            clean = [str(n).strip() for n in family_names if str(n).strip()]
            if clean:
                collected["family_size"] = len(clean)
        elif isinstance(family_names, str) and family_names.strip():
            collected["family_size"] = 1


def _clear_stale_time_hints_if_needed(collected: dict[str, Any], new_fields: dict[str, Any], user_message: str) -> None:
    """When a new date arrives without a fresh time, avoid reusing an old selected_time."""
    if "date_preference" not in new_fields:
        return
    if "selected_time" in new_fields or "time_preference" in new_fields:
        return
    if "same time" in user_message.lower():
        return
    collected.pop("selected_time", None)
    collected.pop("time_preference", None)

# ---------------------------------------------------------------------------
# Intent coercion (orchestrator sometimes returns general for appointment checks)
# ---------------------------------------------------------------------------

_APPOINTMENT_STATUS_PHRASE = re.compile(
    r"(do\s+i\s+have|have\s+i\s+got|have\s+any|any|my|check).{0,48}(appointment|appointments|booking|visit)"
    r"|(appointment|appointments).{0,48}(scheduled|schedul|booked|coming\s+up|have|on\s+file)"
    r"|when\s+is\s+my\s+(appointment|visit)",
    re.IGNORECASE,
)

_URGENT_CLINICAL_RE = re.compile(
    r"ache",
    r"\b(bleed|bleeding|\bblood\b|hemorrhag|"
    r"severe\s+pain|excruciating|unbearable\s+pain|"
    r"swollen|swelling|abscess|"
    r"broken\s+tooth|cracked\s+tooth|knocked\s+out|avulsed|"
    r"dental\s+trauma|jaw\s+(locked|stuck))\b",
    re.IGNORECASE,
)


def _urgent_clinical_context(fields: dict[str, Any], user_message: str) -> bool:
    blob = " ".join(
        [
            user_message,
            str(fields.get("symptoms") or ""),
            str(fields.get("notes") or ""),
        ]
    )
    return bool(_URGENT_CLINICAL_RE.search(blob))


def _apply_clinical_urgency(state: TurnState, user_message: str) -> None:
    """Orchestrator sometimes uses book_new for bleeding/pain; align tone, flags, and workflow."""
    if not _urgent_clinical_context(state.collected_fields, user_message):
        return
    state.is_emergency = True
    state.tone = "emergency"
    if state.workflow in ("general", "unknown", "faq"):
        state.workflow = "emergency"


def _coerce_workflow_for_appointment_lookup(
    state: TurnState,
    user_message: str,
    conversation_summary: str,
) -> None:
    """When we already have a phone, treat visit-status questions as appointment_status not general."""
    if state.workflow not in ("general", "unknown") or not state.collected_fields.get("phone"):
        return
    blob = conversation_summary.lower()
    if _APPOINTMENT_STATUS_PHRASE.search(user_message):
        state.workflow = "appointment_status"
        return
    t = user_message.strip().lower()
    if t in SHORT_AFFIRMATIVE and "appointment" in blob and any(
        w in blob for w in ("look up", "lookup", "look for", "check", "upcoming", "scheduled", "on file")
    ):
        state.workflow = "appointment_status"


def _hydrate_existing_patient_profile(state: TurnState, conn: Any) -> None:
    """If phone matches an existing patient, fill DOB/insurance from DB so we do not re-ask (phone is the key)."""
    if state.workflow not in ("book_new", "family_book"):
        return
    if not state.collected_fields.get("identity_verified"):
        return
    phone = state.collected_fields.get("phone")
    if not phone:
        return
    if not state.patient.get("id"):
        pr = tools.lookup_patient(conn, phone)
        if pr["ok"]:
            state.patient = pr["patient"]
    if not state.patient.get("id"):
        return
    pat = state.patient
    if pat.get("dob") and "dob" not in state.collected_fields:
        state.collected_fields["dob"] = pat["dob"]
    ins = pat.get("insurance")
    if ins and str(ins).strip() and "insurance" not in state.collected_fields:
        state.collected_fields["insurance"] = ins

# ---------------------------------------------------------------------------
# Deterministic readiness checks (Python decides, NOT the orchestrator)
# ---------------------------------------------------------------------------


def _is_ready(workflow: str, collected: dict[str, Any]) -> bool:
    required = READINESS_RULES.get(workflow)
    if required is None:
        return False
    if workflow == "faq":
        return "faq_topic" in collected
    return required.issubset(collected.keys())

# ---------------------------------------------------------------------------
# Slot filtering (exclude rejected slots)
# ---------------------------------------------------------------------------

def _filter_by_time_preference(slots: list[dict[str, Any]], pref: str | None) -> list[dict[str, Any]]:
    """Filter slots by morning/afternoon preference. Returns all if pref is None or unrecognised."""
    if not pref:
        return slots
    p = pref.strip().lower()
    exact = _normalize_time_token(p.replace(" ", ""))
    if exact:
        hits = [s for s in slots if _slot_time_prefix(s["time"]) == exact]
        if hits:
            return hits
    if p in ("morning", "am"):
        return [s for s in slots if int(s["time"].split(":")[0]) < 12]
    if p in ("afternoon", "pm", "evening"):
        return [s for s in slots if int(s["time"].split(":")[0]) >= 12]
    return slots


def _time_token_for_availability_filter(collected: dict[str, Any]) -> str | None:
    """Prefer explicit time_preference; else use selected_time (orchestrator often puts 5pm there, not time_preference)."""
    tp = collected.get("time_preference")
    if isinstance(tp, str) and tp.strip():
        return tp.strip()
    st = collected.get("selected_time")
    if isinstance(st, str) and st.strip():
        return st.strip()
    return None


def _resolved_date_iso(collected: dict[str, Any]) -> str | None:
    """Calendar date (YYYY-MM-DD) for availability, after resolving vague preferences."""
    dr = collected.get("date_resolved")
    if dr:
        return str(dr).strip()[:10]
    pref = collected.get("date_preference")
    if pref:
        return resolve_date(str(pref))
    return None


def _resolved_date_window(collected: dict[str, Any]) -> tuple[str | None, str | None]:
    pref = collected.get("date_preference")
    if not pref:
        day = _resolved_date_iso(collected)
        return day, day
    rng = resolve_date_range(str(pref))
    if rng:
        return rng
    day = _resolved_date_iso(collected)
    return day, day


def _check_availability_excluding(
    conn: Any, state: TurnState, limit: int = 5,
) -> dict[str, Any]:
    resolved_cal = _resolved_date_iso(state.collected_fields)
    range_start, range_end = _resolved_date_window(state.collected_fields)
    if (
        resolved_cal
        and is_same_calendar_day_pt(resolved_cal)
        and is_past_office_close_pt()
    ):
        return same_day_booking_closed_result()

    date_filter = resolved_cal if range_start and range_end and range_start == range_end else None
    date_from = None if date_filter else range_start
    date_to = None if date_filter else range_end
    time_pref = _time_token_for_availability_filter(state.collected_fields)
    fetch_limit = (limit + len(state.rejected_slots)) * (3 if time_pref else 1)
    # One office day can exceed `limit` half-hour blocks (8:00–17:30 → 20 rows). A low SQL LIMIT
    # or truncating filtered[:limit] drops afternoon slots even when they exist in the DB.
    iso_day = (
        resolved_cal
        and len(str(resolved_cal).strip()) >= 10
        and str(resolved_cal)[4] == "-"
        and str(resolved_cal)[7] == "-"
    )
    if iso_day:
        fetch_limit = max(fetch_limit, SLOT_SQL_FETCH_CAP_MIN_SINGLE_DAY)
    avail = tools.check_availability(conn, date_filter=date_filter, date_from=date_from, date_to=date_to, limit=fetch_limit)

    if not avail["ok"] and (date_filter or date_from or date_to):
        avail = tools.check_availability(conn, limit=fetch_limit)
        if avail["ok"]:
            avail["broadened"] = True
            avail["original_date"] = date_filter or date_from

    if not avail["ok"]:
        return avail

    filtered = [s for s in avail["slots"] if s["id"] not in state.rejected_slots]
    filtered = _filter_by_time_preference(filtered, time_pref)

    if not filtered:
        return {"ok": False, "error": "No available slots for that time preference. Try a different time or date?"}
    out_limit = max(limit, SLOT_PIPELINE_RETURN_CAP_SINGLE_DAY) if iso_day else limit
    avail["slots"] = filtered[:out_limit]
    return avail

# ---------------------------------------------------------------------------
# Consecutive slot finder for family booking
# ---------------------------------------------------------------------------

def _minutes_from_slot_time(time_str: str) -> int:
    h, m = map(int, time_str.split(":")[:2])
    return h * 60 + m


def _is_back_to_back_slot_step(prev_time: str, curr_time: str) -> bool:
    return _minutes_from_slot_time(curr_time) - _minutes_from_slot_time(prev_time) == SLOT_DURATION_MINUTES


def _find_consecutive_slots(slots: list[dict[str, Any]], count: int) -> list[dict[str, Any]] | None:
    """Find `count` back-to-back slots on the same date."""
    for i in range(len(slots) - count + 1):
        group = slots[i:i + count]
        if len(set(s["date"] for s in group)) != 1:
            continue
        if all(
            _is_back_to_back_slot_step(group[j - 1]["time"], group[j]["time"])
            for j in range(1, len(group))
        ):
            return group
    return None

# ---------------------------------------------------------------------------
# Slot offer -> user confirms (no silent auto-book of first slot)
# ---------------------------------------------------------------------------


def _slot_date_key(fields: dict[str, Any]) -> str:
    return str(fields.get("date_resolved") or fields.get("date_preference") or "")


def _clear_slot_offer(state: TurnState) -> None:
    state.offered_slot_ids = []
    state.slots_offered_for_date = None
    state.pending_family_size = None


def _clear_appointment_offer(state: TurnState) -> None:
    state.offered_appointment_ids = []


def _parse_slot_id(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _resolve_offered_appointment_id(
    valid_appointment_ids: list[int],
    persisted_offered_ids: list[int],
    fields: dict[str, Any],
    user_message: str,
) -> int | None:
    """Resolve which appointment the user means.

    Explicit ids from the orchestrator work on the first turn. Ordinal phrases
    ("first", "second") apply only after we've shown a list (persisted_offered_ids).
    """
    valid_set = set(valid_appointment_ids)
    for key in ("selected_appointment_id", "appointment_id"):
        aid = _parse_slot_id(fields.get(key))
        if aid is not None and aid in valid_set:
            return aid
    if persisted_offered_ids:
        offered_set = set(persisted_offered_ids)
        aid = infer_offered_list_ordinal(
            user_message, persisted_offered_ids, fields, _normalized_time_from_fields_or_message,
        )
        if aid is not None and aid in offered_set and aid in valid_set:
            return aid
    return None


def _resolve_offered_slot(
    slots: list[dict[str, Any]],
    offered_slot_ids: list[int],
    fields: dict[str, Any],
    user_message: str,
) -> dict[str, Any] | None:
    """Match user choice to a slot; only ids listed in offered_slot_ids are valid."""
    if not offered_slot_ids:
        return None
    offered_set = set(offered_slot_ids)
    sid = _parse_slot_id(fields.get("selected_slot_id"))
    if sid is not None and sid in offered_set:
        for s in slots:
            if s["id"] == sid:
                return s
    norm = _normalized_time_from_fields_or_message(fields, user_message)
    if norm:
        for s in slots:
            if s["id"] not in offered_set:
                continue
            if _slot_time_prefix(s["time"]) == norm:
                return s
    sid = infer_offered_list_ordinal(
        user_message, offered_slot_ids, fields, _normalized_time_from_fields_or_message,
    )
    if sid is not None and sid in offered_set:
        for s in slots:
            if s["id"] == sid:
                return s
    return None


def _resolved_booking_date_iso(fields: dict[str, Any]) -> str | None:
    dr = fields.get("date_resolved")
    if dr:
        return str(dr).strip()[:10]
    pref = fields.get("date_preference")
    if pref:
        return resolve_date(str(pref))
    return None


def _find_slot_outside_offered_shortlist(
    conn: Any,
    slots: list[dict[str, Any]],
    offered_slot_ids: list[int],
    fields: dict[str, Any],
    user_message: str,
) -> dict[str, Any] | None:
    """If the user asked for a time not in the offered subset, find it in the wider fetch or DB."""
    if not offered_slot_ids:
        return None
    date_iso = _resolved_booking_date_iso(fields)
    if not date_iso:
        return None
    norm = _normalized_time_from_fields_or_message(fields, user_message)
    if not norm:
        return None
    offered_set = set(offered_slot_ids)
    for s in slots:
        if s.get("date") != date_iso or _slot_time_prefix(s["time"]) != norm:
            continue
        if s["id"] not in offered_set:
            return s
    row = q.find_available_slot_at_time(conn, date_iso, norm)
    if not row:
        return None
    found = dict(row)
    if found["id"] in offered_set:
        return None
    return found


def _payload_requested_time_found(slot: dict[str, Any], *, reschedule: bool = False) -> dict[str, Any]:
    base = {
        "ok": True,
        "slots": [slot],
        "awaiting_selection": True,
        "requested_time_matched": True,
        "message": (
            "That time was not in the short list we showed, but it is still available. "
            "Reply yes or confirm that time to book — nothing is confirmed until you do."
        ),
        "timezone_note": "All times are US Pacific (PT).",
    }
    if reschedule:
        base["reschedule"] = True
    return base


def _find_consecutive_from_start(
    slots: list[dict[str, Any]], start_id: int, count: int,
) -> list[dict[str, Any]] | None:
    idx = next((i for i, s in enumerate(slots) if s["id"] == start_id), None)
    if idx is None or idx + count > len(slots):
        return None
    group = slots[idx : idx + count]
    if len(set(s["date"] for s in group)) != 1:
        return None
    for j in range(1, len(group)):
        if not _is_back_to_back_slot_step(group[j - 1]["time"], group[j]["time"]):
            return None
    return group


def _affirmative_message(text: str) -> bool:
    return text.strip().lower() in SHORT_AFFIRMATIVE


def _availability_payload(avail: dict[str, Any], offered: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": True,
        "slots": offered,
        "awaiting_selection": True,
        "message": "Nothing is booked until you pick a time from the list.",
        "timezone_note": "All times are US Pacific (PT).",
    }
    if avail.get("broadened"):
        out["broadened"] = True
        out["original_date"] = avail.get("original_date")
    return out


def _appointment_selection_tool_result(appt_list: list[dict[str, Any]], message: str) -> dict[str, Any]:
    return {
        "ok": True,
        "awaiting_appointment_selection": True,
        "appointments": appt_list,
        "message": message,
        "timezone_note": "Appointment times below are US Pacific (PT).",
    }


def _drop_today_slots_if_closed_pt(slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """After 6 PM PT, same-calendar-day slots are not offered (office closed)."""
    if not is_past_office_close_pt():
        return slots
    today = pt_today_iso()
    return [s for s in slots if s.get("date") != today]


def _emergency_availability(conn: Any) -> dict[str, Any]:
    """Next bookable slots; skips remainder of today in PT when office is closed."""
    avail = tools.check_availability(conn, limit=EMERGENCY_AVAILABILITY_SQL_LIMIT)
    if not avail["ok"]:
        return avail
    filtered = _drop_today_slots_if_closed_pt(avail["slots"])
    if not filtered:
        return same_day_booking_closed_result()
    avail["slots"] = filtered
    return avail

# ---------------------------------------------------------------------------
# Deterministic router
# ---------------------------------------------------------------------------

def _split_faq_topics(raw: str) -> list[str]:
    """Split multi-topic FAQ input like 'timings and location' into individual topics."""
    parts = re.split(r"\s+and\s+|,\s*", raw.strip())
    return [p.strip() for p in parts if p.strip()]


def _route_appointment_status(state: TurnState, conn: Any) -> None:
    phone = state.collected_fields.get("phone")
    if not phone:
        return

    patient_result = tools.lookup_patient(conn, phone)
    if not patient_result["ok"]:
        state.tool_results.append(patient_result)
        state.is_complete = True
        return
    state.patient = patient_result["patient"]

    appts = tools.get_patient_appointments(conn, state.patient["id"])
    state.tool_results.append(appts)
    state.is_complete = True


def _route_faq(state: TurnState) -> None:
    topic = state.collected_fields.get("faq_topic")
    if topic:
        subtopics = _split_faq_topics(topic)
    else:
        subtopics = [None]

    for t in subtopics:
        result = tools.get_office_info(t)
        state.tool_results.append(result)
    state.is_complete = True


def _pick_appointment_for_change(
    appt_list: list[dict[str, Any]],
    fields: dict[str, Any],
    user_message: str,
    persisted_offered_ids: list[int],
) -> tuple[int | None, str | None]:
    """Returns (appointment_id, None) or (None, 'not_found'|'list')."""
    sel, err = resolve_appointment_selection_full(
        appt_list,
        fields,
        user_message,
        persisted_offered_ids,
        _normalized_time_from_fields_or_message,
    )
    if err == "not_found":
        return None, "not_found"
    if sel is not None:
        return sel, None
    if len(appt_list) == 1:
        return appt_list[0]["id"], None
    return None, "list"


def _unique_reschedule_source_by_type(
    appt_list: list[dict[str, Any]],
    fields: dict[str, Any],
) -> int | None:
    """If destination date/time is new, use a unique appointment_type match as the source visit."""
    target_hint_present = bool(fields.get("date_preference") or fields.get("date_resolved") or fields.get("selected_time"))
    if not target_hint_present:
        return None
    appt_type = coerce_appointment_type(fields.get("appointment_type"))
    if appt_type == "unknown":
        return None
    matches = [a["id"] for a in appt_list if str(a.get("appointment_type") or "").lower() == appt_type]
    if len(matches) == 1:
        return matches[0]
    return None


def _route_book_new(state: TurnState, conn: Any) -> None:
    fields = state.collected_fields
    phone = fields.get("phone")
    if not phone:
        return

    patient_result = tools.lookup_patient(conn, phone)
    if patient_result["ok"]:
        state.patient = patient_result["patient"]
        if not fields.get("identity_verified"):
            return
    else:
        name = fields.get("name", "")
        if name:
            reg = tools.register_patient(conn, name, phone, fields.get("dob"), fields.get("insurance"))
            state.tool_results.append(reg)
            if reg["ok"]:
                state.patient = reg["patient"]
                fields["identity_verified"] = True
            else:
                return
        else:
            return

    if not state.patient:
        return

    date_key = _slot_date_key(fields)
    if state.slots_offered_for_date and date_key and state.slots_offered_for_date != date_key:
        _clear_slot_offer(state)

    avail = _check_availability_excluding(conn, state, limit=AVAILABILITY_PIPELINE_DEFAULT_LIMIT)
    if not avail["ok"]:
        state.tool_results.append(avail)
        return

    slots = avail["slots"]
    urgent = state.is_emergency or _urgent_clinical_context(fields, state.user_message)
    if urgent:
        state.is_emergency = True
        state.tone = "emergency"
    appt_type = "emergency" if urgent else coerce_appointment_type(fields.get("appointment_type"))

    chosen = _resolve_offered_slot(slots, state.offered_slot_ids, fields, state.user_message)
    if chosen and state.offered_slot_ids:
        visit_notes = build_visit_notes_from_fields(fields)
        es = (fields.get("symptoms") or fields.get("notes")) if urgent else None
        result = tools.book_appointment(
            conn, state.patient["id"], chosen["id"], appt_type,
            is_emergency=urgent,
            emergency_summary=es,
            visit_notes=visit_notes,
        )
        state.tool_results.append(result)
        if result["ok"]:
            result["insurance_on_file"] = state.patient.get("insurance")
            state.is_complete = True
            _clear_slot_offer(state)
        return

    if state.offered_slot_ids:
        extra = _find_slot_outside_offered_shortlist(
            conn, slots, state.offered_slot_ids, fields, state.user_message,
        )
        if extra:
            state.tool_results.append(_payload_requested_time_found(extra, reschedule=False))
            state.offered_slot_ids = [extra["id"]]
            state.slots_offered_for_date = date_key or None
            return

    offered = slots[:MAX_SLOTS_OFFERED_TO_USER]
    state.tool_results.append(_availability_payload(avail, offered))
    state.offered_slot_ids = [s["id"] for s in offered]
    state.slots_offered_for_date = date_key or None


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
    if not appts["ok"]:
        state.tool_results.append(appts)
        return
    appt_list = appts["appointments"]
    if not appt_list:
        state.tool_results.append({"ok": False, "error": "No upcoming appointments to cancel."})
        return

    fields = state.collected_fields
    ids = [a["id"] for a in appt_list]
    picked, err = _pick_appointment_for_change(
        appt_list, fields, state.user_message, state.offered_appointment_ids,
    )
    if err == "not_found":
        state.tool_results.append({
            "ok": False,
            "error": (
                "We could not find an upcoming appointment matching that date or time. "
                "Here are your confirmed visits — please pick one or describe it using the date and time shown."
            ),
            "appointment_not_found": True,
            "appointments": appt_list,
        })
        state.offered_appointment_ids = ids
        return
    if err == "list":
        state.tool_results.append(_appointment_selection_tool_result(
            appt_list,
            "You have more than one upcoming appointment. Which one would you like to cancel?",
        ))
        state.offered_appointment_ids = ids
        return
    cancel_id = picked
    if len(appt_list) == 1:
        _clear_appointment_offer(state)

    result = tools.cancel_appointment(conn, cancel_id)
    state.tool_results.append(result)
    if result["ok"]:
        state.is_complete = True
        _clear_appointment_offer(state)


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
    if not appts["ok"]:
        state.tool_results.append(appts)
        return
    appt_list = appts["appointments"]
    if not appt_list:
        state.tool_results.append({"ok": False, "error": "No upcoming appointments to reschedule."})
        return

    fields = state.collected_fields
    ids = [a["id"] for a in appt_list]
    picked, err = _pick_appointment_for_change(
        appt_list, fields, state.user_message, state.offered_appointment_ids,
    )
    if err == "not_found":
        fallback_id = _unique_reschedule_source_by_type(appt_list, fields)
        if fallback_id is not None:
            appt_id = fallback_id
            err = None
        else:
            state.tool_results.append({
                "ok": False,
                "error": (
                    "We could not find an upcoming appointment matching that date or time. "
                    "Please pick from your confirmed visits or use the date and time from the list."
                ),
                "appointment_not_found": True,
                "appointments": appt_list,
            })
            state.offered_appointment_ids = ids
            return
    elif err == "list":
        state.tool_results.append(_appointment_selection_tool_result(
            appt_list,
            "You have more than one upcoming appointment. Which one should we move?",
        ))
        state.offered_appointment_ids = ids
        return
    else:
        appt_id = picked
    _clear_appointment_offer(state)
    source_appt = next((a for a in appt_list if a["id"] == appt_id), None)

    date_key = _slot_date_key(state.collected_fields)
    if state.slots_offered_for_date and date_key and state.slots_offered_for_date != date_key:
        _clear_slot_offer(state)

    avail = _check_availability_excluding(conn, state, limit=AVAILABILITY_PIPELINE_DEFAULT_LIMIT)
    if not avail["ok"]:
        state.tool_results.append(avail)
        return

    slots = avail["slots"]

    chosen = _resolve_offered_slot(slots, state.offered_slot_ids, state.collected_fields, state.user_message)
    if chosen and state.offered_slot_ids:
        result = tools.reschedule_appointment(conn, appt_id, chosen["id"])
        state.tool_results.append(result)
        if result["ok"]:
            state.is_complete = True
            _clear_slot_offer(state)
            _clear_appointment_offer(state)
        return

    if state.offered_slot_ids:
        extra = _find_slot_outside_offered_shortlist(
            conn, slots, state.offered_slot_ids, state.collected_fields, state.user_message,
        )
        if extra:
            payload = _payload_requested_time_found(extra, reschedule=True)
            if source_appt:
                payload["current_appointment"] = source_appt
            state.tool_results.append(payload)
            state.offered_slot_ids = [extra["id"]]
            state.slots_offered_for_date = date_key or None
            return

    offered = slots[:MAX_SLOTS_OFFERED_TO_USER]
    payload = {**_availability_payload(avail, offered), "reschedule": True}
    if source_appt:
        payload["current_appointment"] = source_appt
    state.tool_results.append(payload)
    state.offered_slot_ids = [s["id"] for s in offered]
    state.slots_offered_for_date = date_key or None


def _route_emergency(state: TurnState, conn: Any) -> None:
    state.is_emergency = True
    state.tone = "emergency"
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
        else:
            avail = _emergency_availability(conn)
            if avail["ok"]:
                offered = avail["slots"][:MAX_SLOTS_OFFERED_TO_USER]
                state.tool_results.append({
                    **_availability_payload(avail, offered),
                    "emergency": True,
                    "need_patient_name": True,
                    "message": (
                        "We do not have a chart for that phone yet. Share the name we should use "
                        "so we can hold an urgent slot—nothing is booked until you confirm a time."
                    ),
                })
                state.offered_slot_ids = [s["id"] for s in offered]
                state.slots_offered_for_date = offered[0]["date"] if offered else None
            else:
                state.tool_results.append(avail)
            return

    if not state.patient:
        return

    avail = _emergency_availability(conn)
    if not avail["ok"]:
        state.tool_results.append(avail)
        return

    slots = avail["slots"]
    date_key = slots[0]["date"] if slots else ""
    if state.slots_offered_for_date and date_key and state.slots_offered_for_date != date_key:
        _clear_slot_offer(state)

    es = fields.get("symptoms") or fields.get("notes") or "Emergency"

    chosen = _resolve_offered_slot(slots, state.offered_slot_ids, fields, state.user_message)
    if chosen and state.offered_slot_ids:
        result = tools.book_appointment(
            conn, state.patient["id"], chosen["id"], "emergency",
            is_emergency=True,
            emergency_summary=es,
        )
        state.tool_results.append(result)
        if result["ok"]:
            result["insurance_on_file"] = state.patient.get("insurance")
            state.is_complete = True
            _clear_slot_offer(state)
        return

    if state.offered_slot_ids:
        extra = _find_slot_outside_offered_shortlist(
            conn, slots, state.offered_slot_ids, fields, state.user_message,
        )
        if extra:
            state.tool_results.append(_payload_requested_time_found(extra, reschedule=False))
            state.offered_slot_ids = [extra["id"]]
            state.slots_offered_for_date = date_key or None
            return
        slot_by_id = {s["id"]: s for s in slots}
        shown = [slot_by_id[i] for i in state.offered_slot_ids if i in slot_by_id]
        if shown:
            state.tool_results.append({
                "ok": True,
                "slots": shown,
                "awaiting_selection": True,
                "emergency": True,
                "message": "Please pick a time below to confirm your urgent visit.",
                "timezone_note": "All times are US Pacific (PT).",
            })
            return

    offered = slots[:MAX_SLOTS_OFFERED_TO_USER]
    state.tool_results.append({**_availability_payload(avail, offered), "emergency": True})
    state.offered_slot_ids = [s["id"] for s in offered]
    state.slots_offered_for_date = date_key or None


def _route_family_book(state: TurnState, conn: Any) -> None:
    fields = state.collected_fields
    phone = fields.get("phone")
    if not phone:
        return

    patient_result = tools.lookup_patient(conn, phone)
    if patient_result["ok"]:
        state.patient = patient_result["patient"]
        if not fields.get("identity_verified"):
            return
    else:
        name = fields.get("name", "")
        if name:
            reg = tools.register_patient(conn, name, phone, fields.get("dob"), fields.get("insurance"))
            state.tool_results.append(reg)
            if reg["ok"]:
                state.patient = reg["patient"]
                fields["identity_verified"] = True
            else:
                return
        else:
            return

    if not state.patient:
        return

    family_size = int(fields.get("family_size", 1))
    family_names = fields.get("family_member_names")
    urgent = state.is_emergency or _urgent_clinical_context(fields, state.user_message)
    if urgent:
        state.is_emergency = True
        state.tone = "emergency"
    appt_type = "emergency" if urgent else coerce_appointment_type(fields.get("appointment_type"))
    avail = _check_availability_excluding(conn, state, limit=family_size * 6)
    if not avail["ok"]:
        state.tool_results.append(avail)
        return

    slots = avail["slots"]

    date_key = _slot_date_key(fields)
    if state.slots_offered_for_date and date_key and state.slots_offered_for_date != date_key:
        _clear_slot_offer(state)

    date_iso = _resolved_booking_date_iso(fields)

    if state.pending_family_size == family_size and state.offered_slot_ids:
        offered_start = state.offered_slot_ids[0]
        chosen = _resolve_offered_slot(slots, state.offered_slot_ids, fields, state.user_message)
        offered_slot = q.find_slot_by_id(conn, offered_start)
        offered_start_date = offered_slot["date"] if offered_slot else None
        offered_start_time = offered_slot["time"] if offered_slot else None
        if (chosen and chosen["id"] == offered_start) or (_affirmative_message(state.user_message) and offered_start_time):
            fresh = None
            if offered_start_date and offered_start_time:
                fresh = find_consecutive_block_starting_at(
                    conn, offered_start_date, offered_start_time, family_size, state.rejected_slots,
                )
            if fresh is None:
                fresh = _find_consecutive_from_start(slots, offered_start, family_size)
            if fresh and len(fresh) == family_size:
                vn = build_visit_notes_from_fields(fields)
                es = (fields.get("symptoms") or fields.get("notes")) if urgent else None
                for slot in fresh:
                    result = tools.book_appointment(
                        conn, state.patient["id"], slot["id"], appt_type,
                        is_emergency=urgent,
                        emergency_summary=es,
                        visit_notes=vn,
                    )
                    if result["ok"]:
                        result["insurance_on_file"] = state.patient.get("insurance")
                    state.tool_results.append(result)
                    state.family_members.append({"slot": slot, "booked": result["ok"]})
                state.is_complete = all(m["booked"] for m in state.family_members)
                if state.is_complete:
                    _clear_slot_offer(state)
                return
        alt_t = _normalized_time_from_fields_or_message(fields, state.user_message)
        if alt_t and date_iso:
            alt_group = find_consecutive_block_starting_at(
                conn, date_iso, alt_t, family_size, state.rejected_slots,
            )
            if alt_group:
                state.tool_results.append({
                    **_availability_payload(avail, alt_group),
                    "family_size": family_size,
                    "family_member_names": family_names,
                    "family_block_pt": {
                        "family_size": family_size,
                        "start": f"{alt_group[0]['date']} {alt_group[0]['time']}",
                        "times": [f"{s['date']} {s['time']}" for s in alt_group],
                    },
                    "requested_time_matched": True,
                    "message": (
                        f"{family_size} back-to-back slots starting {alt_group[0]['time']} on {alt_group[0]['date']} PT — "
                        "confirm to book."
                    ),
                })
                state.offered_slot_ids = [alt_group[0]["id"]]
                state.slots_offered_for_date = date_key or None
                return

    consecutive = _find_consecutive_slots(slots, family_size)
    if not consecutive:
        state.tool_results.append({"ok": False, "error": f"Could not find {family_size} back-to-back slots. Try a different date?"})
        return

    start_id = consecutive[0]["id"]
    if state.offered_slot_ids and state.offered_slot_ids != [start_id]:
        _clear_slot_offer(state)

    state.tool_results.append({
        **_availability_payload(avail, consecutive),
        "family_size": family_size,
        "family_member_names": family_names,
        "family_block_pt": {
            "family_size": family_size,
            "start": f"{consecutive[0]['date']} {consecutive[0]['time']}",
            "times": [f"{s['date']} {s['time']}" for s in consecutive],
        },
        "message": (
            f"Here are {family_size} back-to-back times starting at {consecutive[0]['time']} on {consecutive[0]['date']}. "
            "Reply yes, pick that start time, or say first to confirm — nothing is booked until you confirm."
        ),
    })
    state.offered_slot_ids = [start_id]
    state.pending_family_size = family_size
    state.slots_offered_for_date = date_key or None


_ROUTERS = {
    "faq":                  lambda state, conn: _route_faq(state),
    "appointment_status":   _route_appointment_status,
    "book_new":             _route_book_new,
    "cancel":               _route_cancel,
    "reschedule":           _route_reschedule,
    "emergency":            _route_emergency,
    "family_book":          _route_family_book,
}


def _run_router(state: TurnState, conn: Any) -> None:
    """Deterministic routing based on collected_fields, not orchestrator decisions."""
    if state.is_complete:
        return

    workflow = state.workflow

    if state.collected_fields.get("slots_rejected"):
        for slot_id in state.offered_slot_ids:
            if slot_id not in state.rejected_slots:
                state.rejected_slots.append(slot_id)
        del state.collected_fields["slots_rejected"]
        _clear_slot_offer(state)

    _hydrate_existing_patient_profile(state, conn)

    pre_blocks = False
    if workflow in ("book_new", "family_book"):
        pre_blocks = book_new_or_family_preflight(state, conn, workflow)

    if not pre_blocks and _is_ready(workflow, state.collected_fields):
        router_fn = _ROUTERS.get(workflow)
        if router_fn:
            router_fn(state, conn)

    if not state.is_complete and not state.tool_results:
        state.questions_to_ask = select_questions(
            workflow,
            state.collected_fields,
            max_questions=max_questions_for_workflow(workflow, state.collected_fields),
        )
    elif not state.is_complete and state.tool_results and any(
        isinstance(r, dict) and r.get("awaiting_appointment_selection") for r in state.tool_results
    ):
        state.questions_to_ask = [FIELD_QUESTIONS["appointment_choice"]]
    elif not state.is_complete and state.tool_results and any(
        isinstance(r, dict) and r.get("need_patient_name") for r in state.tool_results
    ):
        state.questions_to_ask = [FIELD_QUESTIONS["name"]]
    elif not state.is_complete and state.tool_results and any(
        isinstance(r, dict) and r.get("awaiting_selection") for r in state.tool_results
    ):
        state.questions_to_ask = [FIELD_QUESTIONS["slot_choice"]]
    elif not state.is_complete and state.tool_results and any(
        isinstance(r, dict) and r.get("awaiting_identity_confirmation") for r in state.tool_results
    ):
        state.questions_to_ask = [FIELD_QUESTIONS["identity_confirm"]]

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
        summary = _recent_summary(conn, cid)
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
            is_complete=persisted.get("is_complete", False),
            offered_slot_ids=persisted.get("offered_slot_ids", []),
            slots_offered_for_date=persisted.get("slots_offered_for_date"),
            pending_family_size=persisted.get("pending_family_size"),
            offered_appointment_ids=persisted.get("offered_appointment_ids", []),
        )

        if not settings.llm_ready:
            reply = (
                "Set LLM_API_KEY (and LLM_PROVIDER / LLM_MODEL) in your environment "
                "to enable AI responses. See .env.template."
            )
            debug_meta: dict[str, Any] = {"turn_state": state.model_dump()}
            persisted_meta = {"turn_state": _persistable_turn_state(state)}
            save_message(conn, cid, "assistant", reply, json.dumps(persisted_meta))
            return cid, reply, debug_meta

        prior_ctx = _orchestrator_prior_context(persisted)
        try:
            orch = run_orchestrator(user_message, summary, prior_ctx)
        except Exception as exc:
            logger.exception("Orchestrator failed")
            orch = OrchestratorOutput()
            state.orchestrator_output["error"] = _sanitize_error(str(exc))

        state.orchestrator_output = orch.model_dump()
        preserve_active_workflow = _should_preserve_active_workflow(persisted, orch.intent, user_message)

        if persisted.get("is_complete") and orch.intent not in ("unknown", "general"):
            carry = _identity_carry_fields(state)
            state.is_complete = False
            state.workflow = orch.intent
            state.collected_fields = carry
            state.tool_results = []
            state.rejected_slots = []
            _clear_slot_offer(state)
            _clear_appointment_offer(state)
            state.is_emergency = orch.intent == "emergency"
            if orch.intent != "emergency":
                state.emergency_logged = False

        if (
            not preserve_active_workflow
            and orch.intent not in ("unknown", "general")
            and state.workflow not in ("unknown", orch.intent)
        ):
            state.workflow = orch.intent
            state.collected_fields = _identity_carry_fields(state)
            state.tool_results = []
            state.rejected_slots = []
            _clear_slot_offer(state)
            _clear_appointment_offer(state)
            state.is_complete = False
            if orch.intent != "emergency":
                state.emergency_logged = False

        if preserve_active_workflow:
            state.orchestrator_output["intent_overridden"] = persisted.get("workflow", state.workflow)
            state.workflow = persisted.get("workflow", state.workflow)
        elif state.workflow == "unknown" or orch.intent != "unknown":
            state.workflow = orch.intent
        state.tone = orch.tone
        if orch.intent == "emergency":
            state.is_emergency = True
        elif orch.intent in ORCHESTRATOR_INTENTS_ROUTINE:
            state.is_emergency = False
        # "unknown" / "general": keep persisted is_emergency (mid emergency flow).
        _clear_stale_time_hints_if_needed(state.collected_fields, orch.extracted_fields, user_message)
        _merge_fields(state.collected_fields, orch.extracted_fields)
        if orch.intent in ("book_new", "family_book"):
            state.collected_fields.pop("symptoms", None)
        _apply_clinical_urgency(state, user_message)

        _coerce_workflow_for_appointment_lookup(state, user_message, summary)
        if state.workflow == "appointment_status":
            state.orchestrator_output["intent"] = "appointment_status"
            if persisted.get("is_complete"):
                state.is_complete = False
                state.tool_results = []
                state.rejected_slots = []
                _clear_slot_offer(state)
                _clear_appointment_offer(state)

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

        debug_meta = {"turn_state": state.model_dump()}
        persisted_meta = {"turn_state": _persistable_turn_state(state)}
        save_message(conn, cid, "assistant", reply, json.dumps(persisted_meta))

    return cid, reply, debug_meta
