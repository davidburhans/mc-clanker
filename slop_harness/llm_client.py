"""Async LLM client with retry and exponential backoff.

Targets OpenAI-compatible APIs (LM Studio, Ollama, etc.).
Uses openai.AsyncOpenAI for identical plumbing to the mc-clanker app.
"""
import asyncio
import os
import sys
from typing import Any

from openai import AsyncOpenAI


# Build RESPONSE_FORMAT using the shared schema builder.
# Import from app.lib.constants when available (app is on the path).
# For slop_harness standalone use, add app to the path.
_srcdir = os.path.join(os.path.dirname(__file__), '..')
if os.path.exists(os.path.join(_srcdir, 'app', 'lib', 'constants.py')):
    sys.path.insert(0, _srcdir)

try:
    from app.lib.constants import get_response_format_schema
    _get_schema = get_response_format_schema  # Per-call, not at import time
except ImportError:
    _get_schema = None

# Fallback: inline the static schema (used when app.lib.constants is unavailable)
_STATIC_RESPONSE_FORMAT = {
        "type": "json_schema",
        "json_schema": {
            "name": "dj_action_state",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "master_bpm": {"type": "integer", "enum": [100, 110, 120, 128, 130, 140, 150]},
                    "master_key": {"type": "string", "enum": ["C major", "C minor", "C# major", "C# minor", "D major", "D minor", "D# major", "D# minor", "E major", "E minor", "F major", "F minor", "F# major", "F# minor", "G major", "G minor", "G# major", "G# minor", "A major", "A minor", "A# major", "A# minor", "B major", "B minor"]},
                    "actions": {
                        "type": "array",
                        "items": {
                            "anyOf": [
                                {
                                    "type": "object",
                                    "properties": {
                                        "action_type": {"type": "string", "const": "retain"},
                                        "stem_index": {"type": "integer"}
                                    },
                                    "required": ["action_type", "stem_index"],
                                    "additionalProperties": False
                                },
                                {
                                    "type": "object",
                                    "properties": {
                                        "action_type": {"type": "string", "const": "add"},
                                        "model_id": {"type": "string"},
                                        "major_family": {"type": "string"},
                                        "sub_family": {"type": "string"},
                                        "timbre_tags": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                                        "notation_tag": {"type": "string"},
                                        "fx_tag": {"type": "string"},
                                        "bars": {"type": "integer", "enum": [4, 8]}
                                    },
                                    "required": ["action_type", "model_id", "major_family", "sub_family", "timbre_tags", "notation_tag", "fx_tag", "bars"],
                                    "additionalProperties": False
                                },
                                {
                                    "type": "object",
                                    "properties": {
                                        "action_type": {"type": "string", "const": "remove"},
                                        "stem_index": {"type": "integer"}
                                    },
                                    "required": ["action_type", "stem_index"],
                                    "additionalProperties": False
                                }
                            ]
                        }
                    },
                    "reasoning": {"type": "string"},
                    "name": {"type": "string"}
                },
                "required": ["master_bpm", "master_key", "actions", "reasoning", "name"],
                "additionalProperties": False
            }
        }
    }


class LLMClient:
    """Async LLM caller with exponential backoff retry — same client as mc-clanker app."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
    ):
        self.base_url = (base_url or os.environ.get(
            "LLM_BASE_URL", "http://localhost:1234/v1"
        )).rstrip("/")
        self.model = model or os.environ.get("LLM_MODEL", "local-model")
        self._client: AsyncOpenAI | None = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                base_url=self.base_url,
                api_key="not-needed",
                timeout=60.0,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def call(
        self,
        messages: list[dict[str, str]],
        max_retries: int = 3,
        extra_body: dict | None = None,
    ) -> str:
        """Call the LLM with exponential backoff retry.

        Args:
            messages: List of {"role": ..., "content": ...} message dicts
            extra_body: Optional dict passed as extra_body to the API (e.g.,
                {"chat_template_kwargs": {"enable_thinking": False}} for Qwen3.5-27B)

        Returns:
            The assistant's raw text content (usually JSON string)

        Raises:
            Exception: After max_retries consecutive failures
        """
        client = self._get_client()
        backoff = 1.0
        last_error: Exception | None = None

        for attempt in range(max_retries):
            try:
                response = await client.chat.completions.create(
                    model=self.model,
                    messages=messages,  # type: ignore[arg-type]
                    temperature=0.7,
                    response_format=_get_schema() if _get_schema else _STATIC_RESPONSE_FORMAT,  # type: ignore[arg-type]
                    extra_body=extra_body,
                )
                content = response.choices[0].message.content  # type: ignore[assignment]
                if content is None:
                    raise Exception("LLM returned empty content")
                return content

            except Exception as e:
                last_error = e
                err_str = str(e).lower()
                # Retry on rate limit or service unavailable
                if "429" in err_str or "503" in err_str or "rate_limit" in err_str:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(backoff)
                        backoff *= 2
                        continue
                # Other errors: no retry
                break

        raise Exception(
            f"LLM call failed after {max_retries} retries: {last_error}"
        )

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
