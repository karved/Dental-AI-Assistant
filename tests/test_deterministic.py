"""Deterministic test suite — no LLM calls, no API key needed.

Covers: safety, date resolution, question selection, readiness, phone
normalization, DB tools, router internals, and state persistence.
"""

from __future__ import annotations

import json
import tempfile
from datetime import date, timedelta
from typing import Any

import pytest

from dental_assistant.application import engine as engine_mod
from dental_assistant.application.appointment_resolution import (
    _resolve_offered_appointment_id,
    resolve_appointment_selection_full,
)
from dental_assistant.application.family_booking import find_consecutive_block_starting_at
from dental_assistant.application.patient_identity import identity_confirmation_reply, name_match_tier
from dental_assistant.application.visit_notes import build_visit_notes_from_fields
from dental_assistant.application.engine import (
    _check_availability_excluding,
    _orchestrator_prior_context,
    _pending_interaction_state,
    _persistable_turn_state,
    _unique_reschedule_source_by_type,
    _urgent_clinical_context,
    _coerce_workflow_for_appointment_lookup,
    _drop_today_slots_if_closed_pt,
    _find_slot_outside_offered_shortlist,
    _find_consecutive_slots,
    _filter_by_time_preference,
    _hydrate_existing_patient_profile,
    _is_ready,
    _keyword_safety_check,
    _load_persisted_state,
    _merge_fields,
    _resolve_offered_slot,
    _route_book_new,
    _route_cancel,
    _route_emergency,
    _route_family_book,
    _route_reschedule,
    _run_router,
    _sanitize_error,
    _should_preserve_active_workflow,
    _split_faq_topics,
    process_message,
)
from dental_assistant.application.conversation import _decorate_display
from dental_assistant.domain.appointments import coerce_appointment_type, visit_summary_for_chat
from dental_assistant.domain.constants import (
    AVAILABILITY_PIPELINE_DEFAULT_LIMIT,
    BLOCKED_PHRASES,
    FIELD_QUESTIONS,
    SAFETY_RESPONSE,
)
from dental_assistant.domain.date_resolver import resolve_date, resolve_date_range
from dental_assistant.domain.models import OrchestratorOutput, TurnState
from dental_assistant.domain.pt_time import pt_today, pt_today_iso, same_day_booking_closed_result
from dental_assistant.domain.time_parse import normalized_time_from_fields_or_message
from dental_assistant.domain.question_selector import max_questions_for_workflow, select_questions
from dental_assistant.infrastructure import queries as q
from dental_assistant.infrastructure.tools import (
    book_appointment,
    cancel_appointment,
    check_availability,
    get_office_info,
    get_patient_appointments,
    lookup_patient,
    normalize_phone,
    register_patient,
    reschedule_appointment,
    save_feedback,
)

# ---------------------------------------------------------------------------
# Fixture: fresh temp DB per test
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_conn():
    from dental_assistant.infrastructure.db import init_db, connection

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name

    init_db(path)
    with connection(path) as conn:
        yield conn


def _turn(collected_fields: dict[str, Any], **kwargs: Any) -> TurnState:
    return TurnState(conversation_id=1, user_message="t", collected_fields=collected_fields, **kwargs)


# ═══════════════════════════════════════════════════════════════════════════
# A. Safety Layer
# ═══════════════════════════════════════════════════════════════════════════

class TestSafety:
    def test_clean_message_passes(self):
        assert _keyword_safety_check("I'd like to book a cleaning") is None

    def test_blocked_phrases(self):
        for phrase in BLOCKED_PHRASES:
            assert _keyword_safety_check(phrase) == SAFETY_RESPONSE

    def test_mixed_case_and_embedded(self):
        assert _keyword_safety_check("I want to KILL MYSELF") is not None
        assert _keyword_safety_check("please help i want to hurt myself badly") is not None

    def test_sensitive_but_allowed(self):
        assert _keyword_safety_check("I have a killer toothache") is None


# ═══════════════════════════════════════════════════════════════════════════
# B. Date Resolver
# ═══════════════════════════════════════════════════════════════════════════

class TestDateResolver:
    @pytest.mark.parametrize("expr", ["today", "now", "asap", "tonight"])
    def test_today_variants(self, expr):
        assert resolve_date(expr) == pt_today().isoformat()

    def test_tomorrow(self):
        assert resolve_date("tomorrow") == (pt_today() + timedelta(days=1)).isoformat()

    def test_next_week_is_monday(self):
        result = date.fromisoformat(resolve_date("next week"))
        assert result.weekday() == 0 and result > pt_today()

    @pytest.mark.parametrize("expr,weekday", [
        ("next monday", 0), ("next friday", 4),
        ("wednesday", 2), ("fri", 4), ("mon", 0),
        ("this wednesday", 2),
    ])
    def test_weekday_resolution(self, expr, weekday):
        result = date.fromisoformat(resolve_date(expr))
        assert result.weekday() == weekday
        assert result >= pt_today()

    def test_early_this_week(self):
        result = date.fromisoformat(resolve_date("early this week"))
        assert result >= pt_today()

    def test_later_this_week(self):
        result = date.fromisoformat(resolve_date("later this week"))
        assert result >= pt_today()

    def test_early_next_month_is_business_day(self):
        result = date.fromisoformat(resolve_date("early next month"))
        assert result.weekday() <= 4

    def test_mid_next_week_is_wednesday(self):
        result = date.fromisoformat(resolve_date("mid next week"))
        assert result.weekday() == 2
        assert result > pt_today()

    def test_later_next_week_is_thursday(self):
        result = date.fromisoformat(resolve_date("later next week"))
        assert result.weekday() == 3
        assert result > pt_today()

    def test_mid_next_month_is_mid_month_business_day(self):
        result = date.fromisoformat(resolve_date("mid next month"))
        assert 15 <= result.day <= 17
        assert result.weekday() <= 4

    def test_late_next_month_is_business_day(self):
        result = date.fromisoformat(resolve_date("late next month"))
        assert result.weekday() <= 4

    def test_next_week_range_spans_multiple_days(self):
        start, end = resolve_date_range("next week")
        assert start < end

    def test_later_next_week_range_starts_thursday(self):
        start, end = resolve_date_range("later next week")
        assert date.fromisoformat(start).weekday() == 3
        assert start < end

    def test_next_month_range_covers_month(self):
        start, end = resolve_date_range("next month")
        assert start.startswith("2026-05") or date.fromisoformat(start).month != pt_today().month
        assert date.fromisoformat(start) < date.fromisoformat(end)

    def test_named_month_resolves_current_or_upcoming_month(self):
        assert resolve_date("april") == pt_today().isoformat()
        assert resolve_date("may") == "2026-05-01"

    def test_named_month_range_variants(self):
        assert resolve_date_range("in april") == (pt_today().isoformat(), "2026-04-30")
        assert resolve_date_range("may month") == ("2026-05-01", "2026-05-31")

    def test_past_iso_date_returns_none(self):
        assert resolve_date("2026-04-01") is None

    def test_april_end_resolves_to_late_april(self):
        assert resolve_date("april end") == "2026-04-21"
        assert resolve_date_range("end of april") == ("2026-04-21", "2026-04-30")

    def test_month_day_phrase(self):
        assert resolve_date("April 7") == "2026-04-07"
        assert resolve_date_range("April 7") == ("2026-04-07", "2026-04-07")

    def test_month_end_with_punctuation(self):
        assert resolve_date("May end?") == "2026-05-21"

    def test_fuzzy_weekday_with_punctuation(self):
        result = date.fromisoformat(resolve_date("Thursdya/"))
        assert result.weekday() == 3

    def test_week_after(self):
        start, end = resolve_date_range("week after")
        assert date.fromisoformat(start).weekday() == 0
        assert (date.fromisoformat(start) - pt_today()).days >= 7
        assert start < end

    def test_early_named_month(self):
        assert resolve_date("early june") == "2026-06-01"

    def test_iso_passthrough(self):
        assert resolve_date("2026-04-10") == "2026-04-10"

    @pytest.mark.parametrize("expr", ["asdfgh", "sure", "yes", "", "   "])
    def test_unresolvable_returns_none(self, expr):
        assert resolve_date(expr) is None


# ═══════════════════════════════════════════════════════════════════════════
# C. Question Selector
# ═══════════════════════════════════════════════════════════════════════════

class TestQuestionSelector:
    def test_book_new_empty_default_two_prompts(self):
        qs = select_questions("book_new", {})
        assert len(qs) == 2
        assert "name" in qs[0].lower()

    @pytest.mark.parametrize("workflow,collected,expected", [
        ("book_new", {}, 1),
        ("book_new", {"name": "A"}, 2),
        ("book_new", {"phone": "5550101001"}, 2),
        ("book_new", {"name": "A", "phone": "B"}, 2),
        ("family_book", {}, 1),
        ("reschedule", {}, 2),
    ])
    def test_max_questions_for_workflow(self, workflow, collected, expected):
        assert max_questions_for_workflow(workflow, collected) == expected

    def test_book_new_with_basics_asks_dob_insurance(self):
        qs = select_questions("book_new", {"name": "X", "phone": "Y"})
        assert len(qs) == 2
        assert "date of birth" in qs[0].lower() or "dob" in qs[0].lower()

    def test_book_new_all_collected(self):
        qs = select_questions("book_new", {
            "name": "X", "phone": "Y", "dob": "Z", "insurance": "W",
            "date_preference": "A", "appointment_type": "B",
        })
        assert qs == []

    def test_emergency_asks_symptoms_then_phone(self):
        assert len(select_questions("emergency", {})) == 1
        assert select_questions("emergency", {"symptoms": "pain"}) == [FIELD_QUESTIONS["phone"]]
        assert select_questions("emergency", {"symptoms": "pain", "phone": "5550101001"}) == []

    def test_cancel_with_phone_ready(self):
        assert select_questions("cancel", {"phone": "X"}) == []

    def test_appointment_status_with_phone_ready(self):
        assert select_questions("appointment_status", {"phone": "X"}) == []

    def test_max_questions_respected(self):
        assert len(select_questions("book_new", {}, max_questions=1)) == 1

    def test_family_book_asks_appointment_type_after_date(self):
        base = {"name": "A", "phone": "B", "family_size": 2, "family_member_names": ["Sam", "Ava"], "date_preference": "next week"}
        qs = select_questions("family_book", base, max_questions=1)
        assert qs == [FIELD_QUESTIONS["appointment_type"]]

    def test_family_book_asks_for_names_after_size(self):
        base = {"name": "A", "phone": "B", "family_size": 2}
        qs = select_questions("family_book", base, max_questions=1)
        assert qs == [FIELD_QUESTIONS["family_member_names"]]

    def test_family_book_asks_for_names_when_only_relationship_labels_known(self):
        base = {"name": "A", "phone": "B", "family_size": 2, "family_member_names": ["wife", "son"]}
        qs = select_questions("family_book", base, max_questions=1)
        assert qs == [FIELD_QUESTIONS["family_member_names"]]


# ═══════════════════════════════════════════════════════════════════════════
# D. Readiness Rules
# ═══════════════════════════════════════════════════════════════════════════

class TestReadiness:
    @pytest.mark.parametrize("workflow,fields,expected", [
        ("book_new", {"name": "A", "phone": "B", "date_preference": "C"}, True),
        ("book_new", {"name": "A"}, False),
        ("reschedule", {"phone": "X"}, True),
        ("cancel", {"phone": "X"}, True),
        ("family_book", {"name": "A", "phone": "B", "family_size": 2, "date_preference": "C", "appointment_type": "checkup"}, True),
        ("family_book", {"name": "A", "phone": "B"}, False),
        ("emergency", {}, False),
        ("emergency", {"phone": "5550101001"}, True),
        ("faq", {"faq_topic": "hours"}, True),
        ("faq", {}, False),
        ("appointment_status", {"phone": "555-010-1001"}, True),
        ("appointment_status", {}, False),
        ("nonexistent", {}, False),
    ])
    def test_matrix(self, workflow, fields, expected):
        assert _is_ready(workflow, fields) is expected


# ═══════════════════════════════════════════════════════════════════════════
# E. Phone Normalization
# ═══════════════════════════════════════════════════════════════════════════

class TestPhoneNormalization:
    @pytest.mark.parametrize("raw,expected", [
        ("5550101001", "555-010-1001"),
        ("555-010-1001", "555-010-1001"),
        ("(555) 010-1001", "555-010-1001"),
        ("+1 555 010 1001", "555-010-1001"),
        ("1-555-010-1001", "555-010-1001"),
    ])
    def test_valid_phones(self, raw, expected):
        phone, err = normalize_phone(raw)
        assert phone == expected and err is None

    @pytest.mark.parametrize("raw,err_substr", [
        ("123", "10 digits"),
        ("", "required"),
        ("12345678901234", "10 digits"),
    ])
    def test_invalid_phones(self, raw, err_substr):
        phone, err = normalize_phone(raw)
        assert phone is None and err_substr in err


# ═══════════════════════════════════════════════════════════════════════════
# F. Tools — Patient CRUD
# ═══════════════════════════════════════════════════════════════════════════

class TestToolsPatient:
    def test_lookup_existing(self, db_conn):
        r = lookup_patient(db_conn, "5550101001")
        assert r["ok"] and r["patient"]["name"] == "Sarah Johnson"

    def test_lookup_unknown(self, db_conn):
        assert not lookup_patient(db_conn, "5550109999")["ok"]

    def test_register_and_duplicate(self, db_conn):
        r = register_patient(db_conn, "Test User", "5550105555")
        assert r["ok"] and r["patient"]["phone"] == "555-010-5555"
        assert not register_patient(db_conn, "Dup", "5550105555")["ok"]


# ═══════════════════════════════════════════════════════════════════════════
# G. Tools — Booking Flow
# ═══════════════════════════════════════════════════════════════════════════

class TestToolsBooking:
    @staticmethod
    def _patient_and_slot(db_conn):
        p = lookup_patient(db_conn, "5550101001")
        s = check_availability(db_conn, limit=1)
        return p["patient"]["id"], s["slots"][0]["id"]

    def test_book_and_double_book(self, db_conn):
        pid, sid = self._patient_and_slot(db_conn)
        assert book_appointment(db_conn, pid, sid, "cleaning")["ok"]
        assert not book_appointment(db_conn, pid, sid, "checkup")["ok"]

    def test_book_invalid_type(self, db_conn):
        pid, sid = self._patient_and_slot(db_conn)
        assert not book_appointment(db_conn, pid, sid, "brain_surgery")["ok"]

    def test_reschedule(self, db_conn):
        pid, sid = self._patient_and_slot(db_conn)
        booked = book_appointment(db_conn, pid, sid, "cleaning")
        assert booked["appointment"].get("modified_at")
        new_sid = [s for s in check_availability(db_conn, limit=5)["slots"] if s["id"] != sid][0]["id"]
        r = reschedule_appointment(db_conn, booked["appointment"]["id"], new_sid)
        assert r["ok"] and r["appointment"]["slot_id"] == new_sid
        assert r.get("insurance_on_file")
        assert r["appointment"].get("modified_at")

    def test_cancel(self, db_conn):
        pid, sid = self._patient_and_slot(db_conn)
        booked = book_appointment(db_conn, pid, sid, "cleaning")
        c = cancel_appointment(db_conn, booked["appointment"]["id"])
        assert c["ok"] and c.get("insurance_on_file")
        assert not cancel_appointment(db_conn, 99999)["ok"]

    def test_get_patient_appointments(self, db_conn):
        pid, sid = self._patient_and_slot(db_conn)
        book_appointment(db_conn, pid, sid, "cleaning")
        r = get_patient_appointments(db_conn, pid)
        assert r["ok"] and len(r["appointments"]) >= 1
        assert all("visit_summary" in a for a in r["appointments"])
        assert r.get("insurance_on_file")

    def test_get_patient_appointments_includes_note_context(self, db_conn):
        pid, sid = self._patient_and_slot(db_conn)
        book_appointment(
            db_conn, pid, sid, "checkup",
            visit_notes="Follow-up checkup after crown prep.",
        )
        r = get_patient_appointments(db_conn, pid)
        assert r["ok"]
        row = next(a for a in r["appointments"] if "crown" in a.get("visit_summary", "").lower())
        assert "Checkup" in row["visit_summary"]


# ═══════════════════════════════════════════════════════════════════════════
# H. Tools — FAQ
# ═══════════════════════════════════════════════════════════════════════════

class TestToolsFAQ:
    @pytest.mark.parametrize("topic,check", [
        ("hours", lambda r: r.get("match_tier") == "exact"),
        ("timings", lambda r: "8:00 AM" in r["answer"]),
        ("financing", lambda r: "financing" in r["answer"].lower()),
    ])
    def test_topic_resolution(self, topic, check):
        r = get_office_info(topic)
        assert r["ok"] and check(r)

    def test_all_topics(self):
        r = get_office_info()
        assert r["ok"] and "topics" in r


# ═══════════════════════════════════════════════════════════════════════════
# I. Tools — Feedback
# ═══════════════════════════════════════════════════════════════════════════

class TestToolsFeedback:
    def test_valid_and_upsert(self, db_conn):
        cid = q.insert_conversation(db_conn)
        assert save_feedback(db_conn, cid, 1)["ok"]
        assert save_feedback(db_conn, cid, -1)["ok"]

    def test_reject_invalid(self, db_conn):
        cid = q.insert_conversation(db_conn)
        assert not save_feedback(db_conn, cid, 5)["ok"]


# ═══════════════════════════════════════════════════════════════════════════
# J. Engine Internals
# ═══════════════════════════════════════════════════════════════════════════

class TestMergeFields:
    def test_overwrite_and_skip_rejected(self):
        c: dict[str, Any] = {"name": "Old"}
        _merge_fields(c, {"name": "New", "slots_rejected": True})
        assert c["name"] == "New"
        assert "slots_rejected" not in c

    def test_resolves_date(self):
        c: dict[str, Any] = {}
        _merge_fields(c, {"date_preference": "tomorrow"})
        assert c["date_resolved"] == (pt_today() + timedelta(days=1)).isoformat()

    def test_unresolvable_date(self):
        c: dict[str, Any] = {}
        _merge_fields(c, {"date_preference": "gibberish"})
        assert "date_resolved" not in c

    def test_infers_family_size_from_names(self):
        c: dict[str, Any] = {}
        _merge_fields(c, {"family_member_names": ["wife", "son"]})
        assert c["family_size"] == 2

    def test_clears_stale_resolved_date_when_new_preference_invalid(self):
        c: dict[str, Any] = {"date_preference": "next month", "date_resolved": "2026-05-01"}
        _merge_fields(c, {"date_preference": "March 30"})
        assert c["date_preference"] == "March 30"
        assert "date_resolved" not in c

    def test_clears_stale_selected_time_when_date_changes_without_new_time(self):
        c: dict[str, Any] = {"date_preference": "april 24", "selected_time": "5pm", "time_preference": "afternoon"}
        engine_mod._clear_stale_time_hints_if_needed(c, {"date_preference": "april 34"}, "Let;s do April 34")
        assert "selected_time" not in c
        assert "time_preference" not in c


class TestAppointmentLookupCoercion:
    @pytest.mark.parametrize("msg,summary", [
        ("do I have an appointment scheduled?", "assistant: hello"),
        ("Yes", "assistant: Would you like me to look for any upcoming appointments for James?"),
    ])
    def test_to_appointment_status(self, msg, summary):
        state = TurnState(
            conversation_id=1,
            user_message=msg,
            workflow="general",
            collected_fields={"phone": "5550101001"},
        )
        _coerce_workflow_for_appointment_lookup(state, msg, summary)
        assert state.workflow == "appointment_status"


class TestBookNewHydrateFromPhone:
    def test_fills_dob_insurance_when_on_file(self, db_conn):
        state = TurnState(
            conversation_id=1,
            user_message="x",
            workflow="book_new",
            collected_fields={
                "name": "James",
                "phone": "5550101004",
                "identity_verified": True,
            },
            patient={},
        )
        _hydrate_existing_patient_profile(state, db_conn)
        assert state.patient.get("id") == 4
        assert state.collected_fields.get("dob") == "2000-06-10"
        assert state.collected_fields.get("insurance") == "Aetna PPO"


class TestSameDayClosedMessage:
    def test_matches_engine_after_hours_shape(self):
        err = same_day_booking_closed_result()
        assert not err["ok"] and err.get("after_hours_pt") is True
        assert "6:00 PM" in err["error"] and "Pacific" in err["error"]


class TestOrchestratorPriorContext:
    def test_whitelists_collected_fields_for_llm1(self):
        persisted = {
            "workflow": "reschedule",
            "is_emergency": False,
            "collected_fields": {
                "phone": "5550101001",
                "date_resolved": "2026-04-08",
                "appointment_type": "cleaning",
                "internal_debug": "should_not_leak",
                "_pending_identity_name": "James Wilson",
            },
            "offered_slot_ids": [1, 2, 3],
        }
        payload = json.loads(_orchestrator_prior_context(persisted))
        assert payload["prior_workflow"] == "reschedule"
        assert payload["collected_fields"]["phone"] == "5550101001"
        assert "internal_debug" not in payload["collected_fields"]
        assert payload["interaction_state"]["awaiting_slot_selection"] is True
        assert payload["interaction_state"]["awaiting_identity_confirmation"] is True


class TestPendingInteractionState:
    def test_compacts_pending_flags(self):
        persisted = {
            "collected_fields": {"_pending_identity_name": "James Wilson"},
            "offered_slot_ids": [1, 2],
            "offered_appointment_ids": [10],
            "slots_offered_for_date": "2026-04-10",
        }
        snapshot = _pending_interaction_state(persisted)
        assert snapshot["awaiting_slot_selection"] is True
        assert snapshot["offered_slot_count"] == 2
        assert snapshot["awaiting_appointment_selection"] is True
        assert snapshot["offered_appointment_count"] == 1
        assert snapshot["awaiting_identity_confirmation"] is True


class TestPreserveActiveWorkflow:
    def test_keeps_mid_flow_workflow_when_orchestrator_is_generic(self):
        persisted = {
            "workflow": "book_new",
            "offered_slot_ids": [1, 2],
            "is_complete": False,
        }
        assert _should_preserve_active_workflow(persisted, "general", "yes") is True

    def test_allows_reset_language_to_break_mid_flow(self):
        persisted = {
            "workflow": "book_new",
            "offered_slot_ids": [1, 2],
            "is_complete": False,
        }
        assert _should_preserve_active_workflow(persisted, "general", "never mind") is False

    def test_family_book_keeps_workflow_on_time_reply(self):
        persisted = {
            "workflow": "family_book",
            "offered_slot_ids": [81],
            "pending_family_size": 1,
            "is_complete": False,
        }
        assert _should_preserve_active_workflow(persisted, "book_new", "2:30 pm") is True


class TestPersistableTurnState:
    def test_omits_transient_debug_payloads(self):
        state = TurnState(
            conversation_id=1,
            user_message="test",
            workflow="book_new",
            collected_fields={"phone": "5550101001"},
            offered_slot_ids=[1, 2],
            tool_results=[{"ok": True, "slots": [{"id": 1}]}],
            questions_to_ask=["Which time works best?"],
            orchestrator_output={"intent": "book_new"},
        )
        snapshot = _persistable_turn_state(state)
        assert snapshot["workflow"] == "book_new"
        assert snapshot["offered_slot_ids"] == [1, 2]
        assert "tool_results" not in snapshot
        assert "questions_to_ask" not in snapshot
        assert "orchestrator_output" not in snapshot


class TestWorkflowIntentReset:
    def test_new_specific_workflow_drops_stale_booking_fields(self):
        state = TurnState(
            conversation_id=1,
            user_message="cancel",
            workflow="book_new",
            patient={"name": "Emily Davis", "phone": "555-010-1003"},
            collected_fields={
                "name": "Emily Davis",
                "phone": "555-010-1003",
                "identity_verified": True,
                "date_preference": "later next week",
                "selected_time": "5pm",
                "appointment_type": "checkup",
            },
        )
        carry = engine_mod._identity_carry_fields(state)
        assert carry == {
            "name": "Emily Davis",
            "phone": "555-010-1003",
            "identity_verified": True,
        }


class TestDropTodaySlotsIfClosed:
    @pytest.mark.parametrize("after_close,expected", [
        (False, None),
        (True, [{"date": "2099-12-31", "id": 2}]),
    ])
    def test_filters_today_when_after_hours(self, monkeypatch, after_close, expected):
        monkeypatch.setattr(engine_mod, "is_past_office_close_pt", lambda: after_close)
        today = pt_today_iso()
        slots = [{"date": today, "id": 1}, {"date": "2099-12-31", "id": 2}]
        out = _drop_today_slots_if_closed_pt(slots)
        assert out == (slots if expected is None else expected)


class TestConsecutiveSlots:
    def test_finds_and_rejects(self):
        slots = [
            {"id": 1, "date": "2026-04-07", "time": "08:00"},
            {"id": 2, "date": "2026-04-07", "time": "08:30"},
            {"id": 3, "date": "2026-04-07", "time": "09:00"},
        ]
        assert _find_consecutive_slots(slots, 2) is not None
        assert _find_consecutive_slots(slots, 3) is not None

        gap = [
            {"id": 1, "date": "2026-04-07", "time": "08:00"},
            {"id": 2, "date": "2026-04-07", "time": "09:00"},
        ]
        assert _find_consecutive_slots(gap, 2) is None


class TestTimePreference:
    def test_filter_buckets_and_exact_clock(self):
        day = [
            {"id": 1, "time": "08:00"},
            {"id": 2, "time": "11:30"},
            {"id": 3, "time": "13:00"},
            {"id": 4, "time": "14:30"},
        ]
        assert len(_filter_by_time_preference(day, "afternoon")) == 2
        assert len(_filter_by_time_preference(day, "morning")) == 2
        assert len(_filter_by_time_preference(day, None)) == 4
        assert _filter_by_time_preference(
            [{"id": 1, "time": "09:00"}, {"id": 2, "time": "17:00"}], "5pm",
        ) == [{"id": 2, "time": "17:00"}]


class TestAvailabilityBroadening:
    def test_broadens_when_date_empty(self, db_conn):
        r = _check_availability_excluding(db_conn, _turn({"date_resolved": "1999-01-01"}))
        assert r["ok"] and r.get("broadened") is True

    def test_resolved_day_includes_late_slots_without_broadening(self, db_conn):
        day = check_availability(db_conn, limit=1)["slots"][0]["date"]
        r = _check_availability_excluding(db_conn, _turn({"date_resolved": day}), limit=15)
        assert r["ok"] and r.get("broadened") is not True
        assert "17:00" in [s["time"] for s in r["slots"]]

    def test_selected_time_filters_like_time_preference(self, db_conn):
        day = check_availability(db_conn, limit=1)["slots"][0]["date"]
        r = _check_availability_excluding(
            db_conn,
            _turn({"date_resolved": day, "selected_time": "5pm"}),
            limit=AVAILABILITY_PIPELINE_DEFAULT_LIMIT,
        )
        assert r["ok"] and [s["time"] for s in r["slots"]] == ["17:00"]

    def test_next_month_uses_range_not_single_day(self, db_conn):
        r = _check_availability_excluding(
            db_conn,
            _turn({"date_preference": "next month"}),
            limit=AVAILABILITY_PIPELINE_DEFAULT_LIMIT,
        )
        assert r["ok"]
        dates = {s["date"] for s in r["slots"]}
        assert all(date.fromisoformat(d) >= date.fromisoformat(resolve_date("next month")) for d in dates)

    def test_same_day_blocked_after_six_pm_pt(self, monkeypatch, db_conn):
        monkeypatch.setattr(engine_mod, "is_past_office_close_pt", lambda: True)
        r = _check_availability_excluding(db_conn, _turn({"date_resolved": pt_today_iso()}))
        assert r == same_day_booking_closed_result()


class TestCancelMultiAppointment:
    def test_disambiguate_then_cancel(self, db_conn):
        pid = lookup_patient(db_conn, "5550101001")["patient"]["id"]
        slots = check_availability(db_conn, limit=5)["slots"]
        assert book_appointment(db_conn, pid, slots[0]["id"], "cleaning")["ok"]
        assert book_appointment(db_conn, pid, slots[1]["id"], "checkup")["ok"]

        state = TurnState(
            conversation_id=1,
            user_message="cancel",
            workflow="cancel",
            collected_fields={"phone": "5550101001"},
        )
        _route_cancel(state, db_conn)
        assert any(r.get("awaiting_appointment_selection") for r in state.tool_results)
        assert len(state.offered_appointment_ids) >= 2

        state2 = TurnState(
            conversation_id=1,
            user_message="first one",
            workflow="cancel",
            collected_fields={"phone": "5550101001"},
            offered_appointment_ids=list(state.offered_appointment_ids),
        )
        _route_cancel(state2, db_conn)
        assert state2.is_complete
        assert any(r.get("ok") and "appointment_id" in r for r in state2.tool_results)


class TestRescheduleSourceSelection:
    def test_unique_type_match_can_identify_source_for_new_target_time(self):
        appts = [
            {"id": 1, "appointment_type": "emergency", "date": "2026-04-06", "time": "08:00"},
            {"id": 2, "appointment_type": "cleaning", "date": "2026-04-06", "time": "13:00"},
        ]
        fields = {"appointment_type": "cleaning", "date_preference": "Wednesday", "selected_time": "5pm"}
        assert _unique_reschedule_source_by_type(appts, fields) == 2

    def test_reschedule_uses_unique_type_as_source_and_offers_target(self, db_conn):
        state = TurnState(
            conversation_id=1,
            user_message="move the cleaning to wednesday 5pm",
            workflow="reschedule",
            collected_fields={
                "phone": "5550101003",
                "appointment_type": "cleaning",
                "date_preference": "Wednesday",
                "date_resolved": "2026-04-08",
                "selected_time": "5pm",
            },
        )
        _route_reschedule(state, db_conn)
        assert not state.is_complete
        payload = next(r for r in state.tool_results if r.get("awaiting_selection"))
        assert payload.get("reschedule") is True
        assert payload.get("current_appointment", {}).get("appointment_type") == "cleaning"
        assert [s["time"] for s in payload["slots"]] == ["17:00"]
        assert state.offered_appointment_ids == []


class TestBookNewUrgentClinical:
    def test_bleeding_symptoms_book_emergency_type_not_checkup(self, db_conn):
        lp = lookup_patient(db_conn, "5550101001")
        assert lp["ok"]
        slot = check_availability(db_conn, limit=1)["slots"][0]
        sid, day = slot["id"], slot["date"]
        state = TurnState(
            conversation_id=1,
            user_message="confirm",
            workflow="book_new",
            collected_fields={
                "phone": "5550101001",
                "identity_verified": True,
                "symptoms": "tooth is bleeding",
                "date_preference": "asap",
                "date_resolved": day,
                "appointment_type": "checkup",
            },
            patient=dict(lp["patient"]),
            offered_slot_ids=[sid],
            slots_offered_for_date=day,
        )
        _route_book_new(state, db_conn)
        booked = next(r for r in state.tool_results if r.get("ok") and r.get("appointment"))
        assert booked["appointment"]["appointment_type"] == "emergency"
        assert booked["appointment"]["emergency_summary"] == "tooth is bleeding"
        assert state.is_emergency is True
        assert state.tone == "emergency"

    def test_without_is_emergency_routine_checkup_not_forced_to_emergency(self, db_conn):
        """After a prior emergency, is_emergency must not stay stuck True for a new book_new."""
        lp = lookup_patient(db_conn, "5550101001")
        slot = check_availability(db_conn, limit=1)["slots"][0]
        sid, day = slot["id"], slot["date"]
        state = TurnState(
            conversation_id=1,
            user_message="confirm",
            workflow="book_new",
            is_emergency=False,
            collected_fields={
                "phone": "5550101001",
                "identity_verified": True,
                "appointment_type": "checkup",
                "date_resolved": day,
            },
            patient=dict(lp["patient"]),
            offered_slot_ids=[sid],
            slots_offered_for_date=day,
        )
        _route_book_new(state, db_conn)
        booked = next(r for r in state.tool_results if r.get("ok") and r.get("appointment"))
        assert booked["appointment"]["appointment_type"] == "checkup"


class TestEmergencyRoute:
    def test_after_close_offers_then_books_on_confirm(self, monkeypatch, db_conn):
        monkeypatch.setattr(engine_mod, "is_past_office_close_pt", lambda: True)
        state = TurnState(
            conversation_id=1,
            user_message="x",
            workflow="emergency",
            collected_fields={"phone": "5550101001", "symptoms": "severe pain"},
        )
        _route_emergency(state, db_conn)
        assert not state.is_complete
        assert any(r.get("awaiting_selection") for r in state.tool_results)
        assert state.offered_slot_ids
        ids = list(state.offered_slot_ids)
        day_key = state.slots_offered_for_date
        lp = lookup_patient(db_conn, "5550101001")
        state2 = TurnState(
            conversation_id=1,
            user_message="confirm",
            workflow="emergency",
            collected_fields={"phone": "5550101001", "symptoms": "severe pain"},
            offered_slot_ids=ids,
            slots_offered_for_date=day_key,
            patient=dict(lp["patient"]),
        )
        _route_emergency(state2, db_conn)
        assert state2.is_complete
        booked = next(r for r in state2.tool_results if r.get("ok") and r.get("appointment"))
        assert booked["appointment"]["date"] != pt_today_iso()
        assert booked["appointment"]["appointment_type"] == "emergency"


class TestRouterGuard:
    def test_skips_when_complete(self, db_conn):
        state = TurnState(
            conversation_id=1, user_message="t", workflow="book_new",
            collected_fields={"name": "A", "phone": "555-010-1001", "date_preference": "tomorrow"},
            is_complete=True,
        )
        _run_router(state, db_conn)
        assert state.tool_results == []


class TestAppointmentChoice:
    @staticmethod
    def _no_norm_time(_fields: dict[str, Any], _msg: str) -> None:
        return None

    def test_explicit_id_without_prior_list(self):
        ids = [10, 20]
        nt = self._no_norm_time
        assert _resolve_offered_appointment_id(ids, [], {"appointment_id": 20}, "", nt) == 20
        assert _resolve_offered_appointment_id(ids, [], {}, "first", nt) is None

    def test_ordinal_only_after_list_shown(self):
        ids = [10, 20]
        assert _resolve_offered_appointment_id(ids, ids, {}, "second", self._no_norm_time) == 20


class TestSlotOutsideOfferedShortlist:
    @pytest.mark.parametrize("narrow_fetch", [False, True])
    def test_requested_time_outside_shortlist(self, db_conn, narrow_fetch):
        from dental_assistant.infrastructure import queries as queries_mod

        day = check_availability(db_conn, limit=1)["slots"][0]["date"]
        if not narrow_fetch:
            same_day = check_availability(db_conn, date_filter=day, limit=25)["slots"]
            assert len(same_day) >= 6
            offered_ids = [same_day[i]["id"] for i in range(5)]
            target = same_day[5]
            slots_arg, fields = same_day, {"date_resolved": day, "selected_time": target["time"]}
        else:
            slots_arg = check_availability(db_conn, date_filter=day, limit=3)["slots"]
            assert len(slots_arg) >= 3
            offered_ids = [s["id"] for s in slots_arg]
            deeper = queries_mod.find_available_slots(db_conn, date_filter=day, limit=50)
            extra = next(s for s in deeper if s["id"] not in offered_ids)
            fields = {"date_resolved": day, "selected_time": extra["time"]}
            target = extra

        found = _find_slot_outside_offered_shortlist(db_conn, slots_arg, offered_ids, fields, "")
        assert found is not None and found["id"] == target["id"]


class TestSlotChoice:
    def test_resolve_by_id_and_time(self):
        slots = [{"id": 1, "time": "08:00", "date": "2026-04-07"}, {"id": 2, "time": "14:30", "date": "2026-04-07"}]
        offered = [1, 2]
        assert _resolve_offered_slot(slots, offered, {"selected_slot_id": 2}, "")["id"] == 2
        assert _resolve_offered_slot(slots, offered, {"selected_time": "2:30pm"}, "")["id"] == 2
        assert _resolve_offered_slot(slots, offered, {}, "yes")["id"] == 1
        assert _resolve_offered_slot(slots, offered, {}, "first one")["id"] == 1
        assert _resolve_offered_slot(slots, [1], {}, "8 am")["id"] == 1
        slots3 = [
            {"id": 1, "time": "08:00", "date": "2026-04-07"},
            {"id": 3, "time": "10:00", "date": "2026-04-07"},
        ]
        assert _resolve_offered_slot(slots3, [1, 3], {"selected_time": "10 am"}, "okay do it 10 am then")["id"] == 3
        assert _resolve_offered_slot(slots, [99], {"selected_slot_id": 99}, "") is None

    def test_no_offer_means_no_match(self):
        slots = [{"id": 1, "time": "08:00", "date": "2026-04-07"}]
        assert _resolve_offered_slot(slots, [], {"selected_slot_id": 1}, "") is None


class TestMultiFAQ:
    def test_split_topics(self):
        assert _split_faq_topics("timings and location") == ["timings", "location"]
        assert _split_faq_topics("hours, insurance") == ["hours", "insurance"]
        assert _split_faq_topics("hours") == ["hours"]


class TestSanitizeError:
    def test_strips_sensitive_data(self):
        assert "AIzaSy" not in _sanitize_error("Error key=AIzaSyABC123 happened")
        assert "sk-abc" not in _sanitize_error("token=sk-abc123xyz")
        assert _sanitize_error("Normal error") == "Normal error"


# ═══════════════════════════════════════════════════════════════════════════
# J1. Patient identity, appointment resolution, visit notes, family blocks
# ═══════════════════════════════════════════════════════════════════════════


class TestPatientIdentity:
    @pytest.mark.parametrize("partial,full,expected", [
        ("James Wilson", "James Wilson", "strong"),
        ("James", "James Wilson", "strong"),
        ("Wilson", "James Wilson", "strong"),
        ("James Blo", "James Wilson", "weak"),
        ("Bob", "James Wilson", "mismatch"),
    ])
    def test_name_match_tier(self, partial, full, expected):
        assert name_match_tier(partial, full) == expected

    @pytest.mark.parametrize("msg,reply", [
        ("yeah", "yes"),
        ("nope", "no"),
        ("that's me", "yes"),
        ("maybe", "unclear"),
    ])
    def test_identity_confirmation_reply(self, msg, reply):
        assert identity_confirmation_reply(msg) == reply


class TestResolveAppointmentSelectionFull:
    def test_unique_match_by_date_and_time(self):
        appts = [
            {"id": 10, "date": "2026-04-10", "time": "09:00"},
            {"id": 11, "date": "2026-04-10", "time": "14:00"},
        ]
        fields = {"date_resolved": "2026-04-10", "selected_time": "2pm"}
        sel, err = resolve_appointment_selection_full(
            appts, fields, "", [], normalized_time_from_fields_or_message,
        )
        assert err is None and sel == 11

    def test_not_found_when_time_wrong(self):
        appts = [{"id": 10, "date": "2026-04-10", "time": "09:00"}]
        fields = {"date_resolved": "2026-04-10"}
        sel, err = resolve_appointment_selection_full(
            appts, fields, "3pm", [], normalized_time_from_fields_or_message,
        )
        assert sel is None and err == "not_found"

    def test_selected_id_from_full_list_not_subset(self):
        appts = [{"id": 99, "date": "2026-04-10", "time": "09:00"}]
        fields = {"selected_appointment_id": 99}
        sel, err = resolve_appointment_selection_full(
            appts, fields, "", [10, 11], normalized_time_from_fields_or_message,
        )
        assert err is None and sel == 99

    def test_same_day_disambiguate_by_visit_type(self):
        appts = [
            {"id": 1, "date": "2026-04-06", "time": "08:00", "appointment_type": "emergency"},
            {"id": 2, "date": "2026-04-06", "time": "09:00", "appointment_type": "cleaning"},
        ]
        fields = {"date_resolved": "2026-04-06", "appointment_type": "cleaning", "selected_time": "9am"}
        sel, err = resolve_appointment_selection_full(
            appts, fields, "move my cleaning", [], normalized_time_from_fields_or_message,
        )
        assert err is None and sel == 2


class TestBuildVisitNotes:
    def test_none_when_no_flag(self):
        assert build_visit_notes_from_fields({}) is None

    def test_includes_detail_and_docs_line(self):
        text = build_visit_notes_from_fields({
            "use_different_insurance": True,
            "alternate_insurance_note": "MetLife",
        })
        assert text and "not on file" in text and "MetLife" in text and "Bring insurance card" in text

    def test_includes_family_names_and_context(self):
        text = build_visit_notes_from_fields({
            "family_member_names": ["Maya Davis"],
            "notes": "for my wife",
        })
        assert text and "Maya Davis" in text and "wife" in text


class TestUrgentClinicalContext:
    @pytest.mark.parametrize("msg,fields,expected", [
        ("my tooth is bleeding", {}, True),
        ("", {"symptoms": "gums are bleeding"}, True),
        ("book a cleaning", {}, False),
        ("mild sensitivity", {"notes": "nothing urgent"}, False),
    ])
    def test_detection(self, msg, fields, expected):
        assert _urgent_clinical_context(fields, msg) is expected


class TestCoerceAppointmentType:
    @pytest.mark.parametrize("raw,expected", [
        (None, "checkup"),
        ("", "checkup"),
        ("  ", "checkup"),
        ("cleaning", "cleaning"),
        ("CHECKUP", "checkup"),
        ("not_a_real_type", "unknown"),
    ])
    def test_coerce(self, raw, expected):
        assert coerce_appointment_type(raw) == expected


class TestVisitSummaryForChat:
    def test_insurance_note_shortcut(self):
        note = build_visit_notes_from_fields({
            "use_different_insurance": True,
            "alternate_insurance_note": "MetLife",
        })
        line = visit_summary_for_chat("checkup", note)
        assert "Different insurance" in line and "card" in line.lower()

    def test_combines_type_and_free_text_note(self):
        line = visit_summary_for_chat("checkup", "Six-month recall.")
        assert line.startswith("Checkup") and "recall" in line.lower()

    def test_type_only_without_notes(self):
        assert visit_summary_for_chat("cleaning", None) == "Cleaning"


class TestConversationDisplayFormatting:
    def test_decorates_friendly_datetime_labels(self):
        payload = _decorate_display([{"date": "2026-04-06", "time": "13:00"}])
        row = payload[0]
        assert row["friendly_date"] == "Monday, April 6"
        assert row["friendly_time"] == "1:00 PM"
        assert row["friendly_datetime_pt"] == "Monday, April 6 at 1:00 PM PT"

    def test_decorates_visit_note_summary(self):
        payload = _decorate_display([{"visit_notes": "Follow-up checkup after crown prep. Bring prior x-rays if available."}])
        row = payload[0]
        assert row["visit_notes_summary"] == "Follow-up checkup after crown prep."

    def test_decorates_family_block_summary(self):
        payload = _decorate_display([{
            "family_block_pt": {
                "family_size": 2,
                "start": "2026-04-10 14:30",
                "times": ["2026-04-10 14:30", "2026-04-10 15:00"],
            },
        }])
        row = payload[0]
        assert "back-to-back appointments" in row["family_block_summary"]


class TestFamilyBookingPayload:
    def test_family_route_marks_back_to_back_block(self, db_conn):
        state = TurnState(
            conversation_id=1,
            user_message="book for two family members friday",
            workflow="family_book",
            collected_fields={
                "name": "Emily",
                "phone": "5550101003",
                "identity_verified": True,
                "family_size": 2,
                "family_member_names": ["Maya", "Leo"],
                "date_preference": "Friday",
                "date_resolved": "2026-04-10",
                "appointment_type": "checkup",
            },
        )
        _route_family_book(state, db_conn)
        payload = next(r for r in state.tool_results if r.get("awaiting_selection"))
        assert payload["family_size"] == 2
        assert payload["family_member_names"] == ["Maya", "Leo"]
        assert payload["family_block_pt"]["family_size"] == 2

    def test_family_affirmative_confirms_current_offered_block(self, db_conn):
        slots = check_availability(db_conn, date_filter="2026-04-10", limit=30)["slots"]
        start_id = next(s["id"] for s in slots if s["time"] == "15:00")
        state = TurnState(
            conversation_id=1,
            user_message="sure",
            workflow="family_book",
            collected_fields={
                "name": "Emily",
                "phone": "5550101003",
                "identity_verified": True,
                "family_size": 2,
                "family_member_names": ["Maya", "Leo"],
                "date_preference": "Friday",
                "date_resolved": "2026-04-10",
                "appointment_type": "checkup",
                "selected_time": "3pm",
            },
            offered_slot_ids=[start_id],
            pending_family_size=2,
        )
        _route_family_book(state, db_conn)
        assert state.is_complete
        booked = [r for r in state.tool_results if r.get("ok") and r.get("appointment")]
        assert len(booked) == 2
        assert [r["appointment"]["time"] for r in booked] == ["15:00", "15:30"]


class TestFamilyConsecutiveBlock:
    def test_finds_two_back_to_back_on_seed_db(self, db_conn):
        day = check_availability(db_conn, limit=1)["slots"][0]["date"]
        day_slots = check_availability(db_conn, date_filter=day, limit=30)["slots"]
        assert len(day_slots) >= 2
        t0 = day_slots[0]["time"]
        t1 = day_slots[1]["time"]
        from dental_assistant.domain.constants import SLOT_DURATION_MINUTES

        h0, m0 = map(int, str(t0).split(":")[:2])
        h1, m1 = map(int, str(t1).split(":")[:2])
        if (h1 * 60 + m1) - (h0 * 60 + m0) != SLOT_DURATION_MINUTES:
            pytest.skip("first two slots on this day are not consecutive in seed")
        start = f"{h0:02d}:{m0:02d}"
        group = find_consecutive_block_starting_at(db_conn, day, start, 2, [])
        assert group is not None and len(group) == 2
        assert group[0]["time"] == t0 and group[1]["time"] == t1

    def test_rejected_ids_exclude_start(self, db_conn):
        day = check_availability(db_conn, limit=1)["slots"][0]["date"]
        day_slots = check_availability(db_conn, date_filter=day, limit=5)["slots"]
        if len(day_slots) < 2:
            pytest.skip("not enough slots")
        t0 = day_slots[0]["time"]
        h0, m0 = map(int, str(t0).split(":")[:2])
        start = f"{h0:02d}:{m0:02d}"
        assert find_consecutive_block_starting_at(db_conn, day, start, 2, [day_slots[0]["id"]]) is None


# ═══════════════════════════════════════════════════════════════════════════
# K. State Persistence
# ═══════════════════════════════════════════════════════════════════════════

class TestStatePersistence:
    def test_new_conversation_empty(self, db_conn):
        cid = q.insert_conversation(db_conn)
        assert _load_persisted_state(db_conn, cid) == {}

    def test_restores_state(self, db_conn):
        cid = q.insert_conversation(db_conn)
        meta = json.dumps({"turn_state": {
            "workflow": "book_new",
            "collected_fields": {"name": "Test"},
            "patient": {"id": 1, "name": "Test"},
        }})
        q.insert_message(db_conn, cid, "assistant", "Hello", meta)
        state = _load_persisted_state(db_conn, cid)
        assert state["workflow"] == "book_new"
        assert state["patient"]["name"] == "Test"

    def test_corrupted_metadata(self, db_conn):
        cid = q.insert_conversation(db_conn)
        q.insert_message(db_conn, cid, "assistant", "Hello", "NOT JSON{{{")
        assert _load_persisted_state(db_conn, cid) == {}

    def test_process_message_uses_only_prior_summary_for_orchestrator(self, monkeypatch):
        from dental_assistant.infrastructure.db import connection, init_db
        from dental_assistant.settings import Settings

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name

        init_db(path)
        with connection(path) as conn:
            cid = q.insert_conversation(conn)
            q.insert_message(conn, cid, "assistant", "How can I help today?", json.dumps({"turn_state": {}}))
            q.insert_message(conn, cid, "user", "I need to reschedule")

        captured: dict[str, str] = {}

        def fake_orchestrator(user_message: str, conversation_summary: str, prior_structured_state: str):
            captured["user_message"] = user_message
            captured["conversation_summary"] = conversation_summary
            captured["prior_structured_state"] = prior_structured_state
            return OrchestratorOutput(intent="general")

        monkeypatch.setattr(engine_mod, "run_orchestrator", fake_orchestrator)
        monkeypatch.setattr(engine_mod, "generate_reply", lambda payload: "Okay")
        monkeypatch.setattr(engine_mod, "get_settings", lambda: Settings(
            llm_provider="openai",
            llm_api_key="test-key",
            llm_model="test-model",
            openai_base_url="https://api.openai.com/v1",
            database_path=path,
            faq_path=None,
        ))

        process_message("Tomorrow morning works", cid, db_path=path)

        assert "I need to reschedule" in captured["conversation_summary"]
        assert "Tomorrow morning works" not in captured["conversation_summary"]
