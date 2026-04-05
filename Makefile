.PHONY: install sync dev api ui lint clean reset-db test smoke help

help:            ## Show this help
	@echo   sync         Install / sync all dependencies
	@echo   dev          Run FastAPI with hot-reload
	@echo   api          Run FastAPI (production)
	@echo   ui           Run Streamlit UI
	@echo   test         Run deterministic test suite
	@echo   lint         Run ruff linter
	@echo   clean        Remove build artifacts and caches
	@echo   reset-db     Delete and recreate the database

install: sync    ## Alias for sync

sync:            ## Install / sync all dependencies
	uv sync --extra dev

dev: sync        ## Run FastAPI with hot-reload
	uv run uvicorn dental_assistant.interfaces.api:app --reload --host 127.0.0.1 --port 8000

api:             ## Run FastAPI (production)
	uv run python main.py

ui:              ## Run Streamlit UI
	uv run streamlit run app.py

test:            ## Run deterministic test suite
	uv run python -m pytest tests/ -v

lint:            ## Run ruff linter
	uv run ruff check dental_assistant/

clean:           ## Remove build artifacts and caches
	uv run python -c "import shutil, pathlib; [shutil.rmtree(p, True) for p in pathlib.Path('.').rglob('__pycache__')]; [shutil.rmtree(d, True) for d in ['.ruff_cache', 'dist'] if pathlib.Path(d).exists()]; print('Cleaned.')"

reset-db:        ## Delete and recreate the database with seed data
	uv run python -c "import pathlib; p=pathlib.Path('dental.db'); p.unlink(True); from dental_assistant.infrastructure.db import init_db; init_db(); print('Database reset with seed data.')"
