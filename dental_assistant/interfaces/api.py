"""FastAPI HTTP interface."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from dental_assistant.application.engine import process_message
from dental_assistant.domain.models import (
    ChatRequest,
    ChatResponse,
    FeedbackRequest,
    FeedbackResponse,
)
from dental_assistant.infrastructure import db as db_mod
from dental_assistant.infrastructure.tools import save_feedback


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_mod.init_db()
    yield


app = FastAPI(title="Dental AI Assistant", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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


@app.post("/feedback", response_model=FeedbackResponse)
def feedback(body: FeedbackRequest):
    try:
        with db_mod.connection() as conn:
            result = save_feedback(conn, body.conversation_id, body.rating)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return FeedbackResponse(conversation_id=body.conversation_id, rating=body.rating)
