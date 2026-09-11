"""E5 dependency-injection tests for the ``AsyncFrameworkLoop`` job-queue port.

The Postgres generator-job submit is made constructor-injectable via
``JobQueuePort`` (mirroring the conductor / mixer_factory / audio seams). The
concrete ``PostgresJobQueueAdapter`` wraps the existing module functions
(``submit_generator_job`` + ``await_jobs``) and structurally satisfies
``ports.JobQueuePort``. Ctor injection makes the framework core depend on the
abstraction, not the concrete adapter (CLAUDE.md dependency inversion), with the
real submit path byte-for-byte unchanged.

These pin:
- omitting ``jobs`` resolves EAGERLY to the concrete ``PostgresJobQueueAdapter``
  (unlike ``audio``, there is no lazy property — the adapter constructor is a
  no-op; the DB session opens lazily inside ``submit_generator_job`` at call
  time, so a default ``PostgresJobQueueAdapter()`` is safe to store eagerly);
- an injected fake ``JobQueuePort`` is stored and reached by ``_submit_job``
  (the real ``submit_generator_job`` is NOT called);
- the pre-existing direct-assignment harness (``loop._jobs = <fake>`` /
  ``loop._submit_job = AsyncMock``) still works (the seam is additive, never
  breaks it).
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from app.framework.job_queue import PostgresJobQueueAdapter
from app.framework.loop_orchestrator import AsyncFrameworkLoop
from app.framework.ports import JobQueuePort


class _FakeJobQueue:
    """In-memory JobQueuePort stand-in — records submit kwargs, returns a sentinel."""

    def __init__(self) -> None:
        self.submitted: list[dict[str, object]] = []

    async def submit(self, **kwargs: object) -> UUID:
        self.submitted.append(kwargs)
        return uuid4()

    async def await_jobs(self, job_ids: list[UUID], timeout: float = 120.0) -> dict[UUID, str | None]:
        return {}

    # REL-12 grew the port with the queue-lifecycle members; the structural
    # isinstance assertions in this file require the fake to declare them too.
    async def abandon_jobs(self, job_ids: list[UUID]) -> int:
        return len(job_ids)

    async def pending_depth(self) -> int:
        return 0


def test_loop_constructs_default_jobs_adapter() -> None:
    """Omitting ``jobs`` yields the concrete PostgresJobQueueAdapter, eagerly.

    Unlike ``audio`` (lazy ``_audio`` property), the jobs default is a plain eager
    attribute: the adapter constructor is a no-op, and the DB session opens lazily
    inside ``submit_generator_job`` at call time.
    """
    loop = AsyncFrameworkLoop(uuid4())
    assert isinstance(loop._jobs, PostgresJobQueueAdapter)


def test_default_jobs_adapter_satisfies_port() -> None:
    """The default adapter structurally satisfies JobQueuePort."""
    loop = AsyncFrameworkLoop(uuid4())
    assert isinstance(loop._jobs, JobQueuePort)


def test_loop_accepts_injected_jobs() -> None:
    """An injected JobQueuePort is stored verbatim (real DI)."""
    fake = _FakeJobQueue()
    loop = AsyncFrameworkLoop(uuid4(), jobs=fake)
    assert isinstance(fake, JobQueuePort)  # structural Protocol satisfied
    assert loop._jobs is fake  # stored verbatim


async def test_submit_job_uses_injected_jobs(monkeypatch) -> None:
    """_submit_job reaches the injected port; submit_generator_job NOT called."""
    import app.framework.job_queue as jq_mod

    # Sentinel proving the real module function is never touched. The adapter
    # resolves the bare ``submit_generator_job`` name from its OWN module global,
    # so patching that global proves the injected port bypassed it.
    real_submit = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(jq_mod, "submit_generator_job", real_submit)

    fake = _FakeJobQueue()
    loop = AsyncFrameworkLoop(uuid4(), jobs=fake)
    result = await loop._submit_job(
        session_id=loop.session_id,
        instrument="Synth Lead",
        prompt="Synth, A minor, 128 BPM",
        major_family="Synth",
        model_id="foundation-1",
        key="A minor",
        bpm=128,
        timbre_tags=["warm"],
        bars=4,
    )
    assert len(fake.submitted) == 1
    assert fake.submitted[0]["instrument"] == "Synth Lead"
    assert fake.submitted[0]["bpm"] == 128
    assert result is not None  # the fake returned a UUID
    real_submit.assert_not_called()  # the real submit path was bypassed


async def test_submit_job_runtime_assignment_still_works() -> None:
    """The pre-existing ``loop._submit_job = AsyncMock`` harness still works."""
    loop = AsyncFrameworkLoop(uuid4())
    sentinel = uuid4()
    loop._submit_job = AsyncMock(return_value=sentinel)  # type: ignore[assignment]
    assert await loop._submit_job(session_id=loop.session_id) is sentinel


def test_jobs_direct_assignment_still_works() -> None:
    """The ``loop._jobs = <fake>`` direct-assignment harness works (additive seam)."""
    loop = AsyncFrameworkLoop(uuid4())
    fake = _FakeJobQueue()
    loop._jobs = fake  # type: ignore[assignment]
    assert loop._jobs is fake
    assert isinstance(loop._jobs, JobQueuePort)
