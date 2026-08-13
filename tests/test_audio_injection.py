"""E5 dependency-injection tests for the ``AsyncFrameworkLoop`` audio-fetch port.

The Garage/MinIO audio fetch is made constructor-injectable via ``AudioFetchPort``
(mirroring the conductor + mixer_factory seams). The concrete ``GarageAudioAdapter``
already structurally satisfies ``ports.AudioFetchPort``; today it is reached
through the lazy ``_audio`` property. Ctor injection makes the framework core
depend on the abstraction, not the concrete adapter (CLAUDE.md dependency
inversion), with the real path byte-for-byte unchanged.

These pin:
- omitting ``audio`` resolves to the concrete ``GarageAudioAdapter`` AND keeps the
  lazy-env / ``_garage`` path (no eager client) — the contract's
  "default = GarageAudioAdapter(None)" is reached LAZILY via the property reading
  ``self._garage`` (None), never eagerly stored;
- an injected fake ``AudioFetchPort`` is stored and reached by ``_fetch_audio``;
- the pre-existing direct-assignment harness (``loop._audio_adapter = <fake>`` /
  ``loop._audio = <fake>`` / ``loop._fetch_audio = AsyncMock``) still works
  (the seam is additive, never breaks it).
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import numpy as np

from app.framework.audio_fetch import GarageAudioAdapter
from app.framework.loop_orchestrator import AsyncFrameworkLoop
from app.framework.ports import AudioFetchPort


class _FakeAudio:
    """In-memory AudioFetchPort stand-in — records calls, returns sentinel PCM."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch(self, audio_path: str) -> np.ndarray | None:
        self.calls.append(audio_path)
        return np.ones((64, 2), dtype=np.float32)


def test_loop_constructs_default_audio_adapter() -> None:
    """Omitting ``audio`` yields the concrete GarageAudioAdapter, built lazily.

    ``_audio_adapter`` stays None until the property is read, then it builds
    ``GarageAudioAdapter(_garage)`` (None here) — the Gap-3 / test_audio_fetch_guard
    lazy path stays intact.
    """
    loop = AsyncFrameworkLoop(uuid4())
    assert loop._audio_adapter is None  # lazy: nothing eager-built in __init__
    assert loop._garage is None  # the _garage attr is preserved
    adapter = loop._audio  # first read triggers the lazy build
    assert isinstance(adapter, GarageAudioAdapter)
    assert adapter._garage_client is None  # no eager env client creation


def test_default_audio_adapter_satisfies_port() -> None:
    """The lazily-built default adapter structurally satisfies AudioFetchPort."""
    loop = AsyncFrameworkLoop(uuid4())
    assert isinstance(loop._audio, AudioFetchPort)


def test_loop_accepts_injected_audio() -> None:
    """An injected AudioFetchPort is stored and surfaced by the property (real DI)."""
    fake = _FakeAudio()
    loop = AsyncFrameworkLoop(uuid4(), audio=fake)
    assert isinstance(fake, AudioFetchPort)  # structural Protocol satisfied
    assert loop._audio_adapter is fake  # stored verbatim
    assert loop._audio is fake  # property returns the injected fake


async def test_fetch_audio_uses_injected_audio() -> None:
    """_fetch_audio reaches the injected port (never the concrete adapter)."""
    fake = _FakeAudio()
    loop = AsyncFrameworkLoop(uuid4(), audio=fake)
    result = await loop._fetch_audio("audio/x.aac")
    assert fake.calls == ["audio/x.aac"]
    assert result is not None and result.shape == (64, 2)
    assert loop._audio is fake  # still the injected fake


async def test_fetch_audio_runtime_assignment_still_works() -> None:
    """The pre-existing ``loop._fetch_audio = AsyncMock`` harness still works."""
    loop = AsyncFrameworkLoop(uuid4())
    sentinel = np.ones((32, 2), dtype=np.float32)
    loop._fetch_audio = AsyncMock(return_value=sentinel)  # type: ignore[assignment]
    assert await loop._fetch_audio("audio/x.aac") is sentinel


def test_audio_adapter_direct_assignment_still_works() -> None:
    """The Gap-3 ``loop._audio_adapter = <fake>`` / ``= None`` reset harness works."""
    loop = AsyncFrameworkLoop(uuid4())
    fake = _FakeAudio()
    loop._audio_adapter = fake  # type: ignore[assignment]
    assert loop._audio is fake
    loop._audio_adapter = None  # reset -> property rebuilds from _garage
    assert isinstance(loop._audio, GarageAudioAdapter)


def test_audio_property_setter_direct_assignment_still_works() -> None:
    """The ``loop._audio = <fake>`` direct-assignment harness works (additive seam)."""
    loop = AsyncFrameworkLoop(uuid4())
    fake = _FakeAudio()
    loop._audio = fake  # type: ignore[assignment]
    assert loop._audio is fake
    assert loop._audio_adapter is fake  # the setter writes the backing attr
