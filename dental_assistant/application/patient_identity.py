"""Name matching and yes/no for patient identity confirmation."""

from __future__ import annotations

import re
from typing import Literal

from dental_assistant.domain.utterances import SHORT_AFFIRMATIVE

NameTier = Literal["strong", "weak", "mismatch"]
IdentityReply = Literal["yes", "no", "unclear"]

_SHORT_NO = frozenset({"no", "nope", "nah", "wrong", "not me", "notme"})


def name_match_tier(partial: str, full_on_file: str) -> NameTier:
    """How well the user's name matches the full name on file for this phone."""
    p = (partial or "").lower().strip()
    f = (full_on_file or "").lower().strip()
    if not p or not f:
        return "weak"
    if p == f:
        return "strong"
    if len(p) >= 3 and p in f:
        return "strong"
    ptoks = [t for t in re.split(r"\s+", p) if len(t) > 1]
    if not ptoks:
        return "weak"
    if all(tok in f for tok in ptoks):
        return "strong"
    if any(tok in f for tok in ptoks):
        return "weak"
    return "mismatch"


def identity_confirmation_reply(message: str) -> IdentityReply:
    t = message.strip().lower()
    if t in SHORT_AFFIRMATIVE:
        return "yes"
    if t in _SHORT_NO:
        return "no"
    if any(p in t for p in ("that's me", "thats me", "it's me", "its me", "correct")):
        return "yes"
    return "unclear"
