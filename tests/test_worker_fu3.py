"""FU-3 worker regression suite (unit rel-fu-worker) — TDD-red.

Pins, all without a real GPU or model weights (harness mirrors
tests/test_worker_correctness.py + the B-series of tests/test_worker_vram.py):

- H1 (FU-3 item 1): the REL-03 breaker counter must be visible on
  ``health_check()`` — the endpoint a load balancer/operator polls — in BOTH
  the healthy and the unhealthy branch. rel-03 decision 11 surfaced it in
  ``get_stats`` only; between timeout #1 and the trip the breadcrumb is
  invisible on /health today.
- B5 (FU-3 item 2): breaker branch pin — ``timeout -> non-timeout-failure ->
  timeout`` must still trip on the second timeout, because "consecutive"
  counts since the last COMPLETED pipeline (rel-03 decision 1): a non-timeout
  failure deliberately leaves the counter untouched (the thread abandoned by
  timeout #1 survives an unrelated later failure still holding VRAM/hf
  locks). GREEN TODAY BY DESIGN — this is the characterization pin that
  blocks a future "reset on any exception" refactor.
- B6 (FU-3 item 5): on py3.11+ ``asyncio.TimeoutError is builtin
  TimeoutError``, so a pipeline-internal I/O timeout (garage upload, socket)
  escaping ``_generate_and_upload`` lands in ``_generate_with_lease``'s
  ``asyncio.TimeoutError`` handler and feeds the breaker — a false trip. It
  must count as an ORDINARY failure (jobs_failed) instead. On py3.10 the bare
  builtin already misses the asyncio handler, so this pin only bites on
  >=3.11 — the dev venv is 3.12, where it is genuinely red today.
- R1 (FU-3 item 3): ``_refresh_lease`` must scope its UPDATE to
  ``worker_id`` (same predicate shape as the DATA-4 terminal writes).
  After a reclaim the row's worker_id is the NEW owner; a zombie's heartbeat
  must not extend a lease it no longer owns (worst case: a reclaimed row
  becomes effectively immortal if the new owner then dies).
- C1 (FU-3 item 4): a 0-rowcount completion (DATA-4 lost-lease guard) must
  NOT be counted in ``jobs_processed`` and must be logged — counting it
  inflates the /health signal the breaker diagnosis reads. Control: a landed
  UPDATE still counts exactly as before.
- S1 (FU-3 item 6): the worker split — ``app/worker_job_rows.py`` must exist
  (the ``_JobRowLifecycle`` mixin seam) and both files must stay under the
  project's 500-LOC rule (AGENTS.md), mirroring the FU-2 orchestrator split.

Import strategy: this dev venv has no torch, so a session fixture imports
app.worker against a stubbed ``app.framework.framework_generator`` (same shape
as tests/test_worker_correctness.py's worker_module fixture; the 3.12 venv
makes B6's TimeoutError alias live).

Case map (plan rel-fu-3-plan.md §3.1):

======  ====================================================================
H1      /health carries consecutive_generation_timeouts (both branches)
B5      timeout -> fail -> timeout still trips the breaker (green pin)
B6      builtin TimeoutError from the pipeline does not feed the breaker
R1      _refresh_lease UPDATE is scoped to worker_id
C1      lost-lease completion not counted processed + logged
S1      worker split files exist and stay under 500 LOC
======  ====================================================================
"""

import asyncio
import logging
import sys
import time
import types
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Fixtures / harness (torch-less, pattern: tests/test_worker_correctness.py)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def worker_module():
    """Import app.worker with the GPU generator module stubbed out."""
    saved = sys.modules.get("app.framework.framework_generator")
    fake = types.ModuleType("app.framework.framework_generator")

    class GeneratorRegistry:  # minimal stand-in; worker logic doesn't use it here
        def __init__(self, *args, **kwargs):
            self.models = {}

        def load(self):
            pass

    fake.GeneratorRegistry = GeneratorRegistry
    sys.modules["app.framework.framework_generator"] = fake
    from app import worker  # imported after the stub is in place

    yield worker
    if saved is None:
        sys.modules.pop("app.framework.framework_generator", None)
    else:
        sys.modules["app.framework.framework_generator"] = saved


def _pool_yielding(conn) -> MagicMock:
    """An asyncpg-like pool whose `async with pool.acquire() as c:` yields conn."""
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    return pool


def _make_conn() -> MagicMock:
    """An asyncpg-like connection. fetchrow defaults to an owned-processing
    row (the lease is held) so happy-path pipelines complete; tests that pin
    the ownership guard override it."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"status": "processing", "worker_id": "vram-worker"})
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock()
    return conn


def _make_worker(worker_module, exit_hook=None):
    """GeneratorWorker with mock DB pool + garage client; exit hook injectable
    (never os._exit) — the rel-03 test pattern."""
    worker = worker_module.GeneratorWorker(
        worker_module.WorkerConfig(
            worker_id="vram-worker",
            pg_dsn="postgresql://localhost/test",
            garage=MagicMock(),
        ),
        exit_hook=exit_hook,
    )
    worker.db = _pool_yielding(_make_conn())
    worker.garage = MagicMock()
    worker.garage.put_object = AsyncMock()
    worker.garage.delete_object = AsyncMock()
    return worker


def _job(**overrides) -> dict:
    job = {
        "id": uuid.uuid4(),
        "model_id": "foundation-1",
        "prompt": "atmospheric pad",
        "key": "C minor",
        "bpm": 128,
        "bars": 4,
    }
    job.update(overrides)
    return job


def _run(coro):
    """Run a coroutine on a fresh loop (pattern used across the worker tests)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _sleeping_generate():
    """A sync fake ``generate_stem`` that outlasts the patched 0.05 s window."""
    return lambda **_kwargs: time.sleep(0.3)


def _failing_generate(message: str):
    """A sync fake ``generate_stem`` that raises an ordinary (non-timeout) error."""

    def _raise(**_kwargs):
        raise RuntimeError(message)

    return _raise


def _failure_calls(conn) -> list:
    """The conn.execute calls that mark a row failed (SQL contains 'failed')."""
    return [call for call in conn.execute.call_args_list if "failed" in call.args[0]]


# ---------------------------------------------------------------------------
# H1 — /health carries the REL-03 breaker counter (FU-3 item 1)
# ---------------------------------------------------------------------------


async def test_health_carries_breaker_counter_zero_then_after_one_timeout(worker_module, monkeypatch):
    """H1 (acceptance): ``health_check()`` must expose
    ``consecutive_generation_timeouts`` in the healthy branch (fresh worker:
    0), after one driven timeout (1), and in the unhealthy branch (DB fetch
    raised) — the wedged-ish state where the breadcrumb matters most."""
    worker = _make_worker(worker_module)

    health = await worker.health_check()
    assert health["status"] == "healthy"
    assert health["consecutive_generation_timeouts"] == 0

    monkeypatch.setattr(worker_module, "GENERATION_TIMEOUT_SECONDS", 0.05)
    worker.generators.generate_stem = _sleeping_generate()
    with pytest.raises(TimeoutError):
        await worker._generate_with_lease(_job())
    assert worker.consecutive_generation_timeouts == 1

    health = await worker.health_check()
    assert health["status"] == "healthy"
    assert health["consecutive_generation_timeouts"] == 1

    conn = worker.db.acquire.return_value.__aenter__.return_value
    conn.fetchval.side_effect = RuntimeError("pool connection closed")
    health = await worker.health_check()
    assert health["status"] == "unhealthy"
    assert health["consecutive_generation_timeouts"] == 1


# ---------------------------------------------------------------------------
# B5 — breaker branch pin: timeout -> non-timeout failure -> timeout (item 2)
# ---------------------------------------------------------------------------


async def test_timeout_fail_timeout_still_trips_breaker(worker_module, monkeypatch):
    """B5 (characterization pin — GREEN today by design): "consecutive" counts
    since the last COMPLETED pipeline, so a RuntimeError between two timeouts
    must leave the counter untouched and the second timeout must trip the
    breaker (exit hook once, with 1). All three pipelines still mark their
    rows failed with their own messages."""
    exit_calls: list[int] = []
    worker = _make_worker(worker_module, exit_hook=exit_calls.append)
    monkeypatch.setattr(worker_module, "GENERATION_TIMEOUT_SECONDS", 0.05)
    job = _job()

    worker.generators.generate_stem = _sleeping_generate()  # timeout #1
    await worker._process_claimed_job(job)
    assert worker.consecutive_generation_timeouts == 1
    assert exit_calls == []

    failed_before = worker.jobs_failed
    worker.generators.generate_stem = _failing_generate("cuda oom")  # non-timeout failure
    await worker._process_claimed_job(job)
    assert worker.jobs_failed == failed_before + 1, "the RuntimeError counts as exactly one ordinary failure"
    assert worker.consecutive_generation_timeouts == 1, "a non-timeout failure must not reset the streak"
    assert exit_calls == []

    worker.generators.generate_stem = _sleeping_generate()  # timeout #2 -> breaker
    await worker._process_claimed_job(job)
    assert worker.consecutive_generation_timeouts == 2
    assert exit_calls == [1]
    assert worker.jobs_failed == 3

    conn = worker.db.acquire.return_value.__aenter__.return_value
    failure_calls = _failure_calls(conn)
    assert len(failure_calls) == 3
    messages = [call.args[1] for call in failure_calls]
    assert sum("exceeded" in message for message in messages) == 2
    assert sum("cuda oom" in message for message in messages) == 1


# ---------------------------------------------------------------------------
# B6 — py3.11 TimeoutError alias: pipeline I/O timeouts are ordinary failures
# ---------------------------------------------------------------------------


async def test_builtin_timeout_from_pipeline_does_not_feed_breaker(worker_module, monkeypatch):
    """B6 (RED on py3.11+, where ``asyncio.TimeoutError is TimeoutError``):
    a TimeoutError raised INSIDE the pipeline (here the Garage upload) must be
    an ordinary pipeline failure — jobs_failed, breaker counter 0, no exit —
    and the row's error_message must carry the ORIGINAL cause, not the
    breaker's 'exceeded' re-wrap. On py3.10 the bare builtin already misses
    the asyncio handler, so this pin only bites on >=3.11 (dev venv: 3.12)."""
    exit_calls: list[int] = []
    worker = _make_worker(worker_module, exit_hook=exit_calls.append)
    worker.generators.generate_stem = lambda **_kwargs: (np.zeros((8, 2), dtype=np.float32), 44100)
    worker.garage.put_object = AsyncMock(side_effect=TimeoutError("upload timed out"))
    monkeypatch.setattr(worker_module, "encode_aac", lambda _audio, sample_rate=44100: b"aac")
    monkeypatch.setattr(worker_module, "get_audio_duration", lambda _audio, sample_rate=44100: 1.0)
    job = _job()

    await worker._process_claimed_job(job)

    assert worker.consecutive_generation_timeouts == 0, "pipeline I/O timeout must not feed the REL-03 breaker"
    assert exit_calls == []
    assert worker.jobs_failed == 1, "a pipeline I/O timeout is an ordinary pipeline failure"
    failure_calls = _failure_calls(worker.db.acquire.return_value.__aenter__.return_value)
    assert len(failure_calls) == 1
    message = failure_calls[0].args[1]
    assert "upload timed out" in message, "the original cause must reach error_message"
    assert "exceeded" not in message, "the breaker's timeout re-wrap must not mask the I/O cause"


# ---------------------------------------------------------------------------
# R1 — _refresh_lease is scoped to worker_id (FU-3 item 3)
# ---------------------------------------------------------------------------


def test_refresh_lease_scoped_to_worker_id(worker_module):
    """R1: the heartbeat's lease refresh must not extend a row this worker no
    longer owns. After a reclaim, worker_id is the NEW owner, so the UPDATE
    needs ``AND worker_id = $3`` — the same predicate shape as the DATA-4
    terminal writes — with params (lease_expiry, job_id, worker_id)."""
    worker = _make_worker(worker_module)
    conn = worker.db.acquire.return_value.__aenter__.return_value
    job_id = uuid.uuid4()

    _run(worker._refresh_lease(job_id))

    call_args = conn.execute.call_args[0]
    sql, params = call_args[0], call_args[1:]  # asyncpg style: execute(sql, *params)
    assert "status = 'processing'" in sql
    assert "worker_id = $3" in sql, "the refresh must not extend a reclaimed row's lease"
    assert len(params) == 3, f"expected (lease_expiry, job_id, worker_id), got {params!r}"
    lease_expiry, param_job_id, param_worker_id = params
    assert lease_expiry.tzinfo is not None, "lease timestamps must be tz-aware"
    assert param_job_id == job_id
    assert param_worker_id == "vram-worker"


# ---------------------------------------------------------------------------
# C1 — a 0-rowcount completion is not a processed job (FU-3 item 4)
# ---------------------------------------------------------------------------


async def test_lost_lease_completion_not_counted_and_logged(worker_module, caplog):
    """C1: when ``_mark_job_complete``'s ownership-guarded UPDATE lands 0 rows
    (lease lost, DATA-4 guard), the pipeline did NOT process a job —
    ``jobs_processed`` must stay put and the skip must be logged at WARNING.
    Control: a landed UPDATE (rowcount 1) still counts exactly as before."""
    worker = _make_worker(worker_module)
    worker._generate_with_lease = AsyncMock(return_value=("audio/x.aac", 1.0))
    conn = worker.db.acquire.return_value.__aenter__.return_value
    conn.execute.side_effect = ["UPDATE 0"]  # completion UPDATE guarded out

    with caplog.at_level(logging.WARNING):
        await worker._process_claimed_job(_job())

    assert worker.jobs_processed == 0, "a 0-rowcount completion is a lost lease, not a processed job"
    assert worker.jobs_failed == 0
    lost_lease_logs = [record for record in caplog.records if "not counted as processed" in record.getMessage()]
    assert lost_lease_logs, "the lost-lease skip must be logged so /health counts stay explainable"
    assert any(record.levelno == logging.WARNING for record in lost_lease_logs)

    control = _make_worker(worker_module)
    control._generate_with_lease = AsyncMock(return_value=("audio/y.aac", 1.0))
    control_conn = control.db.acquire.return_value.__aenter__.return_value
    control_conn.execute.side_effect = ["UPDATE 1", "UPDATE 1"]  # completion + NOTIFY

    await control._process_claimed_job(_job())

    assert control.jobs_processed == 1, "control: a landed completion still counts as processed"


# ---------------------------------------------------------------------------
# S1 — the worker split under the 500-LOC rule (FU-3 item 6)
# ---------------------------------------------------------------------------


def test_worker_split_files_under_500_lines(worker_module):
    """S1 (FU-3 item 6): both worker files must exist and stay under the
    project's 500-LOC rule (AGENTS.md); the ``_JobRowLifecycle`` extraction
    (mirroring FU-2's ``_LoopDelegates``) is the sanctioned split seam, and
    GeneratorWorker must actually inherit it (mixin-first MRO)."""
    for rel in ("app/worker.py", "app/worker_job_rows.py"):
        path = Path(rel)
        assert path.exists(), f"{rel} missing — the _JobRowLifecycle extraction (FU-3 item 6) has not landed"
        line_count = len(path.read_text().splitlines())
        assert line_count < 500, f"{rel} is {line_count} lines — over the project's 500-LOC rule (AGENTS.md)"
    assert issubclass(worker_module.GeneratorWorker, worker_module._JobRowLifecycle), (
        "GeneratorWorker must inherit the _JobRowLifecycle mixin (the moved job-row lifecycle)"
    )
