"""Streamlit UI for local testing (same engine as the API)."""

from __future__ import annotations

import time

import streamlit as st

from dental_assistant.application.engine import process_message
from dental_assistant.infrastructure import db as db_mod
from dental_assistant.infrastructure.tools import save_feedback


def _init_state() -> None:
    defaults = {
        "conversation_id": None,
        "messages": [],
        "feedback_given": False,
        "last_meta": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def _simulate_stream(text: str, delay: float = 0.02) -> None:
    """Write text character-by-character for a streaming feel."""
    placeholder = st.empty()
    displayed = ""
    for ch in text:
        displayed += ch
        placeholder.markdown(displayed + "▌")
        time.sleep(delay)
    placeholder.markdown(displayed)


def _is_conversation_complete() -> bool:
    meta = st.session_state.last_meta
    if not meta:
        return False
    return meta.get("turn_state", {}).get("is_complete", False)


def _render_feedback() -> None:
    """Show feedback buttons only when the conversation workflow is complete."""
    cid = st.session_state.conversation_id
    if not cid or st.session_state.feedback_given:
        return
    if not _is_conversation_complete():
        return

    st.divider()
    st.markdown("**How was your experience?**")
    cols = st.columns([1, 1, 8])
    with cols[0]:
        if st.button("👍", key="fb_up", help="Helpful"):
            db_mod.init_db()
            with db_mod.connection() as conn:
                save_feedback(conn, cid, 1)
            st.session_state.feedback_given = True
            st.toast("Thanks for the feedback!", icon="👍")
            st.rerun()
    with cols[1]:
        if st.button("👎", key="fb_down", help="Not helpful"):
            db_mod.init_db()
            with db_mod.connection() as conn:
                save_feedback(conn, cid, -1)
            st.session_state.feedback_given = True
            st.toast("Thanks for the feedback!", icon="👎")
            st.rerun()


def run() -> None:
    st.set_page_config(page_title="Dental Assistant", page_icon="🦷")
    st.title("🦷 Dental practice assistant")

    _init_state()

    for msg in st.session_state.messages:
        st.chat_message(msg["role"]).write(msg["content"])

    _render_feedback()

    user_text = st.chat_input("Message the front desk...")
    if user_text:
        st.chat_message("user").write(user_text)

        with st.chat_message("assistant"):
            with st.spinner("Thinking..."):
                cid, reply, meta = process_message(
                    user_text, st.session_state.conversation_id
                )

            _simulate_stream(reply)

        st.session_state.conversation_id = cid
        st.session_state.messages.append({"role": "user", "content": user_text})
        st.session_state.messages.append({"role": "assistant", "content": reply})
        st.session_state.last_meta = meta
        st.session_state.feedback_given = False
        st.rerun()

    with st.sidebar:
        if st.button("🔄 New conversation"):
            st.session_state.conversation_id = None
            st.session_state.messages = []
            st.session_state.feedback_given = False
            st.session_state.last_meta = None
            st.rerun()

        st.subheader("Session")
        st.text(f"Conversation: {st.session_state.conversation_id or '—'}")

        turn_state = (st.session_state.last_meta or {}).get("turn_state", {})
        if turn_state:
            st.text(f"Workflow:     {turn_state.get('workflow', '—')}")
            st.text(f"Complete:     {turn_state.get('is_complete', False)}")
            st.text(f"Emergency:    {turn_state.get('is_emergency', False)}")

            collected = turn_state.get("collected_fields", {})
            if collected:
                st.subheader("Collected fields")
                st.json(collected)

            patient = turn_state.get("patient", {})
            if patient:
                st.subheader("Patient")
                st.json(patient)

        with st.expander("Raw metadata"):
            st.json(st.session_state.last_meta or {})

        st.caption("API: `uv run python main.py` → http://localhost:8000/docs")
