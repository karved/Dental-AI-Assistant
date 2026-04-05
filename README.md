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

### FAQ resolution

FAQ responses use a tiered strategy — deterministic matching first, optional
LLM fallback only when needed:

1. **Exact key** — direct lookup in `faq.json` (free, instant).
2. **Keyword + prefix-stem** — substring and word-prefix matching against FAQ text (free, instant).
3. **Fuzzy** — `difflib.get_close_matches` against FAQ keys (free, instant).
4. **LLM fallback** — only if tiers 1–3 fail, sends FAQ context to the LLM for a flexible answer (costs tokens, optional).

This balances cost efficiency with robustness: most queries resolve deterministically
at zero cost, and the LLM is invoked only for genuinely ambiguous inputs.

### Timezone

Vague dates (`today`, `tonight`, `tomorrow`) and the **after 6 PM PT** same-day
booking rule use **US Pacific** (`America/Los_Angeles` by default). Override with
`DISPLAY_TIMEZONE` in `.env` if needed. The conversation agent is instructed to
phrase all times in Pacific.

### Family / household

Family visits book multiple slots on one account (same `patient_id`). Optional
names for spouse or children can live in orchestrator `extracted_fields` (e.g.
`family_member_names`) for a nicer transcript; the take-home schema does not
require separate patient rows. Production systems often model **households** or
link dependent patients for billing and records.

### Known limitations / future improvements

- **Multi-action conversations** — once a workflow completes (`is_complete`), the
  current turn ends. If the user then says "I also need to reschedule another
  appointment", a new conversation or an explicit state reset is needed. In
  production this would be handled by detecting a new intent after completion and
  resetting the workflow fields while preserving patient context.
- **Session timeout** — there is no idle-timeout or session expiry. A production
  system would expire conversations after inactivity and flush any pending
  emergency logs as a safety net.

### Database

SQLite is used for development. All SQL lives in a single `queries.py` data-access
layer, so swapping to PostgreSQL with connection pooling (e.g. `asyncpg` + `SQLAlchemy`)
in production requires changes only in the infrastructure layer — no application logic is affected.
