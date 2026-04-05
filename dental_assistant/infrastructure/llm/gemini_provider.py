"""Google Gemini via Generative Language REST API (API key query param)."""

from __future__ import annotations

from typing import Any

import httpx

from dental_assistant.infrastructure.llm.protocol import LLMProvider


class GeminiRESTProvider(LLMProvider):
    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    def complete(self, prompt: str, **kwargs: Any) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self._model}:generateContent"
        params = {"key": self._api_key}
        body: dict[str, Any] = {"contents": [{"parts": [{"text": prompt}]}]}
        temperature = kwargs.get("temperature")
        if temperature is not None:
            body["generationConfig"] = {"temperature": temperature}
        with httpx.Client(timeout=120.0) as client:
            r = client.post(url, params=params, json=body)
            r.raise_for_status()
            data = r.json()
        candidates = data.get("candidates") or []
        if not candidates:
            raise RuntimeError(f"Gemini returned no candidates: {data!r}")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        texts = [p.get("text", "") for p in parts if isinstance(p, dict)]
        text = "".join(texts).strip()
        if not text:
            raise RuntimeError(f"Gemini returned empty text: {data!r}")
        return text
