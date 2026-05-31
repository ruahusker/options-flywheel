from __future__ import annotations

import json
from typing import Any

import httpx

from app.config import settings


class KimiClientError(Exception):
    pass


def kimi_configured() -> bool:
    return bool(settings.kimi_api_key)


def call_kimi_chat(
    system_prompt: str,
    user_prompt: str,
    *,
    max_completion_tokens: int = 1800,
    response_format: dict[str, Any] | None = None,
) -> str:
    if not settings.kimi_api_key:
        raise KimiClientError("KIMI_API_KEY is not configured.")

    payload: dict[str, Any] = {
        "model": settings.kimi_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_completion_tokens": max_completion_tokens,
    }
    if response_format:
        payload["response_format"] = response_format

    endpoint = f"{settings.kimi_base_url}/chat/completions"
    try:
        with httpx.Client(timeout=settings.kimi_timeout_seconds) as client:
            response = client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {settings.kimi_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:800] if exc.response is not None else ""
        raise KimiClientError(f"Kimi API returned HTTP {exc.response.status_code}: {body}") from exc
    except Exception as exc:
        raise KimiClientError(f"Kimi API request failed: {exc}") from exc

    try:
        return str(data["choices"][0]["message"]["content"] or "").strip()
    except Exception as exc:
        raise KimiClientError(f"Kimi API response did not contain message content: {json.dumps(data)[:800]}") from exc
