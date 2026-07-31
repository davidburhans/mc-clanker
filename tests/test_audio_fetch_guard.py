"""Guard: string-patches on ``app.framework.audio_fetch`` resolve at the call site.

Regression guard for Phase 2 of refactor/plan.md (brief-02 §D). Before the
extract, ``decode_aac`` / ``create_garage_client_from_env`` were imported in
``framework_main_async`` and tests string-patched that module. If those imports
ever drift out of ``audio_fetch``'s namespace, a patch silently becomes a no-op
(the test passes without exercising anything). Asserting the mock is *invoked*
proves the patch reaches the real fetch call site.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import numpy as np

from app.framework.framework_main_async import AsyncFrameworkLoop


async def test_decode_aac_patch_actually_applies() -> None:
    """Patching ``audio_fetch.decode_aac`` must intercept the real fetch call."""
    loop = AsyncFrameworkLoop(uuid4())
    fake_garage = AsyncMock()
    fake_garage.get_object = AsyncMock(return_value=b"fake aac bytes")
    loop._garage = fake_garage  # preset so the adapter reuses it (no env client)

    with patch("app.framework.audio_fetch.decode_aac", return_value=np.zeros((100, 2), dtype="float32")) as mocked:
        result = await loop._fetch_audio("audio/x.aac")

    assert result is not None, "fetch should decode the fake bytes, not return None"
    mocked.assert_called_once(), "patch must reach the real decode call site (else silent no-op)"


async def test_garage_client_patch_actually_applies() -> None:
    """Patching ``audio_fetch.create_garage_client_from_env`` must supply the client."""
    loop = AsyncFrameworkLoop(uuid4())  # _garage stays None -> adapter lazy-creates

    fake_garage = AsyncMock()
    fake_garage.get_object = AsyncMock(return_value=b"fake aac bytes")

    with patch("app.framework.audio_fetch.create_garage_client_from_env", return_value=fake_garage) as mk_client:
        with patch("app.framework.audio_fetch.decode_aac", return_value=np.zeros((10, 2), dtype="float32")):
            result = await loop._fetch_audio("audio/y.aac")

    assert result is not None
    mk_client.assert_called_once(), "patch must reach the real client-creation call site"
