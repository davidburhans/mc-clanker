"""E5 dependency-injection tests for the ``AsyncFrameworkLoop`` audit-sink port.

The show-audit buffering is made constructor-injectable via ``AuditSinkPort``
(mirroring the conductor / mixer_factory / audio / jobs seams). The concrete
``AuditAdapter`` wraps the existing module functions (``append_loop_audit`` +
``flush_recording_buffers``) and structurally satisfies ``ports.AuditSinkPort``.
Ctor injection makes the framework core depend on the abstraction, not the
concrete adapter (CLAUDE.md dependency inversion), with the real append path
byte-for-byte unchanged.

These pin:
- omitting ``audit`` resolves EAGERLY to the concrete ``AuditAdapter`` (like the
  jobs seam, there is no lazy property — the adapter constructor is a no-op; the
  module-level ``_flush_lock`` + ``state.lock`` are owned by the module
  functions, not the adapter);
- an injected fake ``AuditSinkPort`` is stored and reached by
  ``_append_loop_audit`` (the real ``append_loop_audit`` module function is NOT
  called);
- the pre-existing direct-assignment harness (``loop._audit = <fake>`` /
  ``loop._append_loop_audit = AsyncMock``) still works (the seam is additive,
  never breaks it).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

from app.framework.audit_recording import AuditAdapter
from app.framework.loop_orchestrator import AsyncFrameworkLoop
from app.framework.ports import AuditSinkPort


class _FakeAuditSink:
    """In-memory AuditSinkPort stand-in — records append_loop kwargs."""

    def __init__(self) -> None:
        self.appended: list[dict[str, Any]] = []

    async def append_loop(
        self, conductor_response: dict[str, Any], active_stems: list[dict[str, Any]], loop_idx: int
    ) -> None:
        self.appended.append(
            {
                "conductor_response": conductor_response,
                "active_stems": active_stems,
                "loop_idx": loop_idx,
            }
        )

    async def flush(self) -> None:
        """No-op flush (structurally satisfies AuditSinkPort; U4 wires flush into routes)."""
        return None


def _conductor_response() -> dict[str, Any]:
    """Minimal conductor decision payload (matches the append_loop_audit shape)."""
    return {
        "name": "Test State",
        "master_bpm": 128,
        "master_key": "A minor",
        "actions": [{"action": "retain", "stem_index": 0}],
        "reasoning": "keep the groove going",
    }


def _active_stems() -> list[dict[str, Any]]:
    """One active stem, matching the stem dict shape append_loop_audit reads."""
    return [
        {
            "instrument": "Synth Lead",
            "prompt": "Synth Lead, A minor, 128 BPM",
            "major_family": "Synth",
            "model_id": "foundation-1",
            "bpm": 128,
            "key": "A minor",
            "bars": 4,
        }
    ]


def test_loop_constructs_default_audit_adapter() -> None:
    """Omitting ``audit`` yields the concrete AuditAdapter, eagerly.

    Like the jobs seam (and unlike the lazy ``audio`` property), the audit
    default is a plain eager attribute: the adapter constructor is a no-op, and
    the module-level ``_flush_lock`` + ``state.lock`` are owned by the wrapped
    module functions, not the adapter.
    """
    loop = AsyncFrameworkLoop(uuid4())
    assert isinstance(loop._audit, AuditAdapter)


def test_default_audit_adapter_satisfies_port() -> None:
    """The default adapter structurally satisfies AuditSinkPort."""
    loop = AsyncFrameworkLoop(uuid4())
    assert isinstance(loop._audit, AuditSinkPort)


def test_loop_accepts_injected_audit() -> None:
    """An injected AuditSinkPort is stored verbatim (real DI)."""
    fake = _FakeAuditSink()
    loop = AsyncFrameworkLoop(uuid4(), audit=fake)
    assert isinstance(fake, AuditSinkPort)  # structural Protocol satisfied
    assert loop._audit is fake  # stored verbatim


async def test_append_loop_audit_uses_injected_audit(monkeypatch) -> None:
    """_append_loop_audit reaches the injected port; append_loop_audit NOT called."""
    import app.framework.audit_recording as ar_mod

    # Sentinel proving the real module function is never touched. The adapter
    # resolves the bare ``append_loop_audit`` name from its OWN module global,
    # so patching that global proves the injected port bypassed it.
    real_append = AsyncMock()
    monkeypatch.setattr(ar_mod, "append_loop_audit", real_append)

    fake = _FakeAuditSink()
    loop = AsyncFrameworkLoop(uuid4(), audit=fake)
    await loop._append_loop_audit(_conductor_response(), _active_stems(), loop_idx=3)

    assert len(fake.appended) == 1
    assert fake.appended[0]["loop_idx"] == 3
    assert fake.appended[0]["conductor_response"]["master_bpm"] == 128
    real_append.assert_not_called()  # the real append path was bypassed


async def test_append_loop_audit_runtime_assignment_still_works() -> None:
    """The pre-existing ``loop._append_loop_audit = AsyncMock`` harness still works."""
    loop = AsyncFrameworkLoop(uuid4())
    loop._append_loop_audit = AsyncMock(return_value=None)  # type: ignore[assignment]
    await loop._append_loop_audit(_conductor_response(), _active_stems(), loop_idx=1)
    loop._append_loop_audit.assert_awaited_once()  # type: ignore[attr-defined]


def test_audit_direct_assignment_still_works() -> None:
    """The ``loop._audit = <fake>`` direct-assignment harness works (additive seam)."""
    loop = AsyncFrameworkLoop(uuid4())
    fake = _FakeAuditSink()
    loop._audit = fake  # type: ignore[assignment]
    assert loop._audit is fake
    assert isinstance(loop._audit, AuditSinkPort)
