"""FastAPI HTTP interface."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from dental_assistant.application.engine import process_message
from dental_assistant.domain.models import ChatRequest, ChatResponse
from dental_assistant.infrastructure import db as db_mod


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_mod.init_db()
    yield


app = FastAPI(title="Dental AI Assistant", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def chat(body: ChatRequest):
    try:
        cid, reply, _meta = process_message(body.message, body.conversation_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return ChatResponse(conversation_id=cid, reply=reply)
