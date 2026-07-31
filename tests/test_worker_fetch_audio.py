"""
RED Test: test_fetch_audio_decodes_aac_from_garage

This test will FAIL because _fetch_audio() in framework_main_async.py
is a stub that returns None instead of downloading and decoding audio.
"""

import pytest
from unittest.mock import AsyncMock, patch
from uuid import uuid4
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

        loop = AsyncFrameworkLoop(session_id=uuid4())

        # Mock garage client that returns fake AAC bytes
        mock_garage = MagicMock()

        async def mock_get_object(x):
            return b"fake aac data"

        mock_garage.get_object = mock_get_object

        # Mock decode_aac to return fake audio
        fake_audio = np.zeros(44100, dtype=np.float32)

        async def run_test():
            with patch('app.framework.audio_fetch.create_garage_client_from_env', return_value=mock_garage):
                with patch('app.framework.audio_fetch.decode_aac', return_value=fake_audio):
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
            with patch('app.framework.audio_fetch.create_garage_client_from_env', return_value=mock_garage):
                with patch('app.framework.audio_fetch.decode_aac', return_value=np.zeros(1000)):
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
        """D12: decode_aac round-trips AAC bytes to a numpy array.

        This was an unconditional ``pytest.skip`` ("manual test only"), so the
        AAC encode/decode bridge had ZERO executed coverage. It now encodes a
        real sine wave with ``encode_aac`` and decodes it back, skipping ONLY
        when ffmpeg is genuinely absent.
        """
        import shutil
        import numpy as np

        from app.aac_encoder import encode_aac, decode_aac

        if shutil.which("ffmpeg") is None:
            pytest.skip("ffmpeg not installed; cannot round-trip AAC")

        sample_rate = 44100
        # 0.5 s mono sine wave, amplitude in [-1, 1]
        samples = int(sample_rate * 0.5)
        t = np.linspace(0, 0.5, samples, endpoint=False)
        audio = (0.5 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)

        aac_bytes = encode_aac(audio, sample_rate=sample_rate)
        assert isinstance(aac_bytes, bytes) and aac_bytes, "encode_aac must return bytes"

        decoded = decode_aac(aac_bytes, sample_rate=sample_rate)
        assert isinstance(decoded, np.ndarray), (
            f"decode_aac must return np.ndarray, got {type(decoded)}"
        )
        assert decoded.size > 0, "decoded audio must be non-empty"
        # Mono round-trip: first axis is sample count, ~original length (codec
        # framing may trim/pad a handful of samples).
        assert abs(decoded.shape[0] - samples) < samples * 0.1, (
            f"decoded length {decoded.shape[0]} far from encoded {samples}"
        )
