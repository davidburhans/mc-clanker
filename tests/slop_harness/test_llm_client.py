from unittest.mock import AsyncMock, patch

import pytest
from slop_harness.llm_client import LLMClient


def test_client_reads_env_vars():
    """Client reads LLM_BASE_URL and LLM_MODEL from env."""
    import os
    os.environ["LLM_BASE_URL"] = "http://test:999/v1"
    os.environ["LLM_MODEL"] = "test-model"
    client = LLMClient()
    assert client.base_url == "http://test:999/v1"
    assert client.model == "test-model"
    del os.environ["LLM_BASE_URL"]
    del os.environ["LLM_MODEL"]


def test_client_default_values():
    """Client uses defaults when env vars not set."""
    import os
    old_base = os.environ.pop("LLM_BASE_URL", None)
    old_model = os.environ.pop("LLM_MODEL", None)
    client = LLMClient()
    assert client.base_url == "http://localhost:1234/v1"
    assert client.model == "local-model"
    if old_base:
        os.environ["LLM_BASE_URL"] = old_base
    if old_model:
        os.environ["LLM_MODEL"] = old_model


@pytest.mark.asyncio
async def test_call_returns_content():
    """call() returns the assistant message content string."""
    from unittest.mock import AsyncMock, patch, MagicMock

    mock_response = MagicMock()
    mock_response.choices = [MagicMock(message=MagicMock(content='{"actions": [], "reasoning": "test"}'))]

    mock_create = AsyncMock(return_value=mock_response)

    mock_client = MagicMock()
    mock_client.chat.completions.create = mock_create

    with patch.object(LLMClient, "_get_client", return_value=mock_client):
        client = LLMClient()
        result = await client.call([{"role": "user", "content": "hello"}])
        assert result == '{"actions": [], "reasoning": "test"}'
        mock_create.assert_called_once()


@pytest.mark.asyncio
async def test_call_retry_on_429():
    """call() retries on 429 status with backoff."""
    from unittest.mock import AsyncMock, patch, MagicMock

    call_count = 0

    async def mock_create(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise Exception("429 rate limit")
        else:
            mock_resp = MagicMock()
            mock_resp.choices = [MagicMock(message=MagicMock(content='{"ok": true}'))]
            return mock_resp

    mock_client = MagicMock()
    mock_client.chat.completions.create = mock_create

    with patch.object(LLMClient, "_get_client", return_value=mock_client):
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            client = LLMClient()
            result = await client.call([{"role": "user", "content": "hello"}])
            assert result == '{"ok": true}'
            assert call_count == 3
            assert mock_sleep.call_count == 2


@pytest.mark.asyncio
async def test_call_raises_after_3_failures():
    """call() raises after 3 consecutive failures (rate-limit errors)."""
    from unittest.mock import MagicMock

    call_count = 0

    async def mock_create(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise Exception("429 rate limit exceeded")

    mock_client = MagicMock()
    mock_client.chat.completions.create = mock_create

    with patch.object(LLMClient, "_get_client", return_value=mock_client):
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            client = LLMClient()
            with pytest.raises(Exception) as exc_info:
                await client.call([{"role": "user", "content": "hello"}])
            assert "LLM call failed after 3 retries" in str(exc_info.value)
            assert call_count == 3
            assert mock_sleep.call_count == 2


@pytest.mark.asyncio
async def test_call_uses_correct_payload():
    """call() sends correct payload to the API."""
    from unittest.mock import MagicMock

    captured_kwargs = {}

    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content="{}"))]

    async def mock_create(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return mock_resp

    mock_client = MagicMock()
    mock_client.chat.completions.create = mock_create

    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "usr"}]

    with patch.object(LLMClient, "_get_client", return_value=mock_client):
        client = LLMClient()
        await client.call(messages)
    assert "response_format" in captured_kwargs
    assert captured_kwargs["model"] == "local-model"
