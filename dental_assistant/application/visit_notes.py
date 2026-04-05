"""Visit-level notes stored on appointments (non-emergency)."""

from __future__ import annotations

from typing import Any


def build_visit_notes_from_fields(fields: dict[str, Any]) -> str | None:
    """Record operational notes for front desk: family context and insurance changes."""
    parts: list[str] = []

    names = fields.get("family_member_names")
    if isinstance(names, list):
        clean_names = [str(n).strip() for n in names if str(n).strip()]
        if clean_names:
            parts.append("Family booking for: " + ", ".join(clean_names) + ".")
    elif isinstance(names, str) and names.strip():
        parts.append(f"Family booking for: {names.strip()}.")

    note = str(fields.get("notes") or "").strip()
    if note and any(word in note.lower() for word in ("wife", "husband", "spouse", "son", "daughter", "kid", "child")):
        parts.append(f"Family context: {note}.")

    flag = fields.get("use_different_insurance") or fields.get("alternate_insurance_request")
    if flag:
        detail = (fields.get("alternate_insurance_note") or fields.get("insurance_note") or "").strip()
        parts.append("Patient plans to use insurance that is not on file.")
        if detail:
            parts.append(f"Details from patient: {detail}")
        parts.append("Bring insurance card to the visit; front desk will update records.")

    return " ".join(parts) or None
