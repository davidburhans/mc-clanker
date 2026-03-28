"""Async LLM client with retry and exponential backoff.

Targets OpenAI-compatible APIs (LM Studio, Ollama, etc.).
"""
import asyncio
import os
from typing import Any

import aiohttp


class LLMClient:
    """Async LLM caller with exponential backoff retry."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
    ):
        self.base_url = base_url or os.environ.get(
            "LLM_BASE_URL", "http://localhost:1234/v1"
        ).rstrip("/")
        self.model = model or os.environ.get("LLM_MODEL", "local-model")
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def call(
        self,
        messages: list[dict[str, str]],
        max_retries: int = 3,
    ) -> str:
        """Call the LLM with exponential backoff retry.

        Args:
            messages: List of {"role": ..., "content": ...} message dicts

        Returns:
            The assistant's raw text content (usually JSON string)

        Raises:
            Exception: After max_retries consecutive failures
        """
        session = await self._get_session()
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 2048,
        }

        backoff = 1.0
        last_error: Exception | None = None

        for attempt in range(max_retries):
            try:
                async with await session.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as response:
                    if response.status == 429 or response.status == 503:
                        # Retry with backoff
                        await asyncio.sleep(backoff)
                        backoff *= 2
                        continue

                    if response.status != 200:
                        text = await response.text()
                        raise Exception(
                            f"LLM API error {response.status}: {text[:200]}"
                        )

                    data = await response.json()
                    return data["choices"][0]["message"]["content"]

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = e
                if attempt < max_retries - 1:
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                break

        raise Exception(
            f"LLM call failed after {max_retries} retries: {last_error}"
        )

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()