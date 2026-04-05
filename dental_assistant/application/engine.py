"""Deterministic pipeline: safety → orchestrator → tools/question selection → conversation agent."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from dental_assistant.application.conversation import generate_reply
from dental_assistant.application.orchestrator import run_orchestrator
from dental_assistant.domain.models import ConversationAgentInput, OrchestratorOutput
from dental_assistant.domain.question_selector import select_questions
from dental_assistant.infrastructure import db as db_mod
from dental_assistant.infrastructure.tools import save_message
from dental_assistant.settings import get_settings

# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

def _keyword_safety_check(text: str) -> str | None:
    """Return a blocked message if a rule matches; else None."""
    lowered = text.lower()
    blocked_phrases = ("suicide", "kill myself", "self-harm")
    if any(p in lowered for p in blocked_phrases):
        return "I'm not able to help with that. If you're in crisis, please contact local emergency services or a crisis hotline right away."
    return None

# ---------------------------------------------------------------------------
# State helpers — derived from messages.metadata_json (single source of truth)
# ---------------------------------------------------------------------------

def _merge_entities_into_state(state: dict[str, Any], entities: dict[str, Any]) -> None:
    for k, v in entities.items():
        if v is not None and v != "" and k not in state:
            state[k] = v


def _load_state(conn: sqlite3.Connection, conversation_id: int) -> dict[str, Any]:
    """Reconstruct accumulated state from the latest assistant message's metadata."""
    row = conn.execute(
        "SELECT metadata_json FROM messages "
        "WHERE conversation_id = ? AND role = 'assistant' AND metadata_json IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (conversation_id,),
    ).fetchone()
    if not row:
        return {}
    try:
        meta = json.loads(row["metadata_json"] or "{}")
        return meta.get("state", {})
    except (json.JSONDecodeError, TypeError):
        return {}


def _recent_summary(conn: sqlite3.Connection, conversation_id: int, limit: int = 6) -> str:
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
        (conversation_id, limit),
    ).fetchall()
    lines = [f"{r['role']}: {r['content']}" for r in reversed(list(rows))]
    return "\n".join(lines)


def ensure_conversation(conn: sqlite3.Connection, conversation_id: int | None) -> int:
    if conversation_id:
        row = conn.execute("SELECT id FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        if row:
            return conversation_id
    cur = conn.execute("INSERT INTO conversations DEFAULT VALUES")
    return int(cur.lastrowid)

# ---------------------------------------------------------------------------
# Main turn handler
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
    meta: dict[str, Any] = {}
    reply = ""

    blocked = _keyword_safety_check(user_message)
    with db_mod.connection(db_path) as conn:
        cid = ensure_conversation(conn, conversation_id)
        save_message(conn, cid, "user", user_message)

        if blocked:
            save_message(conn, cid, "assistant", blocked, json.dumps({"safety": "keyword_block"}))
            return cid, blocked, {"safety": "keyword_block"}

        state = _load_state(conn, cid)
        summary = _recent_summary(conn, cid)

        if not settings.llm_ready:
            reply = (
                "Set LLM_API_KEY (and LLM_PROVIDER / LLM_MODEL) in your environment to enable AI responses. "
                "See .env.template."
            )
        else:
            try:
                orch = run_orchestrator(user_message, summary)
            except Exception as e:
                orch = OrchestratorOutput(intent="unknown")
                meta["orchestrator_error"] = str(e)
            _merge_entities_into_state(state, orch.entities or {})
            questions = select_questions(orch, state)
            meta["orchestrator"] = orch.model_dump()
            meta["questions_planned"] = questions

            facts: dict[str, Any] = {
                "intent": orch.intent,
                "suggested_questions": questions,
                "filled_slots": dict(state),
            }
            tone = "emergency" if orch.intent == "emergency" else "default"
            goal = (
                "Ask at most one or two short questions from suggested_questions if needed; otherwise move the task forward."
                if questions
                else "Acknowledge and give a concise helpful reply."
            )
            payload = ConversationAgentInput(
                tone=tone,
                facts=facts,
                assistant_goal=goal,
                user_message=user_message,
            )
            try:
                reply = generate_reply(payload)
            except Exception as e:
                reply = "Sorry, something went wrong generating a reply. Please try again."
                meta["conversation_error"] = str(e)

        meta["state"] = state
        save_message(conn, cid, "assistant", reply, json.dumps(meta) if meta else None)

    return cid, reply, meta
