.PHONY: install sync dev api ui lint clean reset-db smoke help

help:            ## Show this help
	@echo   sync         Install / sync all dependencies
	@echo   dev          Run FastAPI with hot-reload
	@echo   api          Run FastAPI (production)
	@echo   ui           Run Streamlit UI
	@echo   smoke        Run quick smoke tests
	@echo   lint         Run ruff linter
	@echo   clean        Remove build artifacts and caches
	@echo   reset-db     Delete and recreate the database

install: sync    ## Alias for sync

sync:            ## Install / sync all dependencies
	uv sync

dev: sync        ## Run FastAPI with hot-reload
	uv run uvicorn dental_assistant.interfaces.api:app --reload --host 127.0.0.1 --port 8000

api:             ## Run FastAPI (production)
	uv run python main.py

ui:              ## Run Streamlit UI
	uv run streamlit run app.py

smoke:           ## Run quick smoke tests
	uv run python -c "from dental_assistant.infrastructure.db import init_db; init_db(); from dental_assistant.application.engine import _is_ready, _keyword_safety_check; from dental_assistant.domain.question_selector import select_questions; from dental_assistant.domain.date_resolver import resolve_date; from dental_assistant.infrastructure.tools import get_office_info; assert _keyword_safety_check('hello') is None; assert _keyword_safety_check('kill myself') is not None; assert _is_ready('book_new', {'name':'A','phone':'5','date_preference':'x'}); assert not _is_ready('book_new', {}); assert len(select_questions('book_new', {})) == 2; assert resolve_date('next week') is not None; assert get_office_info('hours')['ok']; print('All smoke tests passed.')"

lint:            ## Run ruff linter
	uv run ruff check dental_assistant/

clean:           ## Remove build artifacts and caches
	uv run python -c "import shutil, pathlib; [shutil.rmtree(p, True) for p in pathlib.Path('.').rglob('__pycache__')]; [shutil.rmtree(d, True) for d in ['.ruff_cache', 'dist'] if pathlib.Path(d).exists()]; print('Cleaned.')"

reset-db:        ## Delete and recreate the database with seed data
	uv run python -c "import pathlib; p=pathlib.Path('dental.db'); p.unlink(True); from dental_assistant.infrastructure.db import init_db; init_db(); print('Database reset with seed data.')"
