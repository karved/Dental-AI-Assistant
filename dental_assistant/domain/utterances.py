"""User message patterns shared across routing (affirmatives, ordinal picks).

Single implementation for “yes / first / second” style resolution on offered ids.
"""

from __future__ import annotations

import re
from typing import Any, Callable

SHORT_AFFIRMATIVE: frozenset[str] = frozenset({
    "yes", "yeah", "yep", "sure", "please", "ok", "okay", "y", "confirm", "absolutely",
})

_ORDINAL_EXACT: frozenset[str] = frozenset({"ok", "k", "y", "yes", "yeah", "yep", "sure", "confirm", "okay"})


def infer_offered_list_ordinal(
    user_message: str,
    offered_ids: list[int],
    fields: dict[str, Any],
    normalized_time_fn: Callable[[dict[str, Any], str], str | None],
) -> int | None:
    """Map affirmatives / ordinals to an id in ``offered_ids``. Skips when a concrete time is present."""
    if not offered_ids:
        return None
    if normalized_time_fn(fields, user_message):
        return None
    t = user_message.lower().strip()
    if t in _ORDINAL_EXACT:
        return offered_ids[0]
    if re.search(r"\b(yes|yeah|yep|sure|confirm|absolutely)\b", t) or re.search(
        r"\b(sounds good|that works)\b", t
    ):
        return offered_ids[0]
    if re.match(r"^(ok|okay)\W", t):
        return offered_ids[0]
    if any(p in t for p in ("first", "first one", "earliest", "1st")):
        return offered_ids[0]
    if any(p in t for p in ("second", "2nd")) and len(offered_ids) > 1:
        return offered_ids[1]
    if any(p in t for p in ("third", "3rd")) and len(offered_ids) > 2:
        return offered_ids[2]
    return None
