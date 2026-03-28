import asyncio

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
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={
        "choices": [{"message": {"content": '{"actions": [], "reasoning": "test"}'}}]
    })
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)

    mock_post = AsyncMock(return_value=mock_response)

    with patch("aiohttp.ClientSession.post", mock_post):
        client = LLMClient()
        result = await client.call([{"role": "user", "content": "hello"}])
        assert result == '{"actions": [], "reasoning": "test"}'
        mock_post.assert_called_once()


@pytest.mark.asyncio
async def test_call_retry_on_429():
    """call() retries on 429 status with backoff."""
    from unittest.mock import AsyncMock, patch, MagicMock

    call_count = 0

    async def mock_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            mock_resp = MagicMock()
            mock_resp.status = 429
            mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_resp.__aexit__ = AsyncMock(return_value=None)
            return mock_resp
        else:
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.json = AsyncMock(return_value={
                "choices": [{"message": {"content": '{"ok": true}'}}]
            })
            mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_resp.__aexit__ = AsyncMock(return_value=None)
            return mock_resp

    with patch("aiohttp.ClientSession.post", mock_post):
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            client = LLMClient()
            result = await client.call([{"role": "user", "content": "hello"}])
            assert result == '{"ok": true}'
            assert call_count == 3
            assert mock_sleep.call_count == 2


@pytest.mark.asyncio
async def test_call_raises_after_3_failures():
    """call() raises after 3 consecutive aiohttp failures."""
    from unittest.mock import MagicMock

    call_count = 0

    async def mock_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise asyncio.TimeoutError("simulated timeout")

    mock_session = MagicMock()
    mock_session.post = mock_post

    async def mock_get_session():
        return mock_session

    client = LLMClient()
    client._get_session = mock_get_session
    with pytest.raises(Exception) as exc_info:
        await client.call([{"role": "user", "content": "hello"}])
    assert "LLM call failed after 3 retries" in str(exc_info.value)
    assert call_count == 3


@pytest.mark.asyncio
async def test_call_uses_correct_payload():
    """call() sends correct payload to the API."""
    from unittest.mock import AsyncMock, MagicMock

    captured_payload = {}

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={
        "choices": [{"message": {"content": "{}"}}]
    })
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)

    mock_post = AsyncMock(return_value=mock_resp)

    def capture_post(*args, **kwargs):
        captured_payload.update(kwargs)
        return mock_post(*args, **kwargs)

    mock_session = MagicMock()
    mock_session.post = capture_post
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    async def mock_get_session():
        return mock_session

    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "usr"}]
    client = LLMClient()
    client._get_session = mock_get_session
    await client.call(messages)
    assert "json" in captured_payload
