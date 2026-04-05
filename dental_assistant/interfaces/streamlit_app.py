"""Streamlit UI for local testing (same engine as the API)."""

from __future__ import annotations

import streamlit as st

from dental_assistant.application.engine import process_message


def run() -> None:
    st.set_page_config(page_title="Dental Assistant", page_icon="🦷")
    st.title("Dental practice assistant")

    if "conversation_id" not in st.session_state:
        st.session_state.conversation_id = None
    if "messages" not in st.session_state:
        st.session_state.messages = []

    for msg in st.session_state.messages:
        st.chat_message(msg["role"]).write(msg["content"])

    user_text = st.chat_input("Message the front desk…")
    if user_text:
        with st.spinner("Thinking…"):
            cid, reply, _meta = process_message(user_text, st.session_state.conversation_id)
        st.session_state.conversation_id = cid
        st.session_state.messages.append({"role": "user", "content": user_text})
        st.session_state.messages.append({"role": "assistant", "content": reply})
        st.rerun()

    with st.expander("Debug"):
        st.write("conversation_id:", st.session_state.conversation_id)
        if st.button("Reset conversation"):
            st.session_state.conversation_id = None
            st.session_state.messages = []
            st.rerun()

    st.caption("API: uv run uvicorn dental_assistant.interfaces.api:app --reload")
