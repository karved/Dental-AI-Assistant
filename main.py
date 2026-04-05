"""Run the FastAPI server (thin entrypoint)."""

from __future__ import annotations


def run() -> None:
    import uvicorn

    uvicorn.run("dental_assistant.interfaces.api:app", host="127.0.0.1", port=8000, reload=True)


if __name__ == "__main__":
    run()
