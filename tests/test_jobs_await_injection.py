"""U4 TDD-red pin tests: route ``JobQueuePort.await_jobs`` through the orchestrator.

This is the LAST unit completing R14 (all 5 ports ctor-injected AND reached
through their port abstraction). The loop's two await sites
(``loop_steps._step_await_jobs_fetch`` + ``pregeneration.run_pregeneration``)
currently call ``wait_for_multiple_jobs`` via a DIRECT module import — a
double-binding landmine (each module scope is patched separately in the
characterization tests). U4 adds an ``_await_jobs`` delegate on
``AsyncFrameworkLoop`` (mirroring ``_submit_job``/``_fetch_audio``/
``_append_loop_audit``) that routes through ``self._jobs.await_jobs`` (the
ctor-injected ``JobQueuePort``, defaults to ``PostgresJobQueueAdapter``).

TDD-red: these pin tests FAIL before impl because ``AsyncFrameworkLoop`` exposes
no ``_await_jobs`` delegate yet (AttributeError). Once the delegate is added they
go green, proving the await path is reached through the port and the real
``wait_for_multiple_jobs`` is bypassed.

The pins mirror ``tests/test_jobs_injection.py`` (U2 submit-port pins):
- (a) ``_await_jobs`` delegates to ``self._jobs.await_jobs`` with identical kwargs;
- (b) an injected fake ``JobQueuePort`` controls the await result AND the real
  ``wait_for_multiple_jobs`` is NOT called (the landmine pin);
- (c) the pre-existing direct-assignment harness (``loop._await_jobs = AsyncMock``)
  still works (the seam is additive, never breaks it).
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from app.framework.loop_orchestrator import AsyncFrameworkLoop


class _FakeJobQueue:
    """In-memory JobQueuePort stand-in — records submit + await, returns a sentinel.

    Mirrors ``tests/test_jobs_injection.py._FakeJobQueue`` and extends it with an
    ``await_result`` ctor param + an ``awaited`` record, so U4's await pins can
    assert both the kwargs forwarded to the port and the canned return shape.
    """

    def __init__(self, *, await_result: dict[UUID, str | None] | None = None) -> None:
        self.submitted: list[dict[str, object]] = []
        self.awaited: list[dict[str, object]] = []
        self._await_result: dict[UUID, str | None] = {} if await_result is None else await_result

    async def submit(self, **kwargs: object) -> UUID:
        self.submitted.append(kwargs)
        return uuid4()

    async def await_jobs(
        self,
        job_ids: list[UUID],
        timeout: float = 120.0,
    ) -> dict[UUID, str | None]:
        self.awaited.append({"job_ids": list(job_ids), "timeout": timeout})
        return self._await_result

    # REL-12 grew the port with the queue-lifecycle members; this fake mirrors
    # tests/test_jobs_injection.py._FakeJobQueue and declares them too.
    async def abandon_jobs(self, job_ids: list[UUID]) -> int:
        return len(job_ids)

    async def pending_depth(self) -> int:
        return 0


async def test_await_jobs_delegates_to_injected_jobs() -> None:
    """_await_jobs routes through self._jobs.await_jobs with identical kwargs."""
    job_id = uuid4()
    fake = _FakeJobQueue(await_result={job_id: "audio/x.aac"})
    loop = AsyncFrameworkLoop(uuid4(), jobs=fake)

    result = await loop._await_jobs([job_id], timeout=99.0)

    assert result == {job_id: "audio/x.aac"}
    # kwargs forwarded verbatim — the canned return shape is preserved exactly.
    assert fake.awaited == [{"job_ids": [job_id], "timeout": 99.0}]


async def test_await_jobs_uses_port_not_wait_for_multiple_jobs(monkeypatch) -> None:
    """Injected port controls the result; wait_for_multiple_jobs is never reached.

    This is the landmine pin: after the rewire, the loop must reach the await
    result through ``self._jobs.await_jobs``, NOT the module ``wait_for_multiple_jobs``
    the loop_steps/pregeneration paths used to import directly. A sentinel
    asserting ``assert_not_called`` proves the double-binding is broken for good.
    """
    import app.job_waiter as jw

    real = AsyncMock(return_value="MUST-NOT-CALL")
    monkeypatch.setattr(jw, "wait_for_multiple_jobs", real)

    job_id = uuid4()
    fake = _FakeJobQueue(await_result={job_id: "audio/x.aac"})
    loop = AsyncFrameworkLoop(uuid4(), jobs=fake)

    result = await loop._await_jobs([job_id])

    assert result == {job_id: "audio/x.aac"}
    real.assert_not_called()


async def test_await_jobs_runtime_assignment_still_works() -> None:
    """The pre-existing ``loop._await_jobs = AsyncMock(...)`` harness works.

    The delegate is an additive seam: direct-assignment harnesses (used by the
    characterization + divergence tests that will migrate to patch this delegate)
    keep working because a method on the host can always be shadowed by an
    instance attribute.
    """
    loop = AsyncFrameworkLoop(uuid4())
    canned: dict[UUID, str | None] = {uuid4(): "audio/y.aac"}
    loop._await_jobs = AsyncMock(return_value=canned)  # type: ignore[assignment]

    assert await loop._await_jobs([uuid4()]) is canned
