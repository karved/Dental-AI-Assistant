"""Environment-backed configuration (no provider SDKs required)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    """Runtime settings from environment variables."""

    llm_provider: str
    llm_api_key: str | None
    llm_model: str
    openai_base_url: str
    database_path: str
    faq_path: str | None

    @property
    def llm_ready(self) -> bool:
        return bool(self.llm_api_key and self.llm_api_key.strip())


@lru_cache
def get_settings() -> Settings:
    load_dotenv()
    provider = os.getenv("LLM_PROVIDER", "openai").strip().lower()
    key = (
        os.getenv("LLM_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("GEMINI_API_KEY")
    )
    model = os.getenv("LLM_MODEL", "gpt-4o-mini" if provider == "openai" else "gemini-2.0-flash")
    base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    db = os.getenv("DATABASE_PATH", "dental.db")
    faq = os.getenv("FAQ_PATH") or None
    return Settings(
        llm_provider=provider,
        llm_api_key=key,
        llm_model=model,
        openai_base_url=base,
        database_path=db,
        faq_path=faq,
    )


def clear_settings_cache() -> None:
    get_settings.cache_clear()
