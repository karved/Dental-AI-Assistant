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
