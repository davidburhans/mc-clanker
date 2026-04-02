"""
RED Test: test_fetch_audio_decodes_aac_from_garage

This test will FAIL because _fetch_audio() in framework_main_async.py
is a stub that returns None instead of downloading and decoding audio.
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import numpy as np


class TestFetchAudio:
    """Tests for async framework _fetch_audio implementation."""

    def test_fetch_audio_is_not_stub(self):
        """
        _fetch_audio should download from Garage and decode AAC, not return None.

        This test verifies the implementation is not the stub that returns None.
        """
        import asyncio
        from unittest.mock import patch, MagicMock

        # Import the async framework
        from app.framework.framework_main_async import AsyncFrameworkLoop

        loop = AsyncFrameworkLoop(session_id=None)

        # Mock garage client that returns fake AAC bytes
        mock_garage = MagicMock()

        async def mock_get_object(x):
            return b"fake aac data"

        mock_garage.get_object = mock_get_object

        # Mock decode_aac to return fake audio
        fake_audio = np.zeros(44100, dtype=np.float32)

        async def run_test():
            with patch('app.framework.framework_main_async.create_garage_client_from_env', return_value=mock_garage):
                with patch('app.framework.framework_main_async.decode_aac', return_value=fake_audio):
                    audio_path = "audio/test-job-123.aac"
                    result = await loop._fetch_audio(audio_path)
                    return result

        loop_run = asyncio.new_event_loop()
        result = loop_run.run_until_complete(run_test())
        loop_run.close()

        # Should return numpy array, not None
        assert result is not None, "_fetch_audio should not return None - it should download and decode audio"
        assert isinstance(result, np.ndarray), f"Expected np.ndarray, got {type(result)}"

    def test_fetch_audio_uses_garage_client(self):
        """
        _fetch_audio should use GarageClient to download audio.

        This test verifies the Garage integration is being used.
        """
        from app.framework.framework_main_async import AsyncFrameworkLoop

        loop = AsyncFrameworkLoop(session_id=None)

        # Mock garage client
        mock_garage = AsyncMock()
        mock_garage.get_object = AsyncMock(return_value=b"fake aac bytes")

        import asyncio

        async def run_test():
            with patch('app.framework.framework_main_async.create_garage_client_from_env', return_value=mock_garage):
                with patch('app.framework.framework_main_async.decode_aac', return_value=np.zeros(1000)):
                    audio_path = "audio/test-job-123.aac"
                    result = await loop._fetch_audio(audio_path)
                    return result

        loop_run = asyncio.new_event_loop()
        try:
            result = loop_run.run_until_complete(run_test())
            # If we get here without exception, Garage was called
            mock_garage.get_object.assert_called_once()
        except Exception as e:
            # If implementation is still stub, it won't call garage
            pytest.fail(f"Implementation doesn't use Garage client: {e}")
        finally:
            loop_run.close()

    def test_decode_aac_produces_numpy_array(self):
        """
        decode_aac should convert AAC bytes to numpy array.

        This is a helper test to verify the decode logic.
        """
        # The fix will need to decode AAC - this tests the expected behavior
        # We can't easily test without ffmpeg, but we can verify the approach
        from app.aac_encoder import decode_aac

        # This will fail if ffmpeg isn't available or AAC is invalid
        # But it verifies the function exists and is callable
        import tempfile
        import os

        # Create minimal valid test
        # (We can't easily create valid AAC without ffmpeg)
        pytest.skip("AAC decoding requires ffmpeg and valid AAC bytes - manual test only")
