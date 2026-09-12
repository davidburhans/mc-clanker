"""Session plumbing for the /stream.mp3 fan-out (FU-5, rel-fu-cosmetics).

Pure move from app/stream_fanout.py: the leaf per-client session dataclass
and its lock-free queue helpers, split out so both files satisfy the
project's 500-LOC rule. Import direction is one-way (stream_fanout →
sessions); this module is stdlib-only — no locks, no GlobalState, no
subprocess — so the move is behavior-neutral, and app.stream_fanout
re-imports every moved name (patch-point inventory: zero test edits).

Identity contract (pinned by tests/test_fu5_cosmetics.py S2): the
``_STOP_SENTINEL`` poison pill is defined ONCE here and re-exported by
app.stream_fanout, so feeder, eviction, teardown and the client generator
all compare against the same object.
"""

from __future__ import annotations

import queue
from collections.abc import Iterator
from dataclasses import dataclass

#: Wake-up marker for per-client queues. ``None`` is the trigger_shutdown
#: poison (framework_state); the sentinel is ours (eviction/teardown). Both
#: terminate a client generator; the feeder treats both as stop (decision 9).
_STOP_SENTINEL = object()


@dataclass(eq=False)
class _ClientSession:
    """One connected client: bounded queue + fullness clock (identity type:
    registry removal is by identity). The pump thread solely writes it."""

    client_id: int
    queue: queue.Queue
    full_since: float | None = None


def _drain_one(client_queue: queue.Queue) -> None:
    """Discard the oldest buffered item (drop-oldest overflow policy)."""
    try:
        client_queue.get_nowait()
    except queue.Empty:
        pass


def _drain_queue(client_queue: queue.Queue) -> None:
    """Empty a bounded queue without blocking (eviction/teardown helper)."""
    while True:
        try:
            client_queue.get_nowait()
        except queue.Empty:
            return


def _residual_blocks(client_queue: queue.Queue) -> Iterator[bytes]:
    """Yield data blocks queued ahead of a stop marker.

    Teardown never discards already-encoded bytes, and the route stays
    deterministic whichever lands last, a data block or the sentinel.
    """
    while True:
        try:
            block = client_queue.get_nowait()
        except queue.Empty:
            return
        if block is None or block is _STOP_SENTINEL:
            return
        yield block
