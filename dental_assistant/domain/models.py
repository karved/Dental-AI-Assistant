"""Pydantic models for domain objects, API payloads, and structured LLM I/O."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Domain entities (input -- no system-generated fields like id / created_at)
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
# Turn state -- single object that flows through the deterministic pipeline
# ---------------------------------------------------------------------------

Intent = Literal[
    "book_new", "reschedule", "cancel", "family_book",
    "emergency", "faq", "general", "unknown",
]

Tone = Literal["default", "emergency", "calm", "friendly"]


class TurnState(BaseModel):
    """Mutable state for one conversation turn. Flows: engine -> router -> tools -> conversation agent."""

    conversation_id: int
    user_message: str

    workflow: Intent = "unknown"
    patient: dict[str, Any] = Field(default_factory=dict)
    family_members: list[dict[str, Any]] = Field(default_factory=list)

    collected_fields: dict[str, Any] = Field(default_factory=dict)
    orchestrator_output: dict[str, Any] = Field(default_factory=dict)
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    questions_to_ask: list[str] = Field(default_factory=list)
    rejected_slots: list[int] = Field(default_factory=list)

    is_complete: bool = False
    is_emergency: bool = False
    tone: Tone = "default"

# ---------------------------------------------------------------------------
# LLM #1 -- Orchestrator structured output
# ---------------------------------------------------------------------------

class OrchestratorOutput(BaseModel):
    """Strict JSON returned by the orchestrator LLM. No prose.

    The orchestrator ONLY classifies intent and extracts fields.
    It does NOT decide routing, questions, or actions -- that is the
    deterministic Python layer's job.
    """

    intent: Intent = "unknown"
    extracted_fields: dict[str, Any] = Field(default_factory=dict)
    tone: Tone = "default"
    confidence: float | None = None

# ---------------------------------------------------------------------------
# LLM #2 -- Conversation agent input
# ---------------------------------------------------------------------------

class ConversationAgentInput(BaseModel):
    """Structured payload passed to the conversation agent LLM."""

    tone: Tone = "default"
    workflow: str = "unknown"
    patient: dict[str, Any] = Field(default_factory=dict)
    collected_fields: dict[str, Any] = Field(default_factory=dict)
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    questions_to_ask: list[str] = Field(default_factory=list)
    is_complete: bool = False
    is_emergency: bool = False
    user_message: str = ""
