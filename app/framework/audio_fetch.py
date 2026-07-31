"""Garage/MinIO S3 audio-fetch adapter (impure).

Owns the S3 ``get_object`` + AAC decode path so the framework loop depends on a
thin adapter instead of reaching into boto3 / ffmpeg directly. This is the only
module that resolves ``decode_aac`` / ``create_garage_client_from_env`` for the
fetch path — test string-patches therefore target THIS module's namespace
(``app.framework.audio_fetch.decode_aac``) so a patch replaces the name the
fetch path actually reads (brief-02 §D, eliminates the silent no-op risk).
"""

from __future__ import annotations

import asyncio

import numpy as np

# Imported at MODULE TOP (not inside the method) on purpose: a string-patch on
# this module's namespace must replace the name the fetch path resolves. Moving
# these imports inside fetch() would silently break every such patch.
from app.aac_encoder import decode_aac
from app.garage_client import create_garage_client_from_env


class GarageAudioAdapter:
    """Fetch + decode one audio object from Garage/MinIO into a numpy array.

    A garage client may be injected (tests preset ``loop._garage``); when none is
    provided the client is created lazily from env on first use and cached.
    """

    def __init__(self, garage_client=None) -> None:
        self._garage_client = garage_client

    async def fetch(self, audio_path: str) -> np.ndarray | None:
        """Return decoded float32 audio ``[samples, channels]``, or ``None``.

        Empty bytes -> ``None`` (no decode attempted); any fetch/decode error is
        swallowed and logged -> ``None``. Byte-identical to the old inline path.
        """
        try:
            if self._garage_client is None:
                self._garage_client = create_garage_client_from_env()
            aac_bytes = await self._garage_client.get_object(audio_path)

            if not aac_bytes:
                print(f"[AsyncFrameworkLoop] No audio data received from Garage: {audio_path}")
                return None

            # Decode AAC to numpy array (runs in thread pool since it's blocking)
            loop = asyncio.get_running_loop()
            audio_data = await loop.run_in_executor(None, lambda: decode_aac(aac_bytes, sample_rate=44100))

            print(f"[AsyncFrameworkLoop] Fetched and decoded audio from: {audio_path}")
            return audio_data

        except Exception as e:  # noqa: BLE001  # intentional: fetch/decode failures -> None, never crash the loop
            print(f"[AsyncFrameworkLoop] Failed to fetch audio from Garage: {e}")
            return None
