.PHONY: install sync dev api ui test lint clean reset-db help

help:            ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: sync    ## Alias for sync

sync:            ## Install / sync all dependencies
	uv sync

dev: sync        ## Run FastAPI with hot-reload
	uv run uvicorn dental_assistant.interfaces.api:app --reload --host 0.0.0.0 --port 8000

api:             ## Run FastAPI (production)
	uv run python main.py

ui:              ## Run Streamlit UI
	uv run streamlit run app.py

lint:            ## Run ruff linter
	uv run ruff check dental_assistant/

clean:           ## Remove build artifacts and caches
	rm -rf __pycache__ dental_assistant/__pycache__ .ruff_cache dist *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

reset-db:        ## Delete and recreate the database with seed data
	rm -f dental.db
	uv run python -c "from dental_assistant.infrastructure.db import init_db; init_db(); print('Database reset with seed data.')"
