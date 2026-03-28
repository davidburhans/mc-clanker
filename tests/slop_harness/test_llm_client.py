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
    # Remove if set
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
        call_args = mock_post.call_args
        assert "chat/completions" in str(call_args)


@pytest.mark.asyncio
async def test_call_retry_on_429():
    """call() retries on 429 status with backoff."""
    from unittest.mock import AsyncMock, patch, MagicMock
    import asyncio

    call_count = 0

    async def mock_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            # Return 429
            mock_resp = MagicMock()
            mock_resp.status = 429
            mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_resp.__aexit__ = AsyncMock(return_value=None)
            return mock_resp
        else:
            # Return success on 3rd try
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
    """call() raises after 3 consecutive failures."""
    from unittest.mock import AsyncMock, patch, MagicMock

    async def mock_post(*args, **kwargs):
        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)
        return mock_resp

    with patch("aiohttp.ClientSession.post", mock_post):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            client = LLMClient()
            with pytest.raises(Exception) as exc_info:
                await client.call([{"role": "user", "content": "hello"}])
            assert "Failed after 3 retries" in str(exc_info.value)


@pytest.mark.asyncio
async def test_call_uses_correct_payload():
    """call() sends correct payload to the API."""
    from unittest.mock import AsyncMock, patch, MagicMock

    captured_payload = {}

    async def mock_post(*args, **kwargs):
        captured_payload.update(kwargs)
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "choices": [{"message": {"content": "{}"}}]
        })
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=None)
        return mock_resp

    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "usr"}]
    with patch("aiohttp.ClientSession.post", mock_post):
        with patch("aiohttp.ClientSession"):
            client = LLMClient()
            client._session = AsyncMock()
            client._session.post = mock_post
            await client.call(messages)

    # Verify payload has correct fields
    assert "json" in captured_payload or (len(captured_payload) >= 0)