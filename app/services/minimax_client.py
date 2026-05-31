from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.config import settings


class MiniMaxClientError(Exception):
    pass


def minimax_configured() -> bool:
    return bool(settings.minimax_api_key)


def call_minimax_chat(
    system_prompt: str,
    user_prompt: str,
    *,
    max_completion_tokens: int = 1800,
    response_format: dict[str, Any] | None = None,
) -> str:
    if not settings.minimax_api_key:
        raise MiniMaxClientError("MINIMAX_API_KEY is not configured.")

    payload: dict[str, Any] = {
        "model": settings.minimax_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_completion_tokens,
        "reasoning_split": True,
        "temperature": 1.0,
    }
    if response_format:
        payload["response_format"] = response_format

    endpoint = f"{settings.minimax_base_url}/chat/completions"
    try:
        with httpx.Client(timeout=settings.minimax_timeout_seconds) as client:
            response = client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {settings.minimax_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:800] if exc.response is not None else ""
        raise MiniMaxClientError(f"MiniMax API returned HTTP {exc.response.status_code}: {body}") from exc
    except Exception as exc:
        raise MiniMaxClientError(f"MiniMax API request failed: {exc}") from exc

    try:
        return _strip_thinking(str(data["choices"][0]["message"]["content"] or "")).strip()
    except Exception as exc:
        raise MiniMaxClientError(f"MiniMax response did not contain message content: {json.dumps(data)[:800]}") from exc


def _strip_thinking(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"^\s*<think>.*", "", text, flags=re.DOTALL | re.IGNORECASE)
    return text
