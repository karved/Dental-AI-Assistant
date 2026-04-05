"""OpenAI Chat Completions and compatible proxies (LM Studio, Azure OpenAI-style gateways)."""

from __future__ import annotations

from typing import Any

import httpx

from dental_assistant.infrastructure.llm.protocol import LLMProvider


class OpenAICompatProvider(LLMProvider):
    def __init__(self, api_key: str, model: str, base_url: str) -> None:
        self._api_key = api_key
        self._model = model
        self._base = base_url.rstrip("/")

    def complete(self, prompt: str, **kwargs: Any) -> str:
        url = f"{self._base}/chat/completions"
        temperature = kwargs.get("temperature", 0.2)
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=120.0) as client:
            r = client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
        try:
            return (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(f"Unexpected OpenAI-compatible response: {data!r}") from e
