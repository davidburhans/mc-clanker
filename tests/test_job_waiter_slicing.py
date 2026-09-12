"""REL-17 (U12) regression tests — JobWaiter must not hold a dead LISTEN conn for the full wait.

Finding (docs/reliability_audit.md REL-17): ``JobWaiter.wait_for_job_completion``
awaits ``asyncio.wait_for(event.wait(), timeout)`` ONCE with the full job
timeout (600 s in the loop path). If the LISTEN connection dies mid-wait (PG
restart, network partition, pool eviction) the death is undetected until the
full timeout elapses — and the corpse stays checked out of the asyncpg pool
(``max_size=10``) the whole time, starving every other waiter.

Contract pinned here (per refactor/plans/units/rel-17-plan.md §3.1):

* the wait is sliced (``WAITER_SLICE_SECONDS``) and ``conn.is_closed()`` is
  checked between slices — a dead conn ends the wait within one slice (W1);
* a notify landing mid-slice or across slice boundaries still resolves the
  job — slicing cannot delay or swallow a notify (W2, W7);
* timeout semantics are byte-compatible: total wait ≤ timeout, and the final
  status fetch on a fresh pool conn decides the outcome (W3);
* the missed-notify race coverage (pre-check + post-subscribe re-check,
  review A7/C6) is untouched (W4, W5);
* a conn that is already dead at subscribe time skips the wait entirely and
  still releases (W6); the default slice is 5.0 s (W8).

TDD RED expectations at HEAD (no slicing): W1/W2/W6 fail on the behavioral
assertions (full-timeout wait / zero slice checks); W8 fails on the missing
module constant. W3-W5/W7 pin preserved seams and stay green.

Fakes mirror the installed asyncpg 0.31.0 contracts ``JobWaiter`` relies on
(verified from asyncpg source): ``remove_listener`` no-ops on a closed
connection, ``pool.acquire()`` is both awaitable (LISTEN conn) and an async
context manager (status conn), and ``pool.release()`` owns corpse handling.
"""

import asyncio
import time
import uuid
from typing import Any

import app.job_waiter as job_waiter_module
from app.job_waiter import JobWaiter

# --------------------------------------------------------------------------- #
# Named fakes — asyncpg 0.31.0 contracts, no real DB / network
# --------------------------------------------------------------------------- #


class _FakeListenerConn:
    """The bare-``await`` acquired LISTEN connection.

    ``is_closed()`` is the only death signal the sliced wait may poll; it
    flips at ``close_after_s`` after ``add_listener`` (or is born closed).
    ``remove_listener`` mirrors asyncpg 0.31.0: a no-op once closed. NOTE:
    a real asyncpg ``add_listener`` on a corpse would raise; the fake records
    instead, because the contract under test is the wait loop between
    subscribe and notify — a conn that died inside that window must end the
    wait without raising.
    """

    def __init__(self, *, born_closed: bool = False, close_after_s: float | None = None):
        self.born_closed = born_closed
        self.close_after_s = close_after_s
        self._listen_started: float | None = None
        self.listeners: list[tuple[str, Any]] = []
        self.removed: list[tuple[str, Any]] = []
        self.is_closed_calls = 0

    async def add_listener(self, channel: str, callback: Any) -> None:
        self._listen_started = time.monotonic()
        self.listeners.append((channel, callback))

    async def remove_listener(self, channel: str, callback: Any) -> None:
        if self.is_closed():
            return  # asyncpg 0.31.0: no-op on a closed connection (verified from source)
        self.removed.append((channel, callback))

    def is_closed(self) -> bool:
        self.is_closed_calls += 1
        if self.born_closed:
            return True
        if self.close_after_s is None or self._listen_started is None:
            return False
        return (time.monotonic() - self._listen_started) >= self.close_after_s

    def fire_notify(self, payload: str) -> None:
        """Simulate a PG NOTIFY: invoke every stored listener callback.

        asyncpg schedules coroutine-function listeners as loop tasks; mirror
        that so JobWaiter's own ``async def on_notify`` actually sets the
        event (a raw call would strand the coroutine and never wake)."""
        for _channel, callback in self.listeners:
            result = callback(self, 0, "job_completed", payload)
            if asyncio.iscoroutine(result):
                asyncio.get_running_loop().create_task(result)


class _FakeStatusConn:
    """The ``async with`` acquired status connection; fetchrow rows are scripted."""

    def __init__(self, scripted_rows: list[dict[str, Any] | None]):
        self._scripted_rows = list(scripted_rows)
        self.queries: list[str] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.queries.append(query)
        if not self._scripted_rows:
            raise AssertionError(f"unexpected fetchrow #{len(self.queries)}: {query}")
        row = self._scripted_rows.pop(0)
        return dict(row) if row is not None else None


class _AcquireRouter:
    """Routes the two acquire shapes inside JobWaiter to their connections.

    asyncpg's ``pool.acquire()`` result is BOTH awaitable (``await pool.acquire()``
    — the LISTEN conn) and an async context manager (``async with pool.acquire()``
    — the status conn in ``_get_job``); the fake pool returns this router so each
    shape lands on its own scripted connection.
    """

    def __init__(self, pool: "_FakeJobWaiterPool"):
        self._pool = pool

    def __await__(self):
        return self._acquire_listener().__await__()

    async def _acquire_listener(self):
        self._pool.listener_acquires += 1
        return self._pool.listener_conn

    async def __aenter__(self):
        self._pool.status_acquires += 1
        return self._pool.status_conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeJobWaiterPool:
    """Pool fake: records ``release()`` (a corpse conn must never be leaked)."""

    def __init__(self, listener_conn: _FakeListenerConn, status_conn: _FakeStatusConn):
        self.listener_conn = listener_conn
        self.status_conn = status_conn
        self.listener_acquires = 0
        self.status_acquires = 0
        self.released: list[Any] = []

    def acquire(self) -> _AcquireRouter:
        return _AcquireRouter(self)

    async def release(self, conn: Any) -> None:
        self.released.append(conn)


def _pending() -> dict[str, Any]:
    return {"status": "pending"}


def _completed(path: str) -> dict[str, Any]:
    return {"status": "completed", "audio_path": path}


# --------------------------------------------------------------------------- #
# W1-W8
# --------------------------------------------------------------------------- #


async def test_waiter_returns_promptly_when_listener_conn_dies(monkeypatch):
    """W1 (REL-17 acceptance): a LISTEN conn dying mid-wait ends the wait within
    one slice; the caller's final status fetch resolves the outcome."""
    monkeypatch.setattr(job_waiter_module, "WAITER_SLICE_SECONDS", 0.05, raising=False)
    listener = _FakeListenerConn(close_after_s=0.12)
    status = _FakeStatusConn([_pending(), _pending(), _pending()])
    pool = _FakeJobWaiterPool(listener, status)
    waiter = JobWaiter(pool)

    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await asyncio.wait_for(waiter.wait_for_job_completion(uuid.uuid4(), timeout=3.0), timeout=10.0)
    elapsed = loop.time() - started

    assert result is None
    assert elapsed < 1.0, f"dead conn was not detected within one slice (waited {elapsed:.2f}s; unsliced = 3.0s)"
    assert elapsed >= 0.12, "returned before the fake conn actually died"
    assert len(status.queries) == 3, "final status fetch on a fresh conn must still decide the outcome"
    assert pool.released == [listener], "the dead conn must still be released exactly once"


async def test_waiter_resolves_on_notify_across_slice_boundaries(monkeypatch):
    """W2 (REL-17 acceptance): a notify landing after ≥2 slice expiries still
    resolves the job — slicing must neither swallow nor delay notifies."""
    monkeypatch.setattr(job_waiter_module, "WAITER_SLICE_SECONDS", 0.01, raising=False)
    job_id = uuid.uuid4()
    listener = _FakeListenerConn()
    status = _FakeStatusConn([_pending(), _pending(), _completed("audio/notify.aac")])
    pool = _FakeJobWaiterPool(listener, status)
    waiter = JobWaiter(pool)

    loop = asyncio.get_running_loop()
    loop.call_later(0.03, lambda: listener.fire_notify(str(job_id)))

    result = await asyncio.wait_for(waiter.wait_for_job_completion(job_id, timeout=1.0), timeout=10.0)

    assert result == "audio/notify.aac"
    assert listener.is_closed_calls >= 2, "the wait must actually be sliced (is_closed checked per slice)"


async def test_waiter_slices_honor_full_timeout_and_final_status_check(monkeypatch):
    """W3: with no notify, the sliced wait still consumes the FULL timeout and
    the final status fetch still runs (timeout semantics byte-compatible)."""
    monkeypatch.setattr(job_waiter_module, "WAITER_SLICE_SECONDS", 0.02, raising=False)
    listener = _FakeListenerConn()
    status = _FakeStatusConn([_pending(), _pending(), _pending()])
    pool = _FakeJobWaiterPool(listener, status)
    waiter = JobWaiter(pool)

    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await asyncio.wait_for(waiter.wait_for_job_completion(uuid.uuid4(), timeout=0.1), timeout=10.0)
    elapsed = loop.time() - started

    assert result is None
    assert 0.09 <= elapsed <= 0.6, f"slices must honor the full timeout, got {elapsed:.3f}s"
    assert len(status.queries) == 3, "pre-check + post-subscribe re-check + final fetch = exactly 3"
    assert pool.released == [listener]


async def test_waiter_post_subscribe_recheck_catches_missed_notify(monkeypatch):
    """W4: the post-subscribe re-check (review A7/C6) resolves before any slice
    elapses, with the listener added before and removed after it."""
    monkeypatch.setattr(job_waiter_module, "WAITER_SLICE_SECONDS", 0.05, raising=False)
    listener = _FakeListenerConn()
    status = _FakeStatusConn([_pending(), _completed("audio/race.aac")])
    pool = _FakeJobWaiterPool(listener, status)
    waiter = JobWaiter(pool)

    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await asyncio.wait_for(waiter.wait_for_job_completion(uuid.uuid4(), timeout=5.0), timeout=10.0)
    elapsed = loop.time() - started

    assert result == "audio/race.aac"
    assert elapsed < 0.05, f"the re-check must win the missed-notify race before any slice ({elapsed:.3f}s)"
    assert pool.listener_acquires == 1
    assert len(listener.listeners) == 1 and listener.listeners[0][0] == "job_completed"
    assert listener.removed == [("job_completed", listener.listeners[0][1])], "listener must be removed in finally"
    assert pool.released == [listener]


async def test_waiter_pre_check_failed_job_short_circuits():
    """W5: the pre-listen status check short-circuits a failed job — no LISTEN
    conn is ever acquired."""
    listener = _FakeListenerConn()
    status = _FakeStatusConn([{"status": "failed"}])
    pool = _FakeJobWaiterPool(listener, status)
    waiter = JobWaiter(pool)

    result = await asyncio.wait_for(waiter.wait_for_job_completion(uuid.uuid4(), timeout=5.0), timeout=10.0)

    assert result is None
    assert pool.listener_acquires == 0, "a failed job must never acquire a LISTEN conn"
    assert pool.released == []


async def test_waiter_closed_conn_at_subscribe_does_not_wait(monkeypatch):
    """W6 (decision 2): a conn already dead at subscribe time skips the wait
    entirely; remove_listener is tolerated (asyncpg no-ops when closed) and the
    conn is still released."""
    monkeypatch.setattr(job_waiter_module, "WAITER_SLICE_SECONDS", 5.0, raising=False)
    listener = _FakeListenerConn(born_closed=True)
    status = _FakeStatusConn([_pending(), _pending(), _pending()])
    pool = _FakeJobWaiterPool(listener, status)
    waiter = JobWaiter(pool)

    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await asyncio.wait_for(waiter.wait_for_job_completion(uuid.uuid4(), timeout=2.0), timeout=10.0)
    elapsed = loop.time() - started

    assert result is None
    assert elapsed < 0.5, f"a corpse conn must skip the wait entirely (took {elapsed:.2f}s; unsliced = 2.0s)"
    assert len(status.queries) == 3, "the final status fetch on a fresh conn still decides the outcome"
    assert listener.removed == [], "remove_listener no-ops on the closed conn (asyncpg 0.31 contract)"
    assert pool.released == [listener], "the corpse conn must still be released exactly once"


async def test_waiter_immediate_notify_not_delayed_by_slicing(monkeypatch):
    """W7: a notify firing before the FIRST slice expiry resolves immediately —
    the common case must not wait for a slice boundary."""
    slice_seconds = 0.05
    monkeypatch.setattr(job_waiter_module, "WAITER_SLICE_SECONDS", slice_seconds, raising=False)
    job_id = uuid.uuid4()
    listener = _FakeListenerConn()
    status = _FakeStatusConn([_pending(), _pending(), _completed("audio/fast.aac")])
    pool = _FakeJobWaiterPool(listener, status)
    waiter = JobWaiter(pool)

    loop = asyncio.get_running_loop()
    loop.call_later(0.005, lambda: listener.fire_notify(str(job_id)))

    started = loop.time()
    result = await asyncio.wait_for(waiter.wait_for_job_completion(job_id, timeout=5.0), timeout=10.0)
    elapsed = loop.time() - started

    assert result == "audio/fast.aac"
    assert elapsed < slice_seconds, f"a notify already in flight must not wait for a slice ({elapsed:.3f}s)"


def test_waiter_slice_seconds_default_is_five():
    """W8: the spec constant — slices of ~5 s per the REL-17 audit remedy."""
    assert job_waiter_module.WAITER_SLICE_SECONDS == 5.0
