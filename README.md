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

I intentionally did not use a full agent framework for this version. For the scope of the take-home, explicit workflow code made the scheduling state machine easier to test, inspect, and explain than hiding control flow inside autonomous tool loops.

### REST Chat Turn Model

The chat experience is implemented over REST, not WebSockets. Each user message is a separate `POST /chat` request, and the returned `conversation_id` is passed back on the next turn.

This gives the UI a chat-like feel without keeping a long-lived socket open. On every request, the backend loads the latest compact workflow state and recent transcript from SQLite, processes the new message through the orchestrator, deterministic workflow layer, tools, and conversation agent, then persists the updated state for the next request.

For this take-home, the endpoint intentionally stays simple: no RBAC, user accounts, tenant boundaries, streaming transport, or admin-side permissions layer. A production version would likely add authenticated users, RBAC/row-level authorization, scoped conversation access, rate limiting, audit logs, and possibly WebSocket or SSE streaming if partial-token responses or real-time operator handoff became important.

### Orchestration Tradeoff

I considered whether a workflow framework such as LangGraph or Temporal would be appropriate because the assistant state is persisted and replayable across turns. I kept orchestration in deterministic Python for this prototype because the core flows are still small and domain-specific: classify intent, merge fields, check readiness, run tools, ask the next question, and persist state.

In a production version, I would consider LangGraph if the conversational graph grew into many reusable agent nodes or needed framework-level checkpointing. I would consider Temporal for durable background workflows rather than the live chat turn itself: reminder delivery, staff escalation SLAs, human review queues, async insurance checks, retries across external systems, and other long-running operations that need stronger guarantees.

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

### Tone Handling

Supported tones are `default`, `friendly`, `calm`, and `emergency`.

- The orchestrator proposes an initial tone as part of its structured output
- The deterministic engine can override that tone when workflow logic or safety signals require it
- The conversation agent is the component that actually applies the tone in user-facing language

In practice, this means tone is not owned purely by the conversation agent. The ideal control point is the current one: the orchestrator suggests, Python enforces, and the conversation agent expresses.

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

| Table | Key columns | Purpose |
| --- | --- | --- |
| `patients` | `id (PK)`, `name`, `phone`, `dob`, `insurance`, `created_at` | Stores patient identity and profile fields such as phone, DOB, and insurance |
| `available_slots` | `id (PK)`, `date`, `time`, `duration_minutes`, `is_available` | Stores open schedule blocks as generic date/time slots |
| `appointments` | `id (PK)`, `patient_id (FK)`, `slot_id (FK)`, `appointment_type`, `status`, `is_emergency`, `emergency_summary`, `visit_notes`, `created_at`, `modified_at` | Stores confirmed or cancelled bookings, appointment type, and visit notes |
| `conversations` | `id (PK)`, `created_at` | Tracks chat sessions across turns |
| `messages` | `id (PK)`, `conversation_id (FK)`, `role`, `content`, `metadata_json`, `created_at` | Stores user and assistant messages plus `metadata_json` for compact persisted turn state |
| `feedback` | `id (PK)`, `conversation_id (FK)`, `rating`, `created_at` | Stores per-conversation thumbs up/down feedback for future evaluation and quality monitoring |

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
make ui   # separate terminal
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
uv run streamlit run app.py   # separate terminal
```

Set these environment variables in `.env` before running:

- `LLM_PROVIDER`
- `LLM_API_KEY`
- `LLM_MODEL`
- `DATABASE_PATH`

## Future Improvements

- Scalability:
  Move from SQLite to Postgres, add connection pooling, introduce async processing, and use pub/sub for workflow events consumed by reminder, notification, audit, or analytics services
- Product Features:
  Add an admin dashboard for emergency escalation, human-in-the-loop review, reminders and follow-ups, and RAG over patient history or visit notes
- Security:
  Add RBAC and row-level security
- Reliability:
  Add a feedback-driven model improvement loop and a stronger guardrails layer around high-risk or ambiguous inputs
- Architecture:
  Separate stateful workflow orchestration from stateless inference services, formalize workflow registration, and consider Temporal-style durable workflows for reminders, staff escalation SLAs, and cross-system retries

## Prioritization

I prioritized the core booking flow first because it is the most load-bearing part of the user experience. From there, I added reschedule/cancel, emergency handling, and family booking because they have clear operational value and expose the main failure modes: wrong slot selection, lost state across turns, repetitive questioning, and unsafe handling of urgent cases.  

The focus for this take-home was more on the agentic workflow design, LLM architecture, core user-facing features, and a simple UI/UX than on building a fully scalable or future-proof platform from day one.

I intentionally did not spend early time on database hardening, event-driven infrastructure, dashboards, auth, outbound reminders, or richer admin-side tooling. Those are important in production, especially for reminders, admin review, and multi-user access control, but for this prototype correctness, safety, workflow continuity, and extensibility mattered more than breadth. The current design scales by adding new workflows to the same deterministic pattern rather than replacing the control model.

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

## Dry Run Example

Example conversation:

1. User: `I need a checkup next Friday morning. My name is James Wilson and my phone is 5550101004.`
2. Assistant: `I found a few openings for Friday, April 10. Would 9:00 AM, 9:30 AM, or 10:00 AM PT work?`
3. User: `9:30 works.`
4. Assistant: `You’re booked for Friday, April 10 at 9:30 AM PT.`

What happens internally:

**Turn 1**

- Input to orchestrator:
  current user message + recent transcript window + compact prior state
- Orchestrator output:

```json
{
  "intent": "book_new",
  "extracted_fields": {
    "appointment_type": "checkup",
    "date_preference": "next Friday morning",
    "name": "James Wilson",
    "phone": "5550101004"
  },
  "tone": "default"
}
```

- Deterministic layer:
  merges fields, resolves `date_preference` to `date_resolved`, checks readiness, looks up the patient, checks availability, filters morning slots, and stores the offered slot IDs
- Tool calls:
  `lookup_patient(phone)` -> existing patient found
  `check_availability(date_filter/date_range, limit=...)` -> candidate slots returned
- Input to conversation agent:
  workflow + patient + collected fields + tool results + next question + tone hint
- Persisted state:
  compact `turn_state` snapshot with workflow, collected fields, offered slots, and completion flags

**Turn 2**

- User says `9:30 works`
- Input to orchestrator:
  current message + short recent transcript + prior structured state showing `awaiting_slot_selection`
- Orchestrator output:

```json
{
  "intent": "book_new",
  "extracted_fields": {
    "selected_time": "9:30"
  },
  "tone": "default"
}
```

- Deterministic layer:
  resolves the selected time against the previously offered slot list, books the appointment, marks the workflow complete, and clears pending offered-slot state
- Tool call:
  `book_appointment(patient_id, slot_id, appointment_type)`
- Conversation agent:
  converts the structured success payload into the final confirmation message using the final tone chosen by the workflow layer

This is the core pattern across workflows: LLM #1 extracts, Python decides, tools execute, LLM #2 phrases, and SQLite persists enough state for the next turn.
