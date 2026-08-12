"""Hexagonal port interfaces for the async framework (E5).

These ``Protocol`` classes declare the contracts the loop orchestrator depends
on. Each has exactly one production adapter today; declaring the ports up front
lets the orchestrator (and tests) depend on the abstraction, not the concrete
adapter — satisfying the dependency-inversion rule in CLAUDE.md and making the
framework core testable with fakes.

Design notes:
- All ports use structural typing (``Protocol``).
- ``ConductorPort``: WIRED. ``AsyncFrameworkLoop.__init__(session_id, *,
  conductor=None)`` injects against this Protocol (default ``ConductorLLMAsync``,
  which STRUCTURALLY satisfies it after its ``param = None`` type lies were
  corrected). The framework core depends on the abstraction, not the concrete
  class — satisfying the CLAUDE.md dependency-inversion rule.
- ``JobQueuePort`` / ``AudioFetchPort`` / ``AuditSinkPort``: documented contracts.
  The concrete adapters are wired today through the orchestrator's delegate
  methods (``_submit_job`` / ``_fetch_audio`` / ``_append_loop_audit``), which
  are the runtime injection seam tests use via ``patch.object``. Promoting them
  to constructor-injected port objects is a future enhancement.
- ``MixerController`` is declared for documentation/typing only in this pass:
  the concrete ``Mixer`` is NOT yet fully behind it (the orchestrator still
  reaches a few private members at P10/P13 — see refactor/plan.md Phase 11,
  default-deferred). It is included so future extraction is a drop-in.
- Modern typing only (no ``typing.List``/``Dict``/``Optional``).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import numpy as np


@runtime_checkable
class ConductorPort(Protocol):
    """Drives the LLM that decides the next loop's arrangement."""

    async def get_next_state_async(
        self,
        current_bpm: int,
        current_key: str,
        active_stems: list[dict[str, Any]],
        user_override: str | None = "",
        available_instruments: list[str] | None = None,
        stem_history: list[list[dict[str, Any]]] | None = None,
        llm_config: dict[str, Any] | None = None,
        available_models: list[dict[str, Any]] | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return the conductor's decision (master_bpm/key/actions/reasoning/...)."""
        ...


@runtime_checkable
class JobQueuePort(Protocol):
    """Submits generator jobs and awaits their completion (Postgres + LISTEN/NOTIFY)."""

    async def submit(
        self,
        *,
        session_id: UUID,
        instrument: str,
        prompt: str,
        major_family: str,
        model_id: str,
        key: str,
        bpm: int,
        timbre_tags: list[str],
        bars: int,
    ) -> UUID:
        """Insert one pending ``GeneratorJob`` row and return its id."""
        ...

    async def await_jobs(self, job_ids: list[UUID], timeout: float = 120.0) -> dict[UUID, str | None]:
        """Block until the jobs complete; return ``{job_id: audio_path_or_None}``."""
        ...


@runtime_checkable
class AudioFetchPort(Protocol):
    """Fetches one stem's audio bytes from object storage and decodes to PCM."""

    async def fetch(self, audio_path: str) -> np.ndarray | None:
        """Return a ``(samples, 2)`` float32 ndarray, or ``None`` on miss/error."""
        ...


@runtime_checkable
class AuditSinkPort(Protocol):
    """Buffers and flushes the show audit trail (LLM interactions + actions)."""

    async def append_loop(
        self,
        conductor_response: dict[str, Any],
        active_stems: list[dict[str, Any]],
        loop_idx: int,
    ) -> None:
        """Buffer one loop's LLM interaction + per-action rows."""
        ...

    async def flush(self) -> None:
        """Bulk-insert buffered rows; re-queue on failure."""
        ...


@runtime_checkable
class MixerController(Protocol):
    """Real-time audio mixer surface the orchestrator coordinates with.

    P11-U3 COMPLETE: ``prime_loop`` and ``loop_position_seconds`` now ENCAPSULATE
    the P10 loop-1 batch and the P13 boundary read. The orchestrator no longer
    reaches the concrete ``Mixer`` privates (``lock``/``_add_track_internal``/
    ``_ensure_stereo``/``_current_loop_duration``) directly — it calls these two
    methods instead. The privates remain solely as the backing implementation
    inside ``Mixer``. The lock acquired inside both methods is the SAME
    ``threading.Lock`` the daemon ``_callback`` holds during the crossfade, so the
    dual-lock timing is preserved.

    P11-U4 COMPLETE: the orchestrator now ctor-injects the mixer via a FACTORY
    (``AsyncFrameworkLoop(session_id, *, mixer_factory=...)``); the default
    factory IS the concrete ``Mixer`` class, so the real audio path is unchanged
    (closes R14 / the E5 DI goal for the mixer surface).
    """

    sample_rate: int
    current_sample: int
    current_loop_end_sample: int

    def set_next_loop(
        self,
        tracks: list[np.ndarray],
        *,
        next_loop_duration_samples: int,
        loop_idx: int,
    ) -> None: ...

    def pop_transition_event(self) -> object | None: ...

    def clear(self) -> None: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def prime_loop(self, tracks: list[tuple[np.ndarray, int]], *, duration_samples: int) -> None: ...

    def loop_position_seconds(self) -> float: ...
