"""FU-4 TDD-red suite (rel-fu-4-exports): off-loop stats/timeline, timeline
index, reasoning split LOC pin, export_chunks annotation pin, fanout
acquire-rollback orphan pin.

Encodes the acceptance suite from refactor/plans/units/rel-fu-4-plan.md §3.1.
Red-reason map (plan §3.4 step 1):

- O1/O2: the engine-level slow-fake sleeps before EVERY statement; beside a
  live heartbeat the routes must keep the loop gap under budget while the
  sleep provably ran. Red today: auth + aggregates + the per-chunk scans run
  inline and starve the event loop ≥ 0.1 s per statement (the rel-11 residual
  from the rel-13 review).
- I1: ``ix_llm_interactions_show_rel_time`` is absent from the create_all
  schema today (rel-13 §6 residual: the timeline detail scan keysets on
  (relative_time_ms, id) under a show_id filter with no covering index).
- S1: app/lib/reasoning_stats.py does not exist and reasoning_logs.py sits at
  497/500 lines (the split, FU-2/FU-3 pattern).
- H1: chunked_shaped_rows/ndjson_lines carry zero annotations today.
- R1: acquire_client's rollback only discards the reservation — a spawn that
  fails AFTER supervisor.spawn() strands a live transcoder nobody references
  (the rel-10 review P2/P3 residual).

Off-loop mechanics mirror tests/test_db_offloop.py (rel-09 precedent): local
mirror of its heartbeat measurer per the repo's fixture-glue pattern, with 2x
headroom budgets tuned at the top of this file only.
"""

import asyncio
import inspect
import time
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlalchemy import inspect as sqla_inspect
from test_stream_fanout import FakeProc, PopenRecorder, fanout_threads_alive, make_cfg, wait_until

from app.framework.framework_state import state
from app.routes.reasoning_logs import get_reasoning_stats, get_reasoning_timeline
from app.stream_fanout import FanoutInactive, get_stream_fanout

# The fake DB sleeps SLOW_STATEMENT_SECONDS before every statement; the event
# loop must keep beating well under RESPONSIVE_BUDGET_SECONDS. 2x headroom
# (0.1 vs 0.05, the rel-09 tuning note) — tune here, in one place, only on a
# pathologically slow runner.
SLOW_STATEMENT_SECONDS = 0.1
RESPONSIVE_BUDGET_SECONDS = 0.05
HEARTBEAT_INTERVAL_SECONDS = 0.01

_EXPORT_PAGE_SHRINK = 2  # O2: 5 rows over pages of 2 → 3 detail chunk scans


# --------------------------------------------------------------------------- #
# Shared helpers (fixture-glue mirrors of the rel-09/rel-13 suites)
# --------------------------------------------------------------------------- #


def _bearer_request(authorization: str) -> SimpleNamespace:
    """Fake request: get_current_user_from_request reads only .headers.get."""
    return SimpleNamespace(headers={"Authorization": authorization})


def _shrink_export_page(monkeypatch, size: int = _EXPORT_PAGE_SHRINK) -> None:
    """Mirror of test_exports_pagination._shrink_export_page (fixture glue)."""
    from app.lib import export_chunks

    monkeypatch.setattr(export_chunks, "EXPORT_CHUNK_ROWS", size)


class _SlowStatements:
    """Engine listener sleeping before EVERY statement (thread-agnostic slow-fake).

    Works on real sessions regardless of query shape; after FU-4 the sleep runs
    on the to_thread worker thread, where it can no longer stall the loop.
    """

    def __init__(self, engine, seconds: float) -> None:
        self._engine = engine
        self._seconds = seconds

    def __enter__(self) -> "_SlowStatements":
        event.listen(self._engine, "before_cursor_execute", self._pause)
        return self

    def __exit__(self, *exc_info) -> bool:
        event.remove(self._engine, "before_cursor_execute", self._pause)
        return False

    def _pause(self, conn, cursor, statement, parameters, context, executemany) -> None:
        time.sleep(self._seconds)


async def _heartbeat(loop: asyncio.AbstractEventLoop, beats: list[float]) -> None:
    """Append loop timestamps every HEARTBEAT_INTERVAL_SECONDS until cancelled."""
    while True:
        beats.append(loop.time())
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


async def _measure_with_heartbeat(target):
    """Mirror of test_db_offloop.py:_measure_with_heartbeat (local per the
    repo's fixture-glue pattern): run target() beside a heartbeat; return
    (outcome, elapsed, max_gap). A max_gap >= SLOW_STATEMENT_SECONDS means the
    fake DB sleep executed ON the event loop — exactly the rel-11 residual."""
    loop = asyncio.get_running_loop()
    beats: list[float] = []
    beater = asyncio.create_task(_heartbeat(loop, beats))
    await asyncio.sleep(2 * HEARTBEAT_INTERVAL_SECONDS)
    started = loop.time()
    outcome: tuple[str, object]
    try:
        outcome = ("ok", await target())
    except Exception as exc:  # noqa: BLE001 — measuring failures is the point
        outcome = ("raised", exc)
    finally:
        # Yield before cancelling so the beater can stamp the post-block beat
        # (test_db_offloop precedent: cancels-first would hide the starvation).
        await asyncio.sleep(2 * HEARTBEAT_INTERVAL_SECONDS)
        beater.cancel()
        with suppress(asyncio.CancelledError):
            await beater
    elapsed = loop.time() - started
    gaps = [later - earlier for earlier, later in zip(beats, beats[1:])]
    return outcome, elapsed, max(gaps, default=0.0)


async def _assert_loop_responsive(target):
    """Run target and assert the loop never starved while the slow statements ran."""
    outcome, elapsed, max_gap = await _measure_with_heartbeat(target)
    assert max_gap < RESPONSIVE_BUDGET_SECONDS, (
        f"event loop starved {max_gap:.3f}s >= budget {RESPONSIVE_BUDGET_SECONDS}s: "
        "stats/timeline DB work ran on the loop"
    )
    assert elapsed >= SLOW_STATEMENT_SECONDS, f"slow-fake sleep never ran (elapsed {elapsed:.3f}s)"
    return outcome


# --------------------------------------------------------------------------- #
# O1/O2 — stats/timeline do ZERO DB work on the event loop (FU-4 item 1)
# --------------------------------------------------------------------------- #


async def test_stats_route_does_no_db_work_on_event_loop(isolated_export_db):
    """O1 (FU-4 item 1): get_reasoning_stats — auth INCLUDED — runs every DB
    statement off the event loop while the payload stays byte-identical.

    Red today: the Bearer user fetch, the show fetch, the core-totals aggregate,
    the action-count/keys aggregates and the instruments chunk scan all run
    inline, each stalling the loop ≥ SLOW_STATEMENT_SECONDS.
    """
    sandbox = isolated_export_db
    show_id = sandbox.make_show("fu4 stats off-loop")
    sandbox.insert_interactions(
        show_id, 1, loop_index=0, action_type="retain", bpm=128.0, key="C minor",
        instruments=["Bass", "Drums"], reasoning="abcdefgh", was_fallback=False,
    )
    sandbox.insert_interactions(
        show_id, 1, loop_index=1, action_type="add", bpm=130.0, key="C",
        instruments=["Drums"], reasoning="", was_fallback=True,
    )
    sandbox.insert_interactions(
        show_id, 1, loop_index=2, action_type="remove", bpm=None, key=None,
        instruments=None, reasoning=None, was_fallback=False,
    )
    request = _bearer_request(sandbox.headers["Authorization"])

    with _SlowStatements(sandbox.db.engine, SLOW_STATEMENT_SECONDS):
        outcome = await _assert_loop_responsive(lambda: get_reasoning_stats(request=request, show_id=show_id))

    assert outcome[0] == "ok"
    assert outcome[1] == {
        "total_interactions": 3,
        "action_counts": {"retain": 1, "add": 1, "remove": 1},
        "avg_bpm": 129.0,
        "bpm_range": {"min": 128.0, "max": 130.0},
        "keys_used": ["C", "C minor"],
        "instruments_used": ["Bass", "Drums"],
        "fallback_count": 1,
        "fallback_rate": 0.333,
        "avg_reasoning_length": 8.0,
    }


async def test_timeline_route_does_no_db_work_on_event_loop(isolated_export_db, monkeypatch):
    """O2 (FU-4 item 1): get_reasoning_timeline's per-chunk detail scan never
    touches the loop — the exact rel-11 hazard (~150 serial SELECTs for a
    75 k-row show). The page is shrunk to 2 so 5 rows produce count +
    aggregates + 3 detail chunks ≈ 7 slow statements. Red today: every inline
    statement starves the loop."""
    _shrink_export_page(monkeypatch, 2)
    sandbox = isolated_export_db
    show_id = sandbox.make_show("fu4 timeline off-loop")
    for row in (
        dict(loop_index=0, relative_time_ms=0, action_type="retain", bpm=128.0, key="C minor",
             instruments=["Bass"], reasoning="one"),
        dict(loop_index=1, relative_time_ms=10_000, action_type="add", bpm=130.0, key="C minor",
             instruments=["Drums"], reasoning="two"),
        # Post-reset edge: relative_time_ms keeps growing while loop_index restarts.
        dict(loop_index=0, relative_time_ms=35_000, action_type="remove", bpm=126.0, key="C",
             instruments=["Bass"], reasoning="three"),
        dict(loop_index=2, relative_time_ms=61_000, action_type="retain", bpm=124.0, key="C",
             instruments=["Synth"], reasoning="four"),
        dict(loop_index=3, relative_time_ms=62_000, action_type="add", bpm=132.0, key="C minor",
             instruments=["Synth", "Drums"], reasoning="five"),
    ):
        sandbox.insert_interactions(show_id, 1, **row)
    request = _bearer_request(sandbox.headers["Authorization"])

    with _SlowStatements(sandbox.db.engine, SLOW_STATEMENT_SECONDS):
        outcome = await _assert_loop_responsive(
            lambda: get_reasoning_timeline(request=request, show_id=show_id, segment_seconds=30)
        )

    assert outcome[0] == "ok"
    payload = outcome[1]
    assert payload["total_interactions"] == 5
    assert payload["segment_seconds"] == 30
    assert payload["total_segments"] == 3
    assert [seg["seg_index"] for seg in payload["segments"]] == [0, 1, 2]
    assert [seg["interaction_count"] for seg in payload["segments"]] == [2, 1, 2]
    expected_seg_keys = {
        "seg_index", "start_ms", "end_ms", "start_time_formatted", "action_counts", "avg_bpm",
        "instruments_used", "key_changes", "reasoning_snippets", "interaction_ids", "interaction_count",
    }
    for seg in payload["segments"]:
        assert set(seg) == expected_seg_keys, "timeline segment shape must not drift"


# --------------------------------------------------------------------------- #
# I1 — the (show_id, relative_time_ms) timeline index (FU-4 item 2)
# --------------------------------------------------------------------------- #


def test_llm_interactions_show_rel_time_index_exists(isolated_export_db):
    """I1 (FU-4 item 2, rel-13 §6 residual): the model-created schema carries
    ``ix_llm_interactions_show_rel_time`` covering the timeline detail scan's
    (show_id → relative_time_ms) filter+order; the sibling show_loop index is
    untouched. (PG deployments get it via migrations/005 — I1 pins the
    create_all path every test schema and fresh install uses.)"""
    sandbox = isolated_export_db
    indexes = {idx["name"]: idx for idx in sqla_inspect(sandbox.db.engine).get_indexes("llm_interactions")}
    new_index = indexes.get("ix_llm_interactions_show_rel_time")
    assert new_index is not None, (
        "ix_llm_interactions_show_rel_time missing — FU-4 item 2 (rel-13 §6 residual) has not landed; "
        f"got {sorted(indexes)}"
    )
    assert list(new_index["column_names"]) == ["show_id", "relative_time_ms"]
    assert "ix_llm_interactions_show_loop" in indexes, "sibling index must survive the change"


# --------------------------------------------------------------------------- #
# S1 — the reasoning split under the 500-LOC rule (FU-4 item 3)
# --------------------------------------------------------------------------- #


def test_reasoning_logs_split_under_500_lines():
    """S1 (FU-4 item 3): the timeline/stats compute is split out of
    reasoning_logs.py (497/500) into app/lib/reasoning_stats.py; both files
    stay under the project's 500-LOC rule (AGENTS.md) and the pure compute
    entry points import (FU-2/FU-3 S1 pattern)."""
    for rel in ("app/routes/reasoning_logs.py", "app/lib/reasoning_stats.py"):
        path = Path(rel)
        assert path.exists(), f"{rel} missing — the reasoning_stats split (FU-4 item 3) has not landed"
        line_count = len(path.read_text().splitlines())
        assert line_count < 500, f"{rel} is {line_count} lines — over the project's 500-LOC rule (AGENTS.md)"
    from app.lib.reasoning_stats import compute_stats_payload, compute_timeline_payload

    assert callable(compute_stats_payload) and callable(compute_timeline_payload)


# --------------------------------------------------------------------------- #
# H1 — export_chunks seam fully annotated (FU-4 item 4)
# --------------------------------------------------------------------------- #


def test_export_chunks_params_fully_annotated():
    """H1 (FU-4 item 4): both export_chunks seam functions carry a full
    annotation surface — every parameter AND the return.

    Deliberately NO ``inspect.signature(..., eval_str=True)`` resolution here:
    the PEP-563 string annotations reference TYPE_CHECKING-only names
    (DatabaseManager/Session/Query/Column) that are intentionally absent at
    runtime — the pin is PRESENCE; type-correctness is the reviewer's check.
    """
    from app.lib import export_chunks

    for func in (export_chunks.chunked_shaped_rows, export_chunks.ndjson_lines):
        signature = inspect.signature(func)
        for name, param in signature.parameters.items():
            assert param.annotation is not inspect.Parameter.empty, (
                f"export_chunks.{func.__name__} param '{name}' is unannotated (FU-4 item 4)"
            )
        assert signature.return_annotation is not inspect.Parameter.empty, (
            f"export_chunks.{func.__name__} return is unannotated (FU-4 item 4)"
        )


# --------------------------------------------------------------------------- #
# R1 — acquire rollback after spawn kills the orphaned transcoder (FU-4 item 5)
# --------------------------------------------------------------------------- #


@pytest.fixture
def fake_popen(monkeypatch):
    """Patch Popen inside the fanout module; returns the created-proc recorder
    (the test_stream_fanout original is module-local — fixture-glue mirror)."""
    recorder = PopenRecorder()

    def _popen(argv, **kwargs):
        proc = FakeProc(argv, stderr_text=recorder.stderr_text, **kwargs)
        recorder.created.append(proc)
        return proc

    monkeypatch.setattr("app.stream_fanout.subprocess.Popen", _popen)
    return recorder


@pytest.fixture
def fake_ffmpeg_exe(monkeypatch):
    """Hermetic ffmpeg: fixed resolved name, libmp3lame probe answers success."""
    monkeypatch.setattr("app.stream_fanout_args.resolve_ffmpeg_exe", lambda: "ffmpeg")
    probe = SimpleNamespace(stdout="... libmp3lame ...", returncode=0)
    monkeypatch.setattr("app.stream_fanout.subprocess.run", lambda *args, **kwargs: probe)
    return "ffmpeg"


@pytest.fixture(autouse=True)
def _isolate_fanout():
    """Isolate stream state; stop any fanout left running by a test (the
    test_stream_fanout reset_fanout_state body — fixture-glue mirror)."""

    def _isolate() -> None:
        fanout = getattr(state, "stream_fanout", None)
        if fanout is not None:
            fanout._teardown()
        state.stream_fanout = None
        state.audio_clients = []
        state.is_running = True
        state.shutdown_event.clear()
        state.active_subprocesses.clear()
        state.dj_password = ""
        state.audience_password = ""

    _isolate()
    yield
    _isolate()


def test_acquire_rollback_kills_orphaned_transcoder(fake_popen, fake_ffmpeg_exe, monkeypatch):
    """R1 (FU-4 item 5, rel-10 review P2/P3 residual): an acquire that fails
    AFTER supervisor.spawn() (here: thread exhaustion at _start_threads) must
    funnel through _teardown — the spawned transcoder is reaped, the kill list
    empties, the PCM queue unregisters, the singleton retires, and the dead
    object never resurrects. Red today: the rollback only discards the
    reservation, stranding a live ffmpeg + registered queue on a singleton
    that acquire_stream_client then merely _retire()s."""
    def _raise_thread_exhaustion() -> None:
        raise RuntimeError("can't start new thread")

    fanout = get_stream_fanout(state, cfg=make_cfg())
    monkeypatch.setattr(fanout, "_start_threads", _raise_thread_exhaustion)

    with pytest.raises(RuntimeError, match="can't start new thread"):
        fanout.acquire_client()

    proc = fake_popen[0]  # spawn SUCCEEDED — the failure came after it
    assert wait_until(lambda: proc.poll() is not None, timeout=5.0), (
        "orphaned transcoder outlived the failed acquire"
    )
    assert proc.stdin.closed, "teardown left the orphaned transcoder stdin open"
    assert state.active_subprocesses == set(), "shutdown kill list still holds the orphaned transcoder"
    assert state.audio_clients == [], "PCM queue left registered after the failed acquire"
    assert state.stream_fanout is None, "the failed singleton was left on state"
    assert not fanout.status().active
    assert wait_until(lambda: fanout_threads_alive() == 0, timeout=5.0), "fanout threads outlived the rollback"
    with pytest.raises(FanoutInactive):
        fanout.acquire_client()  # retired single-use object: the factory retries fresh, never resurrects
