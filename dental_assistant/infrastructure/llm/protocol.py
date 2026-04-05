"""LLM port — same idea as a typed model wrapper, without an agent framework."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class LLMProvider(ABC):
    """Single-shot text completion; implement per vendor REST API."""

    @abstractmethod
    def complete(self, prompt: str, **kwargs: Any) -> str:
        """Return assistant-visible text for one user prompt."""
