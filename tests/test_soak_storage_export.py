"""rel-soak P7-P8 — the audit's storage + export 24/7 gate (U15).

Audit §Soak-test spec mapping (docs/reliability_audit.md, points 7-8):

7. **Storage reconciliation** — randomized DB outages overlapping uploads +
   ``delete_show`` calls: zero unreferenced Garage objects, zero orphaned
   recording/export files, on-disk bytes bounded by retention config. Two
   storages, two paths (plan decision 9): Garage objects GC through the
   ``JobExpirationCleanup`` passes over an asyncpg-fake connection whose
   ``fetch``/``execute`` raise inside seeded outage windows (``_run_pass``
   isolates a failing pass — a window can never wedge the cycle); show
   recordings/exports are REAL files deleted by the REAL route + retention
   sweep, with the route-level outage simulated by a raising
   ``DatabaseManager`` (a half-delete must never survive an outage).
   Reconciliation is asserted AFTER recovery: eventual consistency.
8. **Real-session export** — no DB mocks: the REAL ``append_loop_audit`` +
   REAL ``AuditAdapter.flush`` against the ``isolated_export_db`` SQLite
   engine; delete-live-show drops ITS buffered rows loudly and the NEXT flush
   succeeds (rel-14 amended contract); both chunked export endpoints return
   complete NDJSON (consumers detect truncation by row count — rel-13).

Opt-in (default SKIPPED in normal runs); see docs/soak_harness.md:

    SOAK=1 .venv/bin/python -m pytest -m soak tests/test_soak_storage_export.py -q
"""

import asyncio
import json
import os
import random
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from soak_helpers import SoakParams, make_isolated_state_fixture, soak_gate, soak_params
from test_storage_retention import RoutingFakeConnection

from app.app_ui import app
from app.cleanup import CleanupConfig, JobExpirationCleanup
from app.framework.audit_recording import AuditAdapter, append_loop_audit
from app.framework.framework_state import state
from app.routes import shows as shows_routes

pytestmark = soak_gate()
_isolated_soak_state = make_isolated_state_fixture()

_RETENTION_DAYS = 7
_OUTAGE_TAG = "pg restarting (soak window)"


@pytest.fixture
def soak_app_client():
    """TestClient that reports route exceptions as 5xx instead of raising
    (P7 needs the outage leg to return 500, not blow up the harness)."""
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# P7 — storage reconciliation under randomized DB outages
# ---------------------------------------------------------------------------


class _OutageRoutingConnection(RoutingFakeConnection):
    """RoutingFakeConnection whose fetch/execute raise inside seeded windows."""

    def __init__(self, fetch_by_table, windows):
        super().__init__(fetch_by_table=fetch_by_table)
        self.windows = windows

    def _in_outage(self) -> bool:
        now = time.monotonic()
        return any(start <= now < start + duration for start, duration in self.windows)

    async def fetch(self, sql, *args):
        if self._in_outage():
            raise RuntimeError(_OUTAGE_TAG)
        flat = " ".join(sql.split())
        if "SELECT audio_path" in flat:  # the expired-jobs pass only
            return self.fetch_by_table["expired_jobs"]
        if "UPDATE generator_jobs" in flat:  # the two reaper passes: nothing stale
            return []
        return await super().fetch(sql, *args)

    async def execute(self, sql, *args):
        if self._in_outage():
            raise RuntimeError(_OUTAGE_TAG)
        return await super().execute(sql, *args)


class _ExplodingDbManager:
    """The route-level 'outage': DatabaseManager.get_instance() raises once."""

    @staticmethod
    def get_instance():
        raise RuntimeError(_OUTAGE_TAG)


async def _drive_cycle(cleanup: JobExpirationCleanup) -> int:
    """One cleanup cycle with the start()-loop's cycle-level isolation.

    ``_run_cleanup`` deliberately does NOT wrap its first pass (the stale-
    processing reap is the cycle's primary duty); in production ``start()``
    logs and retries a cycle that died. Mirror that: an outage hit reclaims
    nothing THIS cycle — the retry reconciles."""
    try:
        return await cleanup._run_cleanup()
    except RuntimeError as exc:
        if _OUTAGE_TAG not in str(exc):
            raise
        return 0


def _cleanup(conn, tmp_path, garage, **retention) -> JobExpirationCleanup:
    """A JobExpirationCleanup over the outage conn, rooted at tmp dirs."""
    config = CleanupConfig(
        pg_dsn="postgresql://u:p@localhost/soak",
        garage=MagicMock(),
        shows_dir=str(tmp_path / "shows"),
        export_dir=str(tmp_path / "exports"),
        audit_archive_dir=str(tmp_path / "audit_archive"),
        session_stale_hours=0,  # out of scope: zero session-reaper SQL
        **retention,
    )
    cleanup = JobExpirationCleanup(config)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    cleanup.db = pool
    cleanup.garage = garage
    return cleanup


def test_p7_storage_reconciliation_under_db_outages(tmp_path, monkeypatch, isolated_export_db, soak_app_client):
    """Audit point 7: outages overlapping cleanup passes + delete_show — after
    recovery: zero unreferenced Garage objects, zero orphaned files, on-disk
    bytes exactly the retention-bounded live set, DB holding exactly the
    surviving shows."""
    params: SoakParams = soak_params()
    sandbox = isolated_export_db
    monkeypatch.setenv("SHOWS_DIR", str(tmp_path / "shows"))

    # --- seed S real shows: 3 past the 7-day retention, 3 inside; two die mid-test
    show_ids = [sandbox.make_show(f"soak {i}") for i in range(params.p7_shows)]
    with sandbox.db.session() as session:
        from app.models import Show

        for show_id in show_ids:
            session.get(Show, show_id).status = "ended" if show_id != show_ids[4] else "live"
    old_mtime = time.time() - (_RETENTION_DAYS + 1) * 86400
    fresh_mtime = time.time() - 86400
    file_size = 1024
    for i, show_id in enumerate(show_ids):
        take = tmp_path / "shows" / str(show_id) / "audio.wav"
        take.parent.mkdir(parents=True)
        take.write_bytes(b"\x01" * (file_size + 512 * i))
        os.utime(take, (old_mtime, old_mtime) if i < 3 else (fresh_mtime, fresh_mtime))
        with sandbox.db.session() as session:
            from app.models import Show

            session.get(Show, show_id).audio_file_path = str(take)

    # --- the Garage side: expired terminal jobs whose objects still exist
    expired_keys = [f"audio/{uuid.uuid4()}.aac" for _ in range(4)]
    garage_objects: dict[str, bytes] = {key: b"aac-bytes" for key in expired_keys}
    fail_key = expired_keys[2]  # this Garage delete fails -> its row must be KEPT

    async def delete_object(path: str) -> None:
        if path == fail_key:
            raise RuntimeError("garage 503 (soak)")
        garage_objects.pop(path, None)

    garage = MagicMock()
    garage.delete_object = AsyncMock(side_effect=delete_object)
    conn = _OutageRoutingConnection({"expired_jobs": [{"audio_path": key} for key in expired_keys]}, [])
    cleanup = _cleanup(
        conn, tmp_path, garage, show_audio_retention_days=_RETENTION_DAYS, export_retention_days=_RETENTION_DAYS
    )

    # old exports get swept, a fresh one stays (the exports leg of point 7)
    (tmp_path / "exports").mkdir(parents=True, exist_ok=True)
    old_export = tmp_path / "exports" / "mc_clanker_old.wav"
    fresh_export = tmp_path / "exports" / "mc_clanker_fresh.wav"
    for export in (old_export, fresh_export):
        export.write_bytes(b"\x02" * 2048)
    os.utime(old_export, (old_mtime, old_mtime))

    # --- seeded outage windows over the FIRST cleanup cycle (fixed-seed RNG)
    rng = random.Random(20260915)
    conn.windows = [
        (time.monotonic() + rng.uniform(0.0, 0.02), rng.uniform(0.1, 0.5)) for _ in range(params.p7_windows)
    ]
    asyncio.run(_drive_cycle(cleanup))
    # wait out the windows so the reconciliation cycle runs clean
    latest_end = max((start + duration for start, duration in conn.windows), default=time.monotonic())
    time.sleep(max(0.0, latest_end - time.monotonic() + 0.05))

    # --- delete_show under a route-level DB outage: NO half-delete may survive
    dead_ids = [show_ids[3], show_ids[4]]  # one 'ended', one 'live'
    owner = SimpleNamespace(id=sandbox.user_id)
    with patch("app.routes.utils.get_current_user_from_request", return_value=owner):
        mp = pytest.MonkeyPatch()
        mp.setattr(shows_routes, "DatabaseManager", _ExplodingDbManager)
        try:
            blocked = soak_app_client.delete(f"/api/shows/{dead_ids[0]}")
        finally:
            mp.undo()
        assert blocked.status_code >= 500, "the route-level outage must surface as 5xx"
        dead_path = tmp_path / "shows" / str(dead_ids[0]) / "audio.wav"
        assert dead_path.exists(), "the outage must not half-delete (row+file survive together)"

        for show_id in dead_ids:
            response = soak_app_client.delete(f"/api/shows/{show_id}")
            assert response.status_code == 204, f"the recovered delete for {show_id} must succeed"

    # --- the clean reconciliation cycle (windows are over: must not raise)
    total = asyncio.run(_drive_cycle(cleanup))
    assert total >= 1, "the clean cycle must reclaim the expired jobs"

    # --- zero unreferenced Garage objects (the failed delete's row is KEPT)
    assert set(garage_objects) == {fail_key}, (
        f"unreferenced Garage objects survived: {set(garage_objects) - {fail_key}}"
    )
    keep_deletes = [
        entry for entry in conn.calls if entry[0] == "execute" and "audio_path = ANY" in " ".join(entry[1].split())
    ]
    assert keep_deletes, "the expired-rows DELETE never ran"
    kept_paths = list(keep_deletes[-1][2][0])
    assert fail_key in kept_paths, "the row whose object delete failed must be kept for retry"
    assert not (set(expired_keys) - {fail_key}) & set(kept_paths), "deleted objects' rows must not be kept"

    # --- zero orphaned recording/export files; bytes == the retention-bounded live set
    survivors = [show_id for show_id in show_ids if show_id not in dead_ids]
    remaining = {str(path) for path in (tmp_path / "shows").rglob("*") if path.is_file()}
    expected_files = {str(tmp_path / "shows" / str(show_ids[5]) / "audio.wav")}
    assert remaining == expected_files, (
        f"orphaned recording files survived: {remaining - expected_files} (expired + deleted shows' files must go)"
    )
    exports_left = {str(path) for path in (tmp_path / "exports").iterdir()}
    assert exports_left == {str(fresh_export)}, "the exports retention sweep must remove only expired exports"
    on_disk = sum(path.stat().st_size for path in (tmp_path / "shows").rglob("*") if path.is_file())
    assert on_disk == file_size + 512 * 5, "on-disk recording bytes must equal exactly the live set"

    # --- the DB holds exactly the surviving shows (ids read INSIDE the session —
    # expire_on_commit detaches instances after the context, the REL-13a bug)
    with sandbox.db.session() as session:
        from app.models import Show

        surviving_ids = sorted(row.id for row in session.query(Show).all())
        paths = {row.id: row.audio_file_path for row in session.query(Show).all()}
    assert surviving_ids == sorted(survivors), "the DB must hold exactly the surviving shows"
    assert paths[show_ids[5]] is not None, "the surviving show keeps its recording path"


def test_p7_outage_window_never_wedges_the_cycle(tmp_path, isolated_export_db):
    """P7 guard: EVERY pass failing (a total-outage cycle) still returns —
    the cycle must come back wedge-free (the _run_pass isolation contract)."""
    conn = _OutageRoutingConnection({"expired_jobs": []}, [(time.monotonic(), 60.0)])
    cleanup = _cleanup(conn, tmp_path, MagicMock())
    total = asyncio.run(asyncio.wait_for(_drive_cycle(cleanup), timeout=10.0))
    assert total == 0, "a fully-outaged cycle reclaims nothing but must return"


# ---------------------------------------------------------------------------
# P8 — real-session capture + export round-trip (no DB mocks)
# ---------------------------------------------------------------------------


def _conductor_response(loop_i: int) -> dict:
    """Realistic conductor decision: 1 add + 2 retains, alternating fallback."""
    return {
        "master_bpm": 128,
        "master_key": "A minor",
        "name": "Fallback State" if loop_i % 2 else "Conductor Live",
        "reasoning": f"soak loop {loop_i}: hold the groove",
        "actions": [
            {
                "action_type": "add",
                "instrument": f"Pad {loop_i}",
                "sub_family": f"Pad {loop_i}",
                "major_family": "Synth",
                "model_id": "foundation-1",
            },
            {"action_type": "retain", "stem_index": 0},
            {"action_type": "retain", "stem_index": 1},
        ],
    }


_SOAK_STEMS = [
    {"instrument": "Electronic Drums", "prompt": "Electronic Drums, A minor, 128 BPM", "model_id": "foundation-1"},
    {"instrument": "Synth Bass", "prompt": "Synth Bass, A minor, 128 BPM", "model_id": "foundation-1"},
]


async def _capture_loops(show_id: int, start_index: int, loops: int) -> list[dict]:
    """The REAL capture path: append_loop_audit buffers one interaction + 3 actions per loop."""
    captured: list[dict] = []
    for i in range(loops):
        response = _conductor_response(start_index + i)
        captured.append({k: v for k, v in response.items() if not k.startswith("_")})
        await append_loop_audit(response, list(_SOAK_STEMS), start_index + i)
    assert state.current_show_id == show_id
    return captured


def test_p8_real_session_capture_and_export_roundtrip(isolated_export_db, soak_app_client):
    """Audit point 8: record N loops through the REAL capture path, flush,
    delete the LIVE show (its buffered rows drop loudly), and the NEXT flush
    still succeeds; both export endpoints return COMPLETE NDJSON."""
    params: SoakParams = soak_params()
    sandbox = isolated_export_db
    show_a = sandbox.make_show("soak live A")
    show_b = sandbox.make_show("survivor B")
    client = soak_app_client
    n = params.p8_loops

    # --- show A records N loops; the REAL adapter flushes to the real SQLite engine
    state.current_show_id = show_a
    state.current_show_start_time = time.time()
    asyncio.run(_capture_loops(show_a, 0, n))
    asyncio.run(AuditAdapter().flush())
    assert not state.llm_interaction_buffer and not state.action_buffer, "flush must drain the buffers"
    with sandbox.db.session() as session:
        from app.models import LLMInteraction, ShowAction

        interactions = session.query(LLMInteraction).filter(LLMInteraction.show_id == show_a).count()
        actions = session.query(ShowAction).filter(ShowAction.show_id == show_a).count()
    assert interactions == n, "N loops must persist N interactions"
    assert actions == 3 * n, "1 add + 2 retains per loop must persist 3N actions"

    # --- B records N loops (buffered, unflushed); A buffers N more, then dies LIVE
    state.current_show_id = show_b
    captured_b = asyncio.run(_capture_loops(show_b, 1000, n))
    state.current_show_id = show_a
    asyncio.run(_capture_loops(show_a, 2000, n))
    buffered_before = (len(state.llm_interaction_buffer), len(state.action_buffer))
    assert buffered_before == (2 * n, 6 * n), "both shows' tails must sit unflushed in the buffers"

    response = client.delete(f"/api/shows/{show_a}", headers=sandbox.headers)
    assert response.status_code == 204
    dropped_a = sum(1 for row in state.llm_interaction_buffer if row.get("show_id") == show_a)
    assert dropped_a == 0, "delete_show must drop EVERY buffered row of the deleted show"
    kept_b = sum(1 for row in state.llm_interaction_buffer if row.get("show_id") == show_b)
    assert kept_b == n, f"the survivor's buffered rows must be untouched, got {kept_b}"

    # --- rel-14 amended contract: the NEXT flush after a delete-live-show SUCCEEDS
    asyncio.run(AuditAdapter().flush())
    assert not state.llm_interaction_buffer and not state.action_buffer
    with sandbox.db.session() as session:
        from app.models import LLMInteraction

        b_interactions = session.query(LLMInteraction).filter(LLMInteraction.show_id == show_b).count()
        a_interactions = session.query(LLMInteraction).filter(LLMInteraction.show_id == show_a).count()
    assert b_interactions == n, "B's flushed tail must persist (rel-14: the next flush after a delete succeeds)"
    assert a_interactions == 0, "the deleted show's audit history must be gone (cascade)"

    # --- both chunked exports: COMPLETE NDJSON for exactly B's rows
    reasoning = client.get(f"/api/llm-config/reasoning-logs/export?show_id={show_b}", headers=sandbox.headers)
    assert reasoning.status_code == 200
    assert "application/x-ndjson" in reasoning.headers.get("content-type", "")
    lines = reasoning.text.splitlines()
    assert len(lines) == n, f"the reasoning export must be complete (no truncation), got {len(lines)}"
    rows = [json.loads(line) for line in lines]
    assert [row["loop_index"] for row in rows] == sorted(row["loop_index"] for row in rows)
    assert sum(1 for row in rows if row["was_fallback"]) == n // 2, "the alternating fallback flags must round-trip"
    assert all(row["reasoning"].startswith("soak loop") for row in rows)

    llm_dump = client.get(f"/api/shows/{show_b}/export/llm-dump", headers=sandbox.headers)
    assert llm_dump.status_code == 200
    dump_lines = llm_dump.text.splitlines()
    assert len(dump_lines) == n, "the llm-dump export must be complete (no truncation)"
    dump_rows = [json.loads(line) for line in dump_lines]
    # lossless round-trip (invariant 4): the assistant turn IS the captured response
    for i, row in enumerate(dump_rows):
        assert json.loads(row["messages"][-1]["content"]) == captured_b[i], (
            f"parsed_response must round-trip byte-exact (row {i})"
        )

    # --- the aggregate views sharing the same path smoke clean
    stats = client.get(f"/api/llm-config/reasoning-logs/stats?show_id={show_b}", headers=sandbox.headers)
    timeline = client.get(f"/api/llm-config/reasoning-timeline?show_id={show_b}", headers=sandbox.headers)
    assert stats.status_code == 200 and timeline.status_code == 200
