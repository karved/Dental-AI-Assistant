"""Resolve provider from settings; single entry `call_llm` for application code."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from dental_assistant.infrastructure.llm.gemini_provider import GeminiRESTProvider
from dental_assistant.infrastructure.llm.openai_provider import OpenAICompatProvider
from dental_assistant.infrastructure.llm.protocol import LLMProvider
from dental_assistant.settings import Settings, get_settings

_REGISTRY: dict[str, Callable[[Settings], LLMProvider]] = {
    "openai": lambda s: OpenAICompatProvider(
        api_key=s.llm_api_key or "",
        model=s.llm_model,
        base_url=s.openai_base_url,
    ),
    "gemini": lambda s: GeminiRESTProvider(api_key=s.llm_api_key or "", model=s.llm_model),
}


def register_provider(name: str, factory: Callable[[Settings], LLMProvider]) -> None:
    """Register a provider name -> builder that reads keys/models from Settings."""
    _REGISTRY[name.lower()] = factory


def _build_provider(settings: Settings) -> LLMProvider:
    name = settings.llm_provider
    if name not in _REGISTRY:
        raise ValueError(
            f"Unknown LLM_PROVIDER={name!r}. Registered: {sorted(_REGISTRY)}. "
            "Add one with register_provider()."
        )
    return _REGISTRY[name](settings)


def call_llm(prompt: str, **kwargs: Any) -> str:
    """Provider-agnostic completion. Config from env: LLM_PROVIDER, LLM_API_KEY, LLM_MODEL."""
    settings = get_settings()
    impl = _build_provider(settings)
    return impl.complete(prompt, **kwargs)
