"""Pydantic models for domain objects, API payloads, and structured LLM I/O."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Domain entities (input — no system-generated fields like id / created_at)
# ---------------------------------------------------------------------------

class Patient(BaseModel):
    id: int | None = None
    name: str
    phone: str
    dob: str | None = None
    insurance: str | None = None


class Slot(BaseModel):
    id: int | None = None
    date: str
    time: str
    duration_minutes: int = 30
    is_available: bool = True


class Appointment(BaseModel):
    id: int | None = None
    patient_id: int
    slot_id: int | None = None
    appointment_type: str = "checkup"
    status: str = "confirmed"
    is_emergency: bool = False
    emergency_summary: str | None = None


class Message(BaseModel):
    id: int | None = None
    conversation_id: int
    role: Literal["user", "assistant"]
    content: str
    metadata_json: str | None = None

# ---------------------------------------------------------------------------
# Read-only views (includes system-generated timestamps)
# ---------------------------------------------------------------------------

class PatientRead(Patient):
    id: int
    created_at: str


class AppointmentRead(Appointment):
    id: int
    created_at: str


class MessageRead(Message):
    id: int
    created_at: str

# ---------------------------------------------------------------------------
# API payloads
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    conversation_id: int | None = None
    message: str = Field(..., min_length=1)


class ChatResponse(BaseModel):
    conversation_id: int
    reply: str


class FeedbackRequest(BaseModel):
    conversation_id: int
    rating: Literal[-1, 1]


class FeedbackResponse(BaseModel):
    conversation_id: int
    rating: int

# ---------------------------------------------------------------------------
# LLM structured I/O
# ---------------------------------------------------------------------------

class OrchestratorOutput(BaseModel):
    """Structured output from LLM #1 (orchestrator). JSON only, no user-facing text."""

    intent: str = "unknown"
    entities: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = None


class ConversationAgentInput(BaseModel):
    """Structured payload passed to LLM #2 (conversation agent)."""

    tone: Literal["default", "emergency", "calm"] = "default"
    facts: dict[str, Any] = Field(default_factory=dict)
    assistant_goal: str = ""
    user_message: str = ""
