# Dental AI Assistant

Conversational AI chatbot for a dental practice — handles booking, rescheduling, cancellations, emergency triage, and general inquiries.

## Quick start

```bash
cp .env.template .env          # fill in LLM_API_KEY
uv sync                        # install dependencies
uv run python main.py          # FastAPI on :8000
uv run streamlit run app.py    # Streamlit UI
```

## Design notes

### Slot model

Slots are modeled as time blocks rather than pre-typed appointment categories.
This reflects real-world scheduling systems where appointment type is determined
at booking time rather than constrained by slot type.

For simplicity, all appointments are treated as fixed-duration (30 minutes).
In a production system, appointment duration would be enforced at the scheduling layer.

### Tool readiness

In the reference architecture, the orchestrator emits `ready_actions` to signal
when tools can execute. In this implementation, tool readiness is instead
determined by a deterministic rule-based layer (`_is_ready` in `engine.py`),
which checks whether the required fields for a given workflow have been collected.
This ensures full control and testability of workflow transitions — the LLM
classifies intent and extracts entities, but never decides what runs next.

### Database

SQLite is used for development. All SQL lives in a single `queries.py` data-access
layer, so swapping to PostgreSQL with connection pooling (e.g. `asyncpg` + `SQLAlchemy`)
in production requires changes only in the infrastructure layer — no application logic is affected.
