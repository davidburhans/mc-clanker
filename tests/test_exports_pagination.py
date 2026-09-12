"""rel-13 TDD-red suite (REL-13 + REL-30): real-session chunked exports, SQL
stats/timeline, limit clamps.

Encodes the contract from refactor/plans/units/rel-13-plan.md §3.1 (T1–T13).
The REL-13a regression is precisely about UNMOCKED sessions: the export
generators used to iterate ORM instances after the session committed+expired
them → DetachedInstanceError after headers were sent. Every export test here
runs the whole request path (auth middleware → owner check → route → shaper)
against a real per-test SQLite engine (``isolated_export_db``, conftest) with
zero DB mocks.

Red-reason map (plan §3.4 step 1):
- T1/T2: TestClient raises DetachedInstanceError mid-stream today.
- T3: same broken stream (its keyset WHERE pin activates post-implementation).
- T4: same broken stream on the llm-dump path.
- T5: stream broken + session totals wrong for chunking (3 vs 2+ceil(N/page)).
- T6/T7: the SQL spy catches the bare unbounded full-table SELECT.
- T8: the SQL spy catches the two materializing .all() scans.
- T9–T12: out-of-range limit values return 200 today, must be 422.
- T13: errors pre-implementation (app.lib.export_chunks does not exist yet);
  pins the chunk generator's abort-loudly failure semantics afterwards.
"""

import asyncio
import json
import time
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.app_ui import app
from app.db import DatabaseManager
from app.framework.audit_recording import append_loop_audit, flush_recording_buffers
from app.framework.framework_state import state
from app.models import LLMInteraction, ShowAction

# Captured once at import, before any test can monkeypatch the class attribute:
# lets a test install several independent counters without wrapping each other.
_PRISTINE_SESSION = DatabaseManager.session

_EXPORT_PAGE_SIZE = 7  # T1's 21 rows = exactly 3 chunks


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _shrink_export_page(monkeypatch, size: int = _EXPORT_PAGE_SIZE) -> None:
    """Point EXPORT_CHUNK_ROWS at a tiny page once the rel-13 helper module lands.

    During the TDD-red window the module does not exist and the routes run one
    unpaginated .all(); the suite's red there is the DetachedInstanceError
    regression, so the pin degrades to a no-op instead of masking it. After
    rel-13 §2.1 the monkeypatch always applies and every export test drives
    multi-chunk scans.
    """
    try:
        from app.lib import export_chunks
    except ImportError:  # TDD-red window: helper module not implemented yet
        return
    monkeypatch.setattr(export_chunks, "EXPORT_CHUNK_ROWS", size)


class _SessionCounter:
    """Counting wrapper result for DatabaseManager.session (plan T5)."""

    def __init__(self):
        self.open_now = 0
        self.max_open = 0
        self.total = 0

    def enter(self):
        self.open_now += 1
        self.total += 1
        self.max_open = max(self.max_open, self.open_now)

    def exit(self):
        self.open_now -= 1


def install_session_counter(monkeypatch) -> _SessionCounter:
    """Wrap DatabaseManager.session so every open/close is counted (+1 enter / −1 exit)."""
    counter = _SessionCounter()

    @contextmanager
    def counting_session(manager_self):
        counter.enter()
        try:
            with _PRISTINE_SESSION(manager_self) as session:
                yield session
        finally:
            counter.exit()

    monkeypatch.setattr(DatabaseManager, "session", counting_session)
    return counter


class _CapturedSelects:
    """Engine spy collecting every statement hitting one table (plan T6/T7/T8)."""

    def __init__(self, engine, table: str):
        self._engine = engine
        self._table = table
        self.statements: list[str] = []

    def __enter__(self):
        event.listen(self._engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc_info):
        event.remove(self._engine, "before_cursor_execute", self._record)
        return False

    def _record(self, conn, cursor, statement, parameters, context, executemany):
        if self._table in statement.lower():
            self.statements.append(statement.lower())


_AGGREGATE_MARKERS = ("group by", "count(", "avg(", "sum(", "min(", "max(", "case when")


def _is_bounded_or_aggregate(statement: str) -> bool:
    """rel-13 contract: no bare unbounded full-entity SELECT may reach SQLite/PG."""
    return "limit" in statement or any(marker in statement for marker in _AGGREGATE_MARKERS)


# Captured-field builders lifted from tests/test_llm_capture.py (the rel-04
# capture suite): identical shapes so export assertions compare against the
# canonical capture vocabulary instead of restating it.


def _valid_parsed_response() -> dict:
    """Schema-valid conductor decision (passes training.dpo_pipeline validation)."""
    return {
        "master_bpm": 128,
        "master_key": "C minor",
        "actions": [{"action_type": "retain", "stem_index": 0}],
        "reasoning": "keep the groove going",
        "name": "Test Set",
    }


def _request_messages() -> list[dict[str, str]]:
    """The exact system+user chat the conductor sends."""
    return [
        {"role": "system", "content": "You are an expert AI DJ."},
        {"role": "user", "content": "Current State:\nMaster BPM: 128\nYOUR TASK: provide DJ actions."},
    ]


def _applied_actions() -> list[dict]:
    """One enacted-stem row (sub_family/major_family/model_id/bars/age/outcome)."""
    return [
        {
            "sub_family": "Electronic Drums",
            "major_family": "Drums",
            "model_id": "foundation-1",
            "bars": 4,
            "age": 2,
            "outcome": "generated",
        }
    ]


def _active_stems() -> list[dict]:
    return [
        {
            "prompt": "Drums, Electronic Drums, 128 BPM",
            "instrument": "Electronic Drums",
            "model_id": "foundation-1",
            "bpm": 128,
            "key": "C minor",
            "bars": 4,
        }
    ]


@pytest.fixture
def app_client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_state():
    """Fresh framework state + open Basic gate (test_reasoning_logs pattern)."""
    state.reset()
    state.dj_password = ""
    state.audience_password = ""
    yield


# --------------------------------------------------------------------------- #
# T1–T8: chunked real-session exports and SQL-side aggregation
# --------------------------------------------------------------------------- #


def test_reasoning_export_real_session_complete_ndjson(isolated_export_db, monkeypatch, app_client):
    """T1 (acceptance, REL-13a/b regression): exporting MORE than one page of a
    real show yields every row as parseable NDJSON in stable format.

    Red today: the generator iterates ORM instances after the session committed
    and expired them → DetachedInstanceError after headers were sent.
    """
    _shrink_export_page(monkeypatch)
    sandbox = isolated_export_db
    show_id = sandbox.make_show("rel13 reasoning export")
    sandbox.insert_interactions(show_id, 21, reasoning=lambda index: f"keep {index}")

    response = app_client.get(f"/api/llm-config/reasoning-logs/export?show_id={show_id}", headers=sandbox.headers)

    assert response.status_code == 200
    assert "application/x-ndjson" in response.headers.get("content-type", "")
    assert "attachment" in response.headers.get("content-disposition", "")
    lines = response.text.splitlines()
    assert len(lines) == 21, "every row must be exported exactly once (invariant 4: complete corpus)"
    rows = [json.loads(line) for line in lines]
    expected_keys = set(
        LLMInteraction(show_id=0, loop_index=0, relative_time_ms=0, prompt_messages=[]).to_reasoning_export_dict()
    )
    for row in rows:
        assert set(row) == expected_keys, "export format must not drift from the shaper"
    loop_indices = [row["loop_index"] for row in rows]
    assert loop_indices == sorted(loop_indices), "loop order must be preserved"


def test_llm_dump_export_real_session_complete_ndjson(isolated_export_db, monkeypatch, app_client):
    """T2 (acceptance, REL-13a/b): the llm-dump export streams the training-corpus
    row (messages/response/meta) for every row of a real show, assistant turn
    byte-exact json.dumps(parsed_response). Red today (same detach mechanism)."""
    _shrink_export_page(monkeypatch)
    sandbox = isolated_export_db
    show_id = sandbox.make_show("rel13 llm dump")
    sandbox.insert_interactions(
        show_id,
        21,
        parsed_response=_valid_parsed_response(),
        prompt_messages=_request_messages(),
        reasoning=lambda index: f"keep {index}",
    )

    response = app_client.get(f"/api/shows/{show_id}/export/llm-dump", headers=sandbox.headers)

    assert response.status_code == 200
    lines = response.text.splitlines()
    assert len(lines) == 21
    rows = [json.loads(line) for line in lines]
    expected_meta_keys = {
        "loop_index",
        "relative_time_ms",
        "bpm",
        "key",
        "set_name",
        "instruments",
        "action_type",
        "applied_actions",
        "reasoning",
        "was_fallback",
        "error",
    }
    assistant_turn = {"role": "assistant", "content": json.dumps(_valid_parsed_response())}
    for row in rows:
        assert set(row) == {"messages", "response", "meta"}
        assert set(row["meta"]) == expected_meta_keys
        assert row["messages"][-1] == assistant_turn
        assert row["response"] == _valid_parsed_response()


def test_export_does_not_mix_concurrent_shows(isolated_export_db, monkeypatch, app_client):
    """T3: the keyset scan keeps the show_id predicate in EVERY chunk query.

    Two interleaved shows carry disjoint data markers (set_name/bpm/reasoning);
    exporting show A must return exactly A's rows only. Red today via the broken
    stream; the SQL-shape pin (every chunk SELECT filters show_id) guards the
    classic keyset bug of dropping the WHERE after page one.
    """
    _shrink_export_page(monkeypatch)
    sandbox = isolated_export_db
    show_a = sandbox.make_show("Show A")
    show_b = sandbox.make_show("Show B")
    for index in range(21):
        sandbox.insert_interactions(
            show_a, 1, loop_index=index, set_name="SetA", bpm=100.0 + index, reasoning=f"alpha-{index}"
        )
        sandbox.insert_interactions(
            show_b, 1, loop_index=index, set_name="SetB", bpm=200.0 + index, reasoning=f"beta-{index}"
        )

    with _CapturedSelects(sandbox.db.engine, "llm_interactions") as spy:
        response = app_client.get(f"/api/llm-config/reasoning-logs/export?show_id={show_a}", headers=sandbox.headers)

    assert response.status_code == 200
    rows = [json.loads(line) for line in response.text.splitlines() if line]
    assert len(rows) == 21, "exactly show A's rows, never a mix"
    for row in rows:
        assert row["set_name"] == "SetA"
        assert row["reasoning"].startswith("alpha-")
        assert row["bpm"] < 200.0
    assert len(spy.statements) >= 3, "three chunks of 7 must issue at least three queries"
    for statement in spy.statements:
        assert "show_id" in statement, f"chunk query lost the show scope: {statement}"


def test_export_roundtrips_every_captured_field(isolated_export_db, monkeypatch, app_client):
    """T4 (acceptance, invariant 4): the REAL capture path (append_loop_audit →
    flush) survives to the llm-dump export field-for-field — chat + response +
    every meta field — and the assistant turn validates against the DPO schema
    (test_llm_capture T12 precedent). Red today via the broken stream."""
    _shrink_export_page(monkeypatch, 1)
    sandbox = isolated_export_db
    show_id = sandbox.make_show("rel13 capture roundtrip")
    state.current_show_id = show_id
    state.current_show_start_time = time.time() - 3.0

    for loop_idx in (0, 1):
        conductor_response = dict(_valid_parsed_response())
        conductor_response["reasoning"] = f"loop {loop_idx} keep the groove"
        conductor_response["_request_messages"] = _request_messages()
        conductor_response["_applied_actions"] = _applied_actions()
        asyncio.run(append_loop_audit(conductor_response, _active_stems(), loop_idx))
    asyncio.run(flush_recording_buffers())

    response = app_client.get(f"/api/shows/{show_id}/export/llm-dump", headers=sandbox.headers)

    assert response.status_code == 200
    rows = [json.loads(line) for line in response.text.splitlines() if line]
    assert len(rows) == 2
    expected_chat = _request_messages() + [{"role": "assistant", "content": json.dumps(_valid_parsed_response())}]
    for row, loop_idx in zip(rows, (0, 1)):
        assert row["messages"] == expected_chat
        assert row["response"] == _valid_parsed_response()
        meta = row["meta"]
        assert meta["loop_index"] == loop_idx
        assert meta["bpm"] == 128
        assert meta["key"] == "C minor"
        assert meta["set_name"] == "Test Set"
        assert meta["instruments"] == ["Electronic Drums"]
        assert meta["action_type"] == "retain"
        assert meta["applied_actions"] == _applied_actions()
        assert meta["reasoning"] == f"loop {loop_idx} keep the groove"
        assert meta["was_fallback"] is False
        assert meta["error"] is None
        assert isinstance(meta["relative_time_ms"], int) and meta["relative_time_ms"] >= 0
    assert rows[1]["meta"]["relative_time_ms"] >= rows[0]["meta"]["relative_time_ms"]

    from training.dpo_pipeline import validate_conductor_schema

    assert validate_conductor_schema(rows[0]["messages"][-1]["content"]) is True


def test_chunked_sessions_released_between_chunks(isolated_export_db, monkeypatch, app_client):
    """T5 (acceptance, pool): one export can never exhaust the pool — at most one
    session open at a time, one session per chunk, all released.

    Red today: the stream dies with DetachedInstanceError and the request held
    3 sessions total (middleware + auth + one route session), not one per chunk.
    """
    _shrink_export_page(monkeypatch)
    sandbox = isolated_export_db
    show_id = sandbox.make_show("rel13 pool")
    sandbox.insert_interactions(show_id, 21)
    counter = install_session_counter(monkeypatch)

    response = app_client.get(f"/api/llm-config/reasoning-logs/export?show_id={show_id}", headers=sandbox.headers)

    assert response.status_code == 200
    assert len(response.text.splitlines()) == 21, "stream completes for a real session"
    assert counter.max_open == 1, "never two sessions open at once"
    assert counter.total >= 2 + 3, "auth + owner + one session per chunk (3 chunks of 7)"
    _assert_chunk_helper_releases_sessions(sandbox, show_id, counter, counter.total)

    if hasattr(sandbox.db.engine.pool, "checkedout"):
        assert sandbox.db.engine.pool.checkedout() == 0, "every connection returned to the pool"


def _assert_chunk_helper_releases_sessions(sandbox, show_id: int, counter, total_before: int) -> None:
    """Helper-level pool precision (plan T5): session released before each yield.

    Unreachable pre-implementation (the route assertion above fails first);
    once app.lib.export_chunks exists it pins the per-yield release exactly.
    """
    from app.lib.export_chunks import chunked_shaped_rows

    def build_query(session):
        return session.query(LLMInteraction).filter(LLMInteraction.show_id == show_id)

    rows = chunked_shaped_rows(
        sandbox.db,
        build_query,
        (LLMInteraction.loop_index, LLMInteraction.id),
        (LLMInteraction.loop_index, LLMInteraction.id),
        LLMInteraction.to_reasoning_export_dict,
        lambda row: (row.loop_index, row.id),
        page_size=_EXPORT_PAGE_SIZE,
    )
    seen = 0
    for _shaped in rows:
        seen += 1
        assert counter.open_now == 0, "session must be released before a shaped row is handed out"
    assert seen == 21
    assert counter.total - total_before == 3, "exactly one session per chunk"
    assert counter.max_open == 1


def test_stats_computed_in_sql_not_python_iteration(isolated_export_db, monkeypatch, app_client):
    """T6 (acceptance, REL-13c): stats aggregates run in SQL — every captured
    llm_interactions statement carries GROUP BY/an aggregate or a page LIMIT —
    while response values stay exactly the Python-loop semantics.

    Red today: the route hydrates the whole table with one bare SELECT.
    """
    sandbox = isolated_export_db
    show_id = sandbox.make_show("rel13 stats")
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

    with _CapturedSelects(sandbox.db.engine, "llm_interactions") as spy:
        response = app_client.get(f"/api/llm-config/reasoning-logs/stats?show_id={show_id}", headers=sandbox.headers)

    assert response.status_code == 200
    assert len(spy.statements) >= 2, "aggregates + instruments scan, not one full-table fetch"
    for statement in spy.statements:
        assert _is_bounded_or_aggregate(statement), f"unbounded full-entity SELECT in stats: {statement}"
    data = response.json()
    assert data["total_interactions"] == 3
    assert data["action_counts"] == {"retain": 1, "add": 1, "remove": 1}
    assert data["avg_bpm"] == 129.0
    assert data["bpm_range"] == {"min": 128.0, "max": 130.0}
    assert data["keys_used"] == ["C", "C minor"]
    assert data["instruments_used"] == ["Bass", "Drums"]
    assert data["fallback_count"] == 1
    assert data["fallback_rate"] == 0.333
    assert data["avg_reasoning_length"] == 8.0, "empty-string reasoning must not count (today's `if i.reasoning`)"


def test_timeline_computed_without_full_table_hydration(isolated_export_db, monkeypatch, app_client):
    """T7 (acceptance, REL-13c): timeline aggregates + detail lists come from
    bounded/aggregate SQL — never one full-table hydration — with today's
    segment semantics (30 s windows, duplicate post-reset loop_index, 200-char
    snippet truncation). Red today: the route runs one bare ordered SELECT."""
    _shrink_export_page(monkeypatch, 2)
    sandbox = isolated_export_db
    show_id = sandbox.make_show("rel13 timeline")
    sandbox.insert_interactions(
        show_id, 1, loop_index=0, relative_time_ms=0, action_type="retain",
        bpm=128.0, key="C minor", instruments=["Bass"], reasoning="x" * 250,
    )
    sandbox.insert_interactions(
        show_id, 1, loop_index=1, relative_time_ms=10_000, action_type="add",
        bpm=130.0, key="C minor", instruments=["Drums"], reasoning="add drums",
    )
    # Post-reset edge: relative_time_ms keeps growing while loop_index restarts.
    sandbox.insert_interactions(
        show_id, 1, loop_index=0, relative_time_ms=35_000, action_type="remove",
        bpm=126.0, key="C", instruments=["Bass"], reasoning="post-reset restart",
    )

    with _CapturedSelects(sandbox.db.engine, "llm_interactions") as spy:
        response = app_client.get(
            f"/api/llm-config/reasoning-timeline?show_id={show_id}&segment_seconds=30", headers=sandbox.headers
        )

    assert response.status_code == 200
    assert len(spy.statements) >= 2, "aggregate query + detail scan, not one full-table fetch"
    for statement in spy.statements:
        assert _is_bounded_or_aggregate(statement), f"unbounded full-entity SELECT in timeline: {statement}"

    data = response.json()
    assert data["total_interactions"] == 3
    assert data["segment_seconds"] == 30
    assert data["total_segments"] == 2
    seg0, seg1 = data["segments"]
    assert (seg0["seg_index"], seg0["start_ms"], seg0["end_ms"]) == (0, 0, 30_000)
    assert seg0["interaction_count"] == 2
    assert sorted(seg0["interaction_ids"]) == [1, 2]
    assert seg0["action_counts"] == {"retain": 1, "add": 1, "remove": 0, "other": 0}
    assert seg0["avg_bpm"] == 129.0
    assert seg0["instruments_used"] == ["Bass", "Drums"]
    assert seg0["key_changes"] == [
        {"loop_index": 0, "key": "C minor", "time_ms": 0},
        {"loop_index": 1, "key": "C minor", "time_ms": 10_000},
    ]
    assert len(seg0["reasoning_snippets"][0]["reasoning"]) == 200, "snippets truncate at 200 chars"
    assert seg0["reasoning_snippets"][1]["reasoning"] == "add drums"
    assert (seg1["seg_index"], seg1["start_ms"], seg1["end_ms"]) == (1, 30_000, 60_000)
    assert seg1["interaction_count"] == 1
    assert seg1["interaction_ids"] == [3]
    assert seg1["action_counts"] == {"retain": 0, "add": 0, "remove": 1, "other": 0}
    assert seg1["avg_bpm"] == 126.0
    assert seg1["key_changes"] == [{"loop_index": 0, "key": "C", "time_ms": 35_000}]
    assert seg1["reasoning_snippets"] == [
        {"loop_index": 0, "time_ms": 35_000, "reasoning": "post-reset restart", "action_type": "remove"}
    ]


def test_export_full_streams_complete_document(isolated_export_db, monkeypatch, app_client):
    """T8 (acceptance, REL-13b): /export/full keeps its exact JSON document
    contract while every table scan becomes page-bounded — the corpus is never
    materialized in one .all().

    Red today: the spy catches the two unbounded full-entity SELECTs."""
    _shrink_export_page(monkeypatch)
    sandbox = isolated_export_db
    show_id = sandbox.make_show("rel13 full")
    sandbox.insert_actions(show_id, 12)
    sandbox.insert_interactions(show_id, 15)

    with _CapturedSelects(sandbox.db.engine, "show_actions") as actions_spy:
        with _CapturedSelects(sandbox.db.engine, "llm_interactions") as interactions_spy:
            response = app_client.get(f"/api/shows/{show_id}/export/full", headers=sandbox.headers)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    document = response.json()
    assert set(document) == {"show", "actions", "llm_interactions"}
    assert document["show"]["id"] == show_id
    assert len(document["actions"]) == 12
    assert len(document["llm_interactions"]) == 15
    expected_action_keys = set(
        ShowAction(show_id=0, loop_index=0, relative_time_ms=0, action_type="retain").to_dict()
    )
    expected_interaction_keys = set(
        LLMInteraction(show_id=0, loop_index=0, relative_time_ms=0, prompt_messages=[]).to_dict()
    )
    for row in document["actions"]:
        assert set(row) == expected_action_keys
    for row in document["llm_interactions"]:
        assert set(row) == expected_interaction_keys
    assert len(actions_spy.statements) >= 2, "12 actions over pages of 7 → at least two chunk scans"
    assert len(interactions_spy.statements) >= 3, "15 interactions over pages of 7 → at least three chunk scans"
    for statement in actions_spy.statements + interactions_spy.statements:
        assert _is_bounded_or_aggregate(statement), f"materializing full-table SELECT in export/full: {statement}"


# --------------------------------------------------------------------------- #
# T9–T12: REL-30 limit clamps (mirror reasoning_logs.py Query(ge=, le=))
# --------------------------------------------------------------------------- #


def test_jobs_limit_clamped(isolated_export_db, app_client):
    """T9 (REL-30): /api/jobs rejects out-of-range limits with 422 and keeps the
    valid/default contract. Red today: limit=999999999 is silently honored."""
    sandbox = isolated_export_db
    for bad_limit in ("999999999", "0", "-1"):
        response = app_client.get(f"/api/jobs?limit={bad_limit}", headers=sandbox.headers)
        assert response.status_code == 422, f"limit={bad_limit} must be rejected"
    ok = app_client.get("/api/jobs?limit=500", headers=sandbox.headers)
    assert ok.status_code == 200
    assert ok.json()["limit"] == 500
    default = app_client.get("/api/jobs", headers=sandbox.headers)
    assert default.status_code == 200
    assert default.json()["limit"] == 50


def test_shows_limit_clamped(isolated_export_db, app_client):
    """T10 (REL-30): /api/shows clamps at Query(50, ge=1, le=500)."""
    sandbox = isolated_export_db
    response = app_client.get("/api/shows?limit=501", headers=sandbox.headers)
    assert response.status_code == 422
    ok = app_client.get("/api/shows?limit=500", headers=sandbox.headers)
    assert ok.status_code == 200
    assert ok.json()["limit"] == 500


def test_show_actions_limit_clamped(isolated_export_db, app_client):
    """T11 (REL-30): per-show actions keep the 1000 default but clamp at le=5000."""
    sandbox = isolated_export_db
    show_id = sandbox.make_show("rel13 actions clamp")
    sandbox.insert_actions(show_id, 3)
    for bad_limit in ("5001", "0"):
        response = app_client.get(f"/api/shows/{show_id}/actions?limit={bad_limit}", headers=sandbox.headers)
        assert response.status_code == 422, f"limit={bad_limit} must be rejected"
    ok = app_client.get(f"/api/shows/{show_id}/actions?limit=5000", headers=sandbox.headers)
    assert ok.status_code == 200
    assert ok.json()["limit"] == 5000
    default = app_client.get(f"/api/shows/{show_id}/actions", headers=sandbox.headers)
    assert default.status_code == 200
    assert default.json()["limit"] == 1000
    assert len(default.json()["actions"]) == 3


def test_show_llm_interactions_limit_clamped(isolated_export_db, app_client):
    """T12 (REL-30): per-show llm-interactions keep the 1000 default, clamp at le=5000."""
    sandbox = isolated_export_db
    show_id = sandbox.make_show("rel13 interactions clamp")
    sandbox.insert_interactions(show_id, 3)
    for bad_limit in ("5001", "0"):
        response = app_client.get(f"/api/shows/{show_id}/llm-interactions?limit={bad_limit}", headers=sandbox.headers)
        assert response.status_code == 422, f"limit={bad_limit} must be rejected"
    ok = app_client.get(f"/api/shows/{show_id}/llm-interactions?limit=5000", headers=sandbox.headers)
    assert ok.status_code == 200
    assert ok.json()["limit"] == 5000
    default = app_client.get(f"/api/shows/{show_id}/llm-interactions", headers=sandbox.headers)
    assert default.status_code == 200
    assert default.json()["limit"] == 1000
    assert len(default.json()["interactions"]) == 3


# --------------------------------------------------------------------------- #
# T13: mid-stream failure semantics (plan decision 8)
# --------------------------------------------------------------------------- #


def test_midstream_chunk_failure_aborts_loudly(isolated_export_db, monkeypatch):
    """T13: a chunk query failing mid-export raises out of the generator — never
    a silent skip — and every line yielded before the failure is complete JSON.

    Red pre-implementation with ModuleNotFoundError: the chunked generator
    contract under test (app.lib.export_chunks) lands with rel-13 §2.1.
    """
    from app.lib.export_chunks import chunked_shaped_rows

    sandbox = isolated_export_db
    show_id = sandbox.make_show("rel13 midstream failure")
    sandbox.insert_interactions(show_id, 14)

    generator = chunked_shaped_rows(
        sandbox.db,
        lambda session: session.query(LLMInteraction).filter(LLMInteraction.show_id == show_id),
        (LLMInteraction.loop_index, LLMInteraction.id),
        (LLMInteraction.loop_index, LLMInteraction.id),
        LLMInteraction.to_reasoning_export_dict,
        lambda row: (row.loop_index, row.id),
        page_size=_EXPORT_PAGE_SIZE,
    )
    yielded = [next(generator) for _ in range(_EXPORT_PAGE_SIZE)]

    @contextmanager
    def broken_session(manager_self):
        raise RuntimeError("db blip mid-export")
        yield  # pragma: no cover — the raise above is the contract

    monkeypatch.setattr(DatabaseManager, "session", broken_session)
    with pytest.raises(RuntimeError, match="db blip mid-export"):
        next(generator)

    for shaped in yielded:
        assert json.dumps(shaped)  # every earlier row serializes cleanly — no half-written rows
