"""Returning-patient identity confirmation and welcome briefing (before booking slots)."""

from __future__ import annotations

from typing import Any

from dental_assistant.application.patient_identity import identity_confirmation_reply, name_match_tier
from dental_assistant.domain.models import TurnState
from dental_assistant.infrastructure import tools


def book_new_or_family_preflight(state: TurnState, conn: Any, workflow: str) -> bool:
    """Identity + optional welcome for an existing phone match. True = stop router for this turn."""
    if workflow not in ("book_new", "family_book"):
        return False
    fields = state.collected_fields
    phone = fields.get("phone")
    name = fields.get("name")
    if not phone or not name:
        return False

    if fields.get("date_preference"):
        fields.setdefault("returning_welcome_done", True)

    pr = tools.lookup_patient(conn, phone)
    if not pr["ok"]:
        return False

    patient = pr["patient"]
    tier = name_match_tier(name, patient.get("name", ""))

    if tier == "strong":
        fields["identity_verified"] = True
        state.patient = patient
    else:
        pending = fields.get("_pending_identity_name")
        reply = identity_confirmation_reply(state.user_message)
        if pending:
            if reply == "yes":
                fields["identity_verified"] = True
                fields["name"] = patient["name"]
                fields.pop("_pending_identity_name", None)
                state.patient = patient
            elif reply == "no":
                state.tool_results.append({
                    "ok": False,
                    "error": (
                        "The name you gave does not match our records for that phone number. "
                        "Please call the office so we can verify your chart, or try again with the correct details."
                    ),
                    "identity_rejected": True,
                })
                state.is_complete = True
                return True
            else:
                state.tool_results.append({
                    "ok": True,
                    "awaiting_identity_confirmation": True,
                    "name_on_file": pending,
                    "message": f"Please reply yes if you are '{pending}', or no if not.",
                })
                return True
        else:
            fields["_pending_identity_name"] = patient["name"]
            state.tool_results.append({
                "ok": True,
                "awaiting_identity_confirmation": True,
                "name_on_file": patient["name"],
                "message": (
                    f"We have '{patient['name']}' on file for this phone. "
                    "Please confirm that's you (yes / no) so we can pull up the right chart."
                ),
            })
            return True

    if not fields.get("identity_verified"):
        return True

    state.patient = patient

    if workflow != "book_new":
        return False

    if fields.get("returning_welcome_done"):
        return False

    appts = tools.get_patient_appointments(conn, patient["id"])
    appt_rows = appts.get("appointments", []) if appts.get("ok") else []
    fields["returning_welcome_done"] = True
    state.tool_results.append({
        "ok": True,
        "returning_patient_briefing": True,
        "appointments": appt_rows,
        "insurance_on_file": patient.get("insurance"),
        "patient_name": patient.get("name"),
        "message": (
            "Mention insurance on file if present; list upcoming visits (date/time PT) or say there are none. "
            "Ask if they need a new visit, reschedule, cancel, or something else."
        ),
    })
    return True
