# Dental Practice Conversational AI Chatbot

A deterministic, workflow-driven chatbot for dental scheduling, patient support, and emergency intake.

## Overview

This project is a conversational assistant for a dental practice that handles scheduling, urgent intake, and common office questions. It uses LLMs for extraction and response generation, while Python owns workflow control, tool execution, and state transitions.

## Demo Features

- New patient booking from intake through slot confirmation
- Existing patient support for appointment lookup, reschedule, and cancellation
- Family booking with sequential or back-to-back slot handling
- Emergency handling with escalation logging after urgent bookings are completed
- FAQ and general office inquiries via JSON-backed deterministic matching

## Architecture

The application uses a two-LLM pattern with a deterministic workflow engine in the middle.

```text
User / UI / API
      |
      v
Load persisted turn state from SQLite
      |
      v
LLM #1: Orchestrator
- intent classification
- field extraction
- structured JSON only
      |
      v
Deterministic Python layer
- merge state
- resolve dates/times
- choose workflow
- run tools
- select next question
      |
      v
LLM #2: Conversation Agent
- natural-language reply only
      |
      v
Persist assistant reply + compact turn state
```

Why this split:

- The orchestrator does classification and extraction only
- The conversation agent does phrasing only
- Python owns all side effects and decisions

I intentionally did not use an agent framework. For scheduling systems, explicit workflow code is easier to test, safer to reason about, and less brittle than autonomous tool loops.

### Stateful vs Stateless

Stateful parts:

- SQLite stores patients, slots, appointments, messages, feedback, and compact per-turn workflow state
- The workflow engine persists a `turn_state` snapshot in `messages.metadata_json`
- Emergency completion is logged to `emergency.log`

Stateless parts:

- The orchestrator LLM sees only the current user message, a short recent transcript window, and a compact structured carry-over
- The conversation agent sees only the current turn's structured context
- Neither LLM owns memory or business logic

### Context Management

The system is stateful, but LLM context is intentionally small.

- Only the latest few transcript messages are passed to the orchestrator via `ORCHESTRATOR_RECENT_MESSAGES`
- Only selected prior fields are carried forward via `ORCHESTRATOR_PRIOR_STATE_KEYS`, not the entire conversation payload
- Bulky tool outputs are not persisted into future turn state

This keeps prompts bounded, makes behavior more stable across long chats, and reduces the chance that older irrelevant context overrides current user intent.

## Design + Stack

- Deterministic routing over LLM-driven workflows  
  Booking, rescheduling, and cancellation have real side effects. Readiness checks, branching, and tool execution are implemented in Python so they remain testable and predictable.

- File-based FAQ  
  FAQ content lives in JSON, so office staff could update common answers without changing application code or redeploying.

- Type-agnostic slots  
  Slots are generic time blocks. Appointment type is attached at booking time, which keeps the scheduling model simple and flexible.

- Provider-agnostic LLM wrapper  
  The workflow layer does not depend on one model vendor. The current project supports provider switching and is configured to work with `gpt-5.4-mini`.

- Modular workflow shape  
  Workflows already follow a clear pattern: collect fields, determine readiness, route deterministically, execute tools, and generate a reply. That makes it straightforward to add new scenarios tomorrow without changing the core control model.
- Stack
  `FastAPI`, `Streamlit`, `SQLite`, `Pydantic`, and `gpt-5.4-mini`

- Core workflow behaviors
  1-2 questions per turn, skip already known fields, deterministic vague-date handling, recovery when offered times are rejected, emergency escalation logging, and per-conversation feedback capture for future evals

## Schema Summary

| Table | Purpose |
| --- | --- |
| `patients` | Stores patient identity and profile fields such as phone, DOB, and insurance |
| `available_slots` | Stores open schedule blocks as generic date/time slots |
| `appointments` | Stores confirmed or cancelled bookings, appointment type, and visit notes |
| `conversations` | Tracks chat sessions across turns |
| `messages` | Stores user and assistant messages plus `metadata_json` for compact persisted turn state |
| `feedback` | Stores per-conversation thumbs up/down feedback for future evaluation and quality monitoring |

Why this matters:

- `messages.metadata_json` is the bridge between stateless LLM calls and stateful workflows
- `feedback` creates a clean path for offline evaluation, regression analysis, and model iteration
- `appointments` plus `messages` make it possible to analyze not just what the assistant said, but what it actually caused in the scheduling system

Example persisted turn state:

```json
{
  "turn_state": {
    "workflow": "book_new",
    "patient": {
      "id": 4,
      "name": "James Wilson",
      "phone": "555-010-1004"
    },
    "collected_fields": {
      "name": "James Wilson",
      "phone": "5550101004",
      "date_preference": "next Friday",
      "date_resolved": "2026-04-10",
      "appointment_type": "checkup"
    },
    "offered_slot_ids": [21, 22, 23],
    "slots_offered_for_date": "2026-04-10",
    "offered_appointment_ids": [],
    "rejected_slots": [],
    "is_complete": false,
    "is_emergency": false,
    "emergency_logged": false
  }
}
```

## How to Run

Using the Makefile:

```bash
cp .env.template .env
make sync
make api
make ui
```

Useful commands:

- `make dev` for FastAPI with reload
- `make test` for the deterministic test suite
- `make lint` for Ruff
- `make reset-db` to recreate the seeded SQLite database

If you prefer direct commands:

```bash
uv sync --extra dev
uv run python main.py
uv run streamlit run app.py
```

Set these environment variables in `.env` before running:

- `LLM_PROVIDER`
- `LLM_API_KEY`
- `LLM_MODEL`
- `DATABASE_PATH`

## Future Improvements

- Scalability:
  Move from SQLite to Postgres, add connection pooling, introduce async processing, and use pub/sub for workflow events and downstream consumers
- Product Features:
  Add an admin dashboard for emergency escalation, human-in-the-loop review, reminders and follow-ups, and RAG over patient history or visit notes
- Security:
  Add RBAC and row-level security
- Reliability:
  Add a feedback-driven model improvement loop and a stronger guardrails layer around high-risk or ambiguous inputs
- Architecture:
  Separate stateful workflow orchestration from stateless inference services and formalize workflow registration so new scenarios can be added with less engine coupling

## Prioritization

I prioritized the core booking flow first because it is the most load-bearing part of the user experience. From there, I added reschedule/cancel, emergency handling, and family booking because they have clear operational value and expose the main failure modes: wrong slot selection, lost state across turns, repetitive questioning, and unsafe handling of urgent cases.

I intentionally did not spend early time on dashboards, auth, outbound reminders, or richer admin tooling. For this take-home, correctness, safety, workflow continuity, and extensibility mattered more than breadth. The current design scales by adding new workflows to the same deterministic pattern rather than replacing the control model.

## Repo Structure

```text
dental_assistant/
  application/      # deterministic engine, orchestrator wiring, workflow helpers
  domain/           # prompts, models, constants, parsing, question selection
  infrastructure/   # db, SQL queries, tools, LLM provider layer
  interfaces/       # FastAPI and Streamlit entrypoints
  data/             # FAQ content
tests/              # deterministic test suite
Makefile            # common dev commands
README.md
```
