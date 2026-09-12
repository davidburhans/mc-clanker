"""rel-04 `rel-llm-capture` acceptance suite (REL-04 + REL-14-amended + DPO field audit).

TDD-red suite for unit 4 of refactor/plans/rel-remediation-plan.md (§U4, amended).
Encodes the contract from refactor/plans/units/rel-04-plan.md §3.1 (tests T1–T16):

- REL-04: audit buffers stay bounded — periodic threshold flush from P12 routed
  through the ctor-injected AuditSinkPort; crash loses at most the unflushed
  tail; best-effort bounded flush at lifespan shutdown; stop-flush kept.
- REL-14 (amended): no silent discard — start_show flushes before its buffer
  reset and RETAINS rows when the flush fails; delete_show loudly drops only the
  deleted show's buffered rows so the FK-poison loop is gone.
- DPO field audit: the conductor's exact chat is captured (``prompt_messages``
  becomes the {role, content} list), a transport-key convention keeps parsed
  responses schema-pure, ``applied_actions`` records the enacted stems with
  generation outcome, and ``to_llm_dump_dict`` emits a training-consumer row.

Fixture patterns are mirrored from: test_loop_fixes.py (_reset_audit_state,
_FakeMixer _run_loop driver), test_framework_characterization.py (Gap-8 fake
DB), test_round3_fix_d.py (SQLite + shows routes + owner patch), and
test_round3_fix_c.py (_FakeAsyncLLMClient conductor wiring).
"""

import asyncio
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.app_ui import app
from app.framework.audit_recording import append_loop_audit, flush_recording_buffers
from app.framework.framework_main_async import AsyncFrameworkLoop
from app.framework.framework_state import state
from app.framework.loop_steps import _CommitResult
from app.framework.pregeneration import run_pregeneration

os.environ["DATABASE_URL"] = ""  # Force SQLite for tests (test_shows_api.py pattern)


# --------------------------------------------------------------------------- #
# Shared builders
# --------------------------------------------------------------------------- #


def _valid_parsed_response() -> dict:
    """A schema-valid conductor decision (passes training.dpo_pipeline validation)."""
    return {
        "master_bpm": 128,
        "master_key": "C minor",
        "actions": [{"action_type": "retain", "stem_index": 0}],
        "reasoning": "keep the groove going",
        "name": "Test Set",
    }


def _request_messages() -> list[dict[str, str]]:
    """The exact system+user chat the conductor sends (decision 7)."""
    return [
        {"role": "system", "content": "You are an expert AI DJ."},
        {"role": "user", "content": "Current State:\nMaster BPM: 128\nYOUR TASK: provide DJ actions."},
    ]


def _applied_actions() -> list[dict]:
    """One enacted-stem row per plan decision 9 (sub_family/major_family/model_id/bars/age/outcome)."""
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


def _three_stems() -> list[dict]:
    """P6-shaped stems: index 1 will be a cache hit (absent from the outcomes map)."""

    def stem(sub_family: str, age: int) -> dict:
        return {
            "prompt": f"{sub_family}, 128 BPM, C minor",
            "model_id": "foundation-1",
            "bpm": 128,
            "key": "C minor",
            "bars": 4,
            "_age": age,
            "_original_details": {
                "sub_family": sub_family,
                "major_family": "Drums" if "Drum" in sub_family else "Synth",
                "model_id": "foundation-1",
                "bars": 4,
                "_age": age,
            },
        }

    return [stem("Electronic Drums", 2), stem("Synth Pad", 5), stem("Synth Lead", 0)]


def _buffered_llm_row(show_id: int, loop_idx: int) -> dict:
    """Minimal bulk-insert-valid LLMInteraction mapping (columns match the model)."""
    return {
        "show_id": show_id,
        "loop_index": loop_idx,
        "timestamp": datetime.now(timezone.utc),
        "relative_time_ms": loop_idx * 1000,
        "prompt_messages": {"stub": "legacy context-summary dict"},
        "parsed_response": None,
        "reasoning": None,
        "error": None,
        "was_fallback": False,
    }


def _buffered_action_row(show_id: int, loop_idx: int) -> dict:
    return {
        "show_id": show_id,
        "loop_index": loop_idx,
        "timestamp": datetime.now(timezone.utc),
        "relative_time_ms": loop_idx * 1000,
        "action_type": "retain",
        "stem_index": 0,
        "stem_details": {"index": 0},
        "action_description": f"Retained stem at loop {loop_idx}",
    }


# --------------------------------------------------------------------------- #
# Fakes (mirroring existing suite fixtures)
# --------------------------------------------------------------------------- #


class _FakeMessage:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str | None) -> None:
        self.message = _FakeMessage(content)


class _FakeCompletion:
    def __init__(self, content: str | None) -> None:
        self.choices = [_FakeChoice(content)]


class _FakeAsyncLLMClient:
    """Named fake for openai.AsyncOpenAI (test_round3_fix_c.py pattern)."""

    def __init__(self, content: str | None) -> None:
        self.content = content
        self.calls: list[dict] = []

    @property
    def chat(self):
        owner = self

        class _Completions:
            async def create(self, **kwargs):
                owner.calls.append(kwargs)
                return _FakeCompletion(owner.content)

        class _Chat:
            completions = _Completions()

        return _Chat()


class _SpyAuditSink:
    """Ctor-injectable AuditSinkPort fake recording flush calls (T1)."""

    def __init__(self) -> None:
        self.flush_calls = 0
        self.fail = False

    async def append_loop(self, conductor_response, active_stems, loop_idx) -> None:
        await append_loop_audit(conductor_response, active_stems, loop_idx)

    async def flush(self) -> None:
        self.flush_calls += 1
        if self.fail:
            raise RuntimeError("flush boom")


class _CaptureSession:
    """Records bulk_insert_mappings rows + batch sizes into the owning _ScriptedDB.

    Rows persist only on commit (like a real DB): the failed flush attempt in T2
    must leave nothing behind so the re-queue + retry can be observed exactly.
    """

    def __init__(self, db: "_ScriptedDB") -> None:
        self._db = db
        self._pending: dict[str, list] = {}
        self._pending_batches: list[tuple[str, int]] = []

    def bulk_insert_mappings(self, model, mappings) -> None:
        rows = list(mappings)
        self._pending.setdefault(model.__name__, []).extend(rows)
        self._pending_batches.append((model.__name__, len(rows)))

    def commit(self) -> None:
        for name, rows in self._pending.items():
            self._db.rows.setdefault(name, []).extend(rows)
        self._db.batches.extend(self._pending_batches)

    def rollback(self) -> None:
        self._pending = {}
        self._pending_batches = []

    def close(self) -> None:
        pass


class _ScriptedDB:
    """Fake DatabaseManager (characterization Gap-8 pattern): records bulk writes.

    ``fail`` toggles simulated commit outages to exercise the flush re-queue.
    """

    def __init__(self) -> None:
        self.rows: dict[str, list] = {}
        self.batches: list[tuple[str, int]] = []
        self.fail = False

    @contextmanager
    def session(self):
        sess = _CaptureSession(self)
        try:
            yield sess
            if self.fail:
                raise RuntimeError("simulated DB outage (commit failed)")
            sess.commit()
        finally:
            sess.close()


class _PoisonBulkSession:
    """Delegates a real session but makes bulk_insert_mappings fail (T5b)."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def bulk_insert_mappings(self, *args, **kwargs) -> None:
        raise RuntimeError("simulated DB outage (bulk insert failed)")

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _PoisonBulkDB:
    """DatabaseManager stand-in: real sessions, poisoned bulk inserts only."""

    def __init__(self, real) -> None:
        self._real = real

    @contextmanager
    def session(self):
        with self._real.session() as inner:
            yield _PoisonBulkSession(inner)


class _CaptureMixer:
    """Mixer stand-in so _run_loop runs without audio hardware (loop_fixes pattern
    plus ``loop_position_seconds`` returning 0 so P13 breaks immediately)."""

    sample_rate = 44100
    current_sample = 0
    current_loop_end_sample = 0

    def __init__(self) -> None:
        self.lock = threading.Lock()

    def clear(self) -> None:
        self.current_loop_end_sample = 0

    def prime_loop(self, tracks, *, duration_samples) -> None:
        self.current_loop_end_sample = self.current_sample + duration_samples

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def pop_transition_event(self):
        return None

    def loop_position_seconds(self) -> float:
        return 0.0

    def set_next_loop(self, *args, **kwargs) -> None:
        pass


class _FixedConductor:
    """ConductorPort fake replaying one fixed decision (never touches network)."""

    def __init__(self) -> None:
        self._response = _valid_parsed_response()

    async def get_next_state_async(self, **kwargs) -> dict:
        return dict(self._response)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _init_db():
    from app.db import DatabaseManager

    DatabaseManager.get_instance().create_tables()


@pytest.fixture(autouse=True)
def _reset_capture_state():
    """Keep the global state singleton clean between tests (round3_fix_d shape)."""
    state.reset()
    state.shutdown_event.clear()
    state.is_running = True
    state.is_generating = False
    # loop_count is NOT cleared by state.reset(); a foreign suite (e.g.
    # test_api.py) can leave it non-zero, so the harness zeroes it to keep the
    # "exactly N committed loops" contract absolute.
    state.loop_count = 0
    state.dj_password = ""
    state.audience_password = ""
    state.current_show_id = None
    state.current_show_start_time = None
    state.current_show_sink = None
    state.is_show_recording = False
    state.is_show_started = False
    state.is_recording = False
    state.export_sink = None
    state.recording_file_path = None
    state.llm_interaction_buffer = []
    state.action_buffer = []
    yield
    state.shutdown_event.clear()
    state.is_running = True
    state.is_generating = False
    state.current_show_id = None
    state.current_show_start_time = None
    state.current_show_sink = None
    state.is_show_recording = False
    state.is_show_started = False
    state.llm_interaction_buffer = []
    state.action_buffer = []


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Keep recordings inside the tmp dir (never the repo's data dir)."""
    monkeypatch.setenv("SHOWS_DIR", str(tmp_path / "shows"))
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path / "exports"))


@pytest.fixture
def client():
    return TestClient(app)


def _owner():
    user = MagicMock()
    user.id = 1
    user.username = "rel04-owner"
    user.is_active = True
    return user


def _make_show(status: str) -> int:
    from app.db import DatabaseManager
    from app.models import Show

    db = DatabaseManager.get_instance()
    with db.session() as session:
        show = Show(user_id=1, title=f"rel04 {uuid.uuid4().hex[:6]}", status=status)
        session.add(show)
        session.flush()
        return show.id


def _unique_show_id() -> int:
    """A show id that cannot collide with rows left in the shared dev SQLite file."""
    return int(time.time_ns() % 900_000_000) + 1


# --------------------------------------------------------------------------- #
# _run_loop harness (T3/T4)
# --------------------------------------------------------------------------- #


def _install_fake_db(monkeypatch) -> _ScriptedDB:
    import app.db as db_mod

    fake_db = _ScriptedDB()
    monkeypatch.setattr(db_mod.DatabaseManager, "get_instance", lambda: fake_db)
    return fake_db


def _patch_threshold(monkeypatch, threshold: int) -> None:
    import app.framework.loop_steps as loop_steps_mod

    monkeypatch.setattr(loop_steps_mod, "AUDIT_FLUSH_THRESHOLD_ROWS", threshold, raising=False)


async def _wait_until(predicate, timeout: float = 10.0) -> None:
    """Yield-driven poll (real asyncio.sleep(0) yields; bounded so hangs fail)."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for loop progress")
        await asyncio.sleep(0)


def _prime_loop_state(show_id: int) -> None:
    state.is_generating = True
    state.is_running = True
    state.shutdown_event.clear()
    state.current_show_id = show_id
    state.current_show_start_time = time.time()


# --------------------------------------------------------------------------- #
# T1 — REL-04 wiring: P12 threshold flush routes through the audit port
# --------------------------------------------------------------------------- #


async def test_post_commit_flushes_past_threshold_via_audit_port():
    """T1: >200 buffered rows -> P12 awaits the injected sink's flush once.

    At exactly 200 rows nothing flushes; a raising sink must not escape P12
    (the module flush re-queues internally; the loop retries next iteration).
    """
    commit = _CommitResult(
        needs_pregen=False,
        needs_initial_record=False,
        rec_stems=[],
        rec_set_name="",
        rec_reasoning="",
        state_snapshot={},
    )

    spy = _SpyAuditSink()
    loop = AsyncFrameworkLoop(uuid.uuid4(), audit=spy)
    loop._loop_idx = 2  # not 1: P12's loop-1 branch must not run
    state.llm_interaction_buffer = [_buffered_llm_row(9, i) for i in range(201)]
    await loop._step_post_commit(commit, [], 0)
    assert spy.flush_calls == 1

    at_threshold = _SpyAuditSink()
    loop_at = AsyncFrameworkLoop(uuid.uuid4(), audit=at_threshold)
    loop_at._loop_idx = 2
    state.llm_interaction_buffer = [_buffered_llm_row(9, i) for i in range(200)]
    await loop_at._step_post_commit(commit, [], 0)
    assert at_threshold.flush_calls == 0

    failing = _SpyAuditSink()
    failing.fail = True
    loop_fail = AsyncFrameworkLoop(uuid.uuid4(), audit=failing)
    loop_fail._loop_idx = 2
    state.llm_interaction_buffer = [_buffered_llm_row(9, i) for i in range(201)]
    await loop_fail._step_post_commit(commit, [], 0)  # must not raise
    assert failing.flush_calls == 1


# --------------------------------------------------------------------------- #
# T2 — flush failure re-queues without loss, order preserved
# --------------------------------------------------------------------------- #


async def test_flush_failure_requeues_without_loss(monkeypatch):
    """T2: a failing flush re-prepends every row IN ORDER; a later working flush
    persists the full corpus (re-queued rows before newer appends)."""
    fake_db = _install_fake_db(monkeypatch)

    llm_rows = [_buffered_llm_row(31, i) for i in range(205)]
    action_rows = [_buffered_action_row(31, i) for i in range(60)]
    state.llm_interaction_buffer = list(llm_rows)
    state.action_buffer = list(action_rows)

    fake_db.fail = True
    await flush_recording_buffers()  # swallows internally and re-queues
    assert state.llm_interaction_buffer == llm_rows
    assert state.action_buffer == action_rows

    newer = _buffered_llm_row(31, 999)
    state.llm_interaction_buffer.append(newer)

    fake_db.fail = False
    await flush_recording_buffers()
    assert fake_db.rows["LLMInteraction"] == llm_rows + [newer]
    assert fake_db.rows["ShowAction"] == action_rows
    assert state.llm_interaction_buffer == []
    assert state.action_buffer == []


# --------------------------------------------------------------------------- #
# T3/T4 — buffer bounded across loops; crash loses at most the unflushed tail
# --------------------------------------------------------------------------- #


def _build_loop_harness(monkeypatch, threshold: int):
    """AsyncFrameworkLoop + fake mixer/conductor/db + patched threshold."""
    _patch_threshold(monkeypatch, threshold)
    fake_db = _install_fake_db(monkeypatch)
    loop = AsyncFrameworkLoop(uuid.uuid4(), conductor=_FixedConductor())
    loop.mixer = _CaptureMixer()
    loop.running = True

    async def _noop_pregen(for_loop_idx, snapshot):
        loop._pregen_done.set()

    monkeypatch.setattr(loop, "_pre_generate_next_loop", _noop_pregen)
    return loop, fake_db


async def test_buffer_bounded_across_loops(monkeypatch):
    """T3: N driven loops -> exactly N interaction rows reach the DB; every
    flushed llm batch and the final buffer stay <= threshold + per-loop growth."""
    threshold = 3
    n_loops = 12
    loop, fake_db = _build_loop_harness(monkeypatch, threshold)
    _prime_loop_state(show_id=77)

    appends = {"n": 0}
    real_append = loop._append_loop_audit

    async def counting_append(conductor_response, active_stems, loop_idx):
        await real_append(conductor_response, active_stems, loop_idx)
        appends["n"] += 1
        if appends["n"] >= n_loops:
            state.is_generating = False
            loop.running = False

    monkeypatch.setattr(loop, "_append_loop_audit", counting_append)

    task = asyncio.create_task(loop._run_loop())
    await asyncio.wait_for(task, timeout=15.0)

    completed = state.loop_count
    assert completed == n_loops, f"expected {n_loops} committed loops, got {completed}"
    llm_in_db = fake_db.rows.get("LLMInteraction", [])
    assert len(llm_in_db) == completed
    llm_batches = [size for name, size in fake_db.batches if name == "LLMInteraction"]
    assert all(size <= threshold + 2 for size in llm_batches), llm_batches
    assert len(state.llm_interaction_buffer) <= threshold + 2


async def test_crash_loses_at_most_unflushed_tail(monkeypatch):
    """T4: cancelling the loop mid-show ("crash") loses at most threshold+1 rows;
    every appended row is either in the DB or still buffered (nothing else lost)."""
    threshold = 3
    k_loops = 7
    loop, fake_db = _build_loop_harness(monkeypatch, threshold)
    _prime_loop_state(show_id=77)

    park = asyncio.Event()
    appends = {"n": 0}
    real_append = loop._append_loop_audit

    async def counting_append(conductor_response, active_stems, loop_idx):
        await real_append(conductor_response, active_stems, loop_idx)
        appends["n"] += 1
        if appends["n"] >= k_loops:
            await park.wait()  # park mid-iteration: the cancel lands here

    monkeypatch.setattr(loop, "_append_loop_audit", counting_append)

    task = asyncio.create_task(loop._run_loop())
    await _wait_until(lambda: appends["n"] >= k_loops)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    rows_in_db = len(fake_db.rows.get("LLMInteraction", []))
    assert rows_in_db >= k_loops - (threshold + 1)
    assert rows_in_db + len(state.llm_interaction_buffer) == k_loops


# --------------------------------------------------------------------------- #
# T5/T5b — start_show: flush before clearing, no silent discard
# --------------------------------------------------------------------------- #


def _seed_show_buffer_rows(show_id: int) -> tuple[list, list]:
    llm_rows = [_buffered_llm_row(show_id, i) for i in range(5)]
    action_rows = [_buffered_action_row(show_id, i) for i in range(2)]
    state.llm_interaction_buffer = list(llm_rows)
    state.action_buffer = list(action_rows)
    return llm_rows, action_rows


def test_start_show_flushes_before_clearing_no_silent_discard(client):
    """T5: starting show B must persist the previous show's buffered rows first
    (invariant 4: the buffers ARE the fine-tuning corpus) — never discard them."""
    from app.db import DatabaseManager
    from app.models import LLMInteraction, ShowAction

    show_a = _make_show("ended")
    show_b = _make_show("draft")
    llm_rows, action_rows = _seed_show_buffer_rows(show_a)

    with patch("app.routes.utils.get_current_user_from_request", return_value=_owner()):
        response = client.post(f"/api/shows/{show_b}/start")
    assert response.status_code == 200, response.text

    db = DatabaseManager.get_instance()
    with db.session() as session:
        assert session.query(LLMInteraction).filter(LLMInteraction.show_id == show_a).count() == len(llm_rows)
        assert session.query(ShowAction).filter(ShowAction.show_id == show_a).count() == len(action_rows)
    assert state.llm_interaction_buffer == []
    assert state.action_buffer == []


def test_start_show_retains_rows_when_flush_fails(client, monkeypatch, capsys):
    """T5b: when the flush fails, start_show must RETAIN the buffered rows (the
    old unconditional clear recreated the silent-discard bug it was meant to fix)."""
    import app.db as db_mod

    show_a = _make_show("ended")
    show_b = _make_show("draft")
    _seed_show_buffer_rows(show_a)

    real_db = db_mod.DatabaseManager.get_instance()
    monkeypatch.setattr(db_mod.DatabaseManager, "get_instance", lambda: _PoisonBulkDB(real_db))

    with patch("app.routes.utils.get_current_user_from_request", return_value=_owner()):
        response = client.post(f"/api/shows/{show_b}/start")
    assert response.status_code == 200, response.text

    assert len(state.llm_interaction_buffer) == 5
    assert len(state.action_buffer) == 2
    assert state.llm_interaction_buffer[0]["show_id"] == show_a
    assert "retaining 5" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# T6/T7 — delete_show: poison gone, only the deleted show's rows dropped
# --------------------------------------------------------------------------- #


def _seed_llm_rows_for(show_id: int, count: int) -> list:
    rows = [_buffered_llm_row(show_id, i) for i in range(count)]
    state.llm_interaction_buffer = rows
    state.action_buffer = []
    return rows


def _seed_mixed_buffers(show_a: int, show_b: int) -> None:
    state.llm_interaction_buffer = [_buffered_llm_row(show_a, i) for i in range(4)]
    state.llm_interaction_buffer += [_buffered_llm_row(show_b, i) for i in range(3)]
    state.action_buffer = []


def _delete_show(client, show_id: int) -> None:
    with patch("app.routes.utils.get_current_user_from_request", return_value=_owner()):
        response = client.delete(f"/api/shows/{show_id}")
    assert response.status_code == 204, response.text


def test_delete_live_show_drops_only_its_buffered_rows(client, capsys):
    """T6: deleting the LIVE show drops exactly its own buffered rows (loudly),
    keeping the other show's pending rows intact."""
    show_a = _make_show("live")
    show_b = _make_show("draft")
    _seed_mixed_buffers(show_a, show_b)
    state.current_show_id = show_a
    state.current_show_sink = None
    state.is_show_recording = True
    state.is_show_started = True

    _delete_show(client, show_a)

    assert {row["show_id"] for row in state.llm_interaction_buffer} == {show_b}
    assert len(state.llm_interaction_buffer) == 3
    assert "dropped 4" in capsys.readouterr().out


async def test_delete_live_show_then_more_loops_and_flush_succeeds(client):
    """T7 (acceptance): after deleting the live show, later loops + a flush
    persist ONLY the new show's rows — the FK-poison loop is gone."""
    from app.db import DatabaseManager
    from app.models import LLMInteraction, ShowAction

    show_a = _make_show("live")
    show_b = _make_show("draft")
    _seed_llm_rows_for(show_a, 4)
    state.current_show_id = show_a
    state.current_show_sink = None
    state.is_show_recording = True
    state.is_show_started = True

    _delete_show(client, show_a)
    assert state.llm_interaction_buffer == []  # T6 contract: the poison rows are gone

    state.current_show_id = show_b
    state.current_show_start_time = time.time()
    loop = AsyncFrameworkLoop(uuid.uuid4())
    for i in range(3):
        await loop._append_loop_audit(_valid_parsed_response(), _active_stems(), i + 1)

    await flush_recording_buffers()  # must succeed with zero A rows anywhere

    db = DatabaseManager.get_instance()
    with db.session() as session:
        assert session.query(LLMInteraction).filter(LLMInteraction.show_id == show_b).count() == 3
        assert session.query(ShowAction).filter(ShowAction.show_id == show_b).count() == 3
        assert session.query(LLMInteraction).filter(LLMInteraction.show_id == show_a).count() == 0
        assert session.query(ShowAction).filter(ShowAction.show_id == show_a).count() == 0


# --------------------------------------------------------------------------- #
# T8 — lifespan shutdown flushes the audit tail, best-effort and bounded
# --------------------------------------------------------------------------- #


def _patch_lifespan_deps(monkeypatch) -> None:
    """Hermetic lifespan: parked framework task, fake garage, no onboarding I/O."""
    import app.app_ui as app_ui_mod
    import app.garage_client as garage_mod
    import app.onboarding as onboarding_mod

    class _FakeGarage:
        async def ensure_bucket_exists(self) -> None:
            return None

    async def _park_forever(session_id):
        await asyncio.Event().wait()

    async def _fake_onboarding():
        return []

    monkeypatch.setattr(app_ui_mod, "run_framework_loop_async", _park_forever)
    monkeypatch.setattr(garage_mod, "create_garage_client_from_env", lambda: _FakeGarage())
    monkeypatch.setattr(onboarding_mod, "run_onboarding_checks", _fake_onboarding)


def test_shutdown_lifespan_flushes_audit_tail(client, monkeypatch):
    """T8: entering+leaving the lifespan awaits flush_recording_buffers exactly once."""
    import app.framework.audit_recording as audit_recording_mod

    mock_flush = AsyncMock()
    monkeypatch.setattr(audit_recording_mod, "flush_recording_buffers", mock_flush)
    _patch_lifespan_deps(monkeypatch)

    with TestClient(app):
        pass
    mock_flush.assert_awaited_once()


def test_shutdown_lifespan_survives_flush_failure(client, monkeypatch):
    """T8 variant: a failing shutdown flush must not break shutdown."""
    import app.framework.audit_recording as audit_recording_mod

    mock_flush = AsyncMock(side_effect=RuntimeError("db down at shutdown"))
    monkeypatch.setattr(audit_recording_mod, "flush_recording_buffers", mock_flush)
    _patch_lifespan_deps(monkeypatch)

    with TestClient(app):  # must not raise
        pass
    mock_flush.assert_awaited_once()


def test_shutdown_lifespan_flush_timeout_is_bounded(client, monkeypatch):
    """T8 variant: a hung flush cannot hang shutdown (bounded by the module constant)."""
    import app.framework.audit_recording as audit_recording_mod

    async def _never_returns():
        await asyncio.Event().wait()

    mock_flush = AsyncMock(side_effect=_never_returns)
    monkeypatch.setattr(audit_recording_mod, "flush_recording_buffers", mock_flush)
    monkeypatch.setattr(
        audit_recording_mod, "FLUSH_SHUTDOWN_FLUSH_TIMEOUT_SECONDS", 0.05, raising=False
    )
    _patch_lifespan_deps(monkeypatch)

    with TestClient(app):  # exit completes despite the hung flush
        pass
    mock_flush.assert_awaited_once()


# --------------------------------------------------------------------------- #
# T9–T15 — DPO field audit: capture the real chat, outcomes, and export
# --------------------------------------------------------------------------- #


async def test_conductor_attaches_exact_request_messages(monkeypatch):
    """T9: get_next_state_async attaches the exact [system, user] chat it sent
    under the ``_request_messages`` transport key."""
    from app.framework.framework_conductor_async import ConductorLLMAsync

    conductor = ConductorLLMAsync(api_base="http://llm.invalid", model_name="test-model")
    fake = _FakeAsyncLLMClient(json.dumps(_valid_parsed_response()))
    monkeypatch.setattr(conductor, "_get_async_client", lambda config=None: fake)

    response = await conductor.get_next_state_async(
        current_bpm=128,
        current_key="C minor",
        active_stems=[{"prompt": "Drums, Electronic Drums, 128 BPM", "_age": 2}],
        user_override="dark techno",
    )

    msgs = response.get("_request_messages")
    assert msgs is not None, "conductor must attach _request_messages"
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert msgs[0]["content"] == conductor.system_instruction
    user_content = msgs[1]["content"]
    assert "Master BPM: 128" in user_content
    assert "Index 0 (age 2)" in user_content
    assert "OVERRIDE: dark techno" in user_content


async def test_append_loop_audit_stores_chat_strips_transport_keys():
    """T10: transport keys persist in dedicated fields and are stripped from
    parsed_response; responses without them keep the legacy context + None."""
    loop = AsyncFrameworkLoop(uuid.uuid4())
    state.current_show_id = 42
    state.current_show_start_time = 1_000_000.0

    chat = _request_messages()
    applied = _applied_actions()
    resp = dict(_valid_parsed_response())
    resp["_request_messages"] = chat
    resp["_applied_actions"] = applied
    await loop._append_loop_audit(resp, _active_stems(), 3)

    row = state.llm_interaction_buffer[0]
    assert row["prompt_messages"] == chat
    assert all(not key.startswith("_") for key in row["parsed_response"])
    assert row["applied_actions"] == applied

    plain = dict(_valid_parsed_response())  # no transport keys (fallbacks/fakes)
    await loop._append_loop_audit(plain, _active_stems(), 4)
    row2 = state.llm_interaction_buffer[1]
    assert isinstance(row2["prompt_messages"], dict)
    assert row2["prompt_messages"]["loop_index"] == 4
    assert row2["applied_actions"] is None


async def test_flush_roundtrips_all_captured_fields_sqlite():
    """T11: chat + applied_actions + the conductor context survive the real
    SQLite flush round-trip with every column populated."""
    from app.db import DatabaseManager
    from app.models import LLMInteraction

    show_id = _unique_show_id()
    state.current_show_id = show_id
    state.current_show_start_time = time.time() - 3.0

    resp = dict(_valid_parsed_response())
    resp["_request_messages"] = _request_messages()
    resp["_applied_actions"] = _applied_actions()
    await append_loop_audit(resp, _active_stems(), 3)
    await flush_recording_buffers()

    db = DatabaseManager.get_instance()
    with db.session() as session:
        row = session.query(LLMInteraction).filter(LLMInteraction.show_id == show_id).one()
        assert row.applied_actions == _applied_actions()
        assert row.prompt_messages == _request_messages()
        assert row.bpm == 128
        assert row.key == "C minor"
        assert row.set_name == "Test Set"
        assert row.instruments == ["Electronic Drums"]
        assert row.action_type == "retain"
        assert row.reasoning == "keep the groove going"


async def test_llm_dump_contains_every_captured_field_and_validates():
    """T12 (acceptance, DPO): to_llm_dump_dict emits the training-consumer row —
    messages [system, user, assistant-JSON-string], meta with every captured
    field, and an assistant turn that validates against the DPO schema."""
    from app.models.llm_interaction import LLMInteraction
    from training.dpo_pipeline import validate_conductor_schema

    interaction = LLMInteraction(
        show_id=5,
        loop_index=3,
        relative_time_ms=12_345,
        prompt_messages=_request_messages(),
        parsed_response=_valid_parsed_response(),
        applied_actions=_applied_actions(),
        reasoning="keep the groove going",
        error=None,
        was_fallback=False,
        bpm=128,
        key="C minor",
        set_name="Test Set",
        instruments=["Electronic Drums"],
        action_type="retain",
    )
    dump = interaction.to_llm_dump_dict()

    assert [m["role"] for m in dump["messages"]] == ["system", "user", "assistant"]
    assistant_content = dump["messages"][-1]["content"]
    assert validate_conductor_schema(assistant_content) is True
    assert json.loads(assistant_content) == _valid_parsed_response()
    assert set(dump["meta"]) == {
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
    assert dump["response"] == _valid_parsed_response()
    assert "reasoning" not in dump and "error" not in dump


async def test_llm_dump_tolerates_legacy_prompt_messages():
    """T13: legacy rows whose prompt_messages is the old context-summary dict
    degrade to an assistant-only dump instead of raising."""
    from app.models.llm_interaction import LLMInteraction

    interaction = LLMInteraction(
        show_id=5,
        loop_index=1,
        relative_time_ms=0,
        prompt_messages={"loop_index": 1, "note": "legacy context-summary stub"},
        parsed_response=_valid_parsed_response(),
        reasoning="legacy",
        error=None,
        was_fallback=False,
    )
    dump = interaction.to_llm_dump_dict()
    assert isinstance(dump["messages"], list)
    assert [m["role"] for m in dump["messages"]] == ["assistant"]
    assert json.loads(dump["messages"][0]["content"]) == _valid_parsed_response()


async def test_await_jobs_fetch_returns_stem_outcomes(monkeypatch):
    """T14: P8 returns {orig_idx: "generated" | "failed"}; the applied-actions
    builder defaults missing (cache-hit) stems to "cached"."""
    loop = AsyncFrameworkLoop(uuid.uuid4())
    loop.mixer = _CaptureMixer()
    stems = _three_stems()
    job0, job2 = uuid.uuid4(), uuid.uuid4()
    pending = [(job0, 0, "cache-key-0"), (job2, 2, "cache-key-2")]

    async def fake_await_jobs(job_ids, timeout=120.0):
        return {job0: "audio/ok.aac", job2: None}

    async def fake_fetch(audio_path):
        return np.zeros((4, 2), dtype=np.float32)

    monkeypatch.setattr(loop, "_await_jobs", fake_await_jobs)
    monkeypatch.setattr(loop, "_fetch_audio", fake_fetch)

    outcomes = await loop._step_await_jobs_fetch(pending, stems)
    assert outcomes == {0: "generated", 2: "failed"}

    from app.framework.audit_recording import _audit_applied_actions

    applied = _audit_applied_actions(stems, outcomes)
    assert [row["outcome"] for row in applied] == ["generated", "cached", "failed"]
    assert applied[0]["sub_family"] == "Electronic Drums"
    assert applied[0]["major_family"] == "Drums"
    assert applied[0]["model_id"] == "foundation-1"
    assert applied[0]["bars"] == 4
    assert applied[0]["age"] == 2


async def test_pregen_results_carry_capture_fields(monkeypatch):
    """T15: the pregen path carries _request_messages + stem_outcomes so both
    loop paths capture identically; the P2 reconstruction feeds the chat into
    the buffered row (never the fallback stub)."""
    loop = AsyncFrameworkLoop(uuid.uuid4())
    loop.mixer = _CaptureMixer()
    loop.stem_cache = {}
    chat = _request_messages()
    resp = dict(_valid_parsed_response())
    resp["actions"] = [
        {
            "action_type": "add",
            "sub_family": "Synth Lead",
            "major_family": "Synth",
            "model_id": "foundation-1",
            "timbre_tags": ["warm"],
            "notation_tag": "melody",
            "fx_tag": "dry",
            "bars": 4,
        }
    ]
    resp["_request_messages"] = chat
    loop.conductor = _FixedConductor()
    loop.conductor._response = resp

    async def fake_submit(**kwargs):
        return uuid.uuid4()

    async def fake_await(job_ids, timeout=120.0):
        return {job_id: "audio/x.aac" for job_id in job_ids}

    async def fake_fetch(audio_path):
        return np.zeros((4, 2), dtype=np.float32)

    monkeypatch.setattr(loop, "_submit_job", fake_submit)
    monkeypatch.setattr(loop, "_await_jobs", fake_await)
    monkeypatch.setattr(loop, "_fetch_audio", fake_fetch)
    monkeypatch.setattr(loop, "_build_prompt", lambda track, key, bpm: f"{track['sub_family']} prompt")

    snapshot = {
        "current_bpm": 128,
        "current_key": "C minor",
        "active_stems": [],
        "llm_config": {"base_url": "x", "api_key": "k", "model": "m"},
        "user_override": "",
        "available_instruments": ["Any"],
        "stem_history": [],
    }
    await run_pregeneration(loop, 3, snapshot)

    results = loop._pregen_results
    assert results is not None
    assert results["_request_messages"] == chat
    assert results["stem_outcomes"] == {0: "generated"}

    state.is_generating = True
    loop._loop_idx = 3
    decision = await loop._step_pregen_decision()
    assert decision.pregen_ready is True
    assert decision.conductor_response["_request_messages"] == chat

    state.current_show_id = 88
    state.current_show_start_time = 1_000_000.0
    await loop._append_loop_audit(decision.conductor_response, [], 3)
    assert state.llm_interaction_buffer[0]["prompt_messages"] == chat


# --------------------------------------------------------------------------- #
# T16 — stop flush kept (keep-green pin alongside the new threshold path)
# --------------------------------------------------------------------------- #


async def test_stop_show_flush_persists_remaining_rows(client):
    """T16: start -> append 2 loops -> POST stop persists both rows (the
    existing stop-flush behavior, now alongside the periodic threshold path)."""
    from app.db import DatabaseManager
    from app.models import LLMInteraction

    show_b = _make_show("draft")
    with patch("app.routes.utils.get_current_user_from_request", return_value=_owner()):
        started = client.post(f"/api/shows/{show_b}/start")
    assert started.status_code == 200, started.text
    assert state.current_show_id == show_b

    loop = AsyncFrameworkLoop(uuid.uuid4())
    for i in range(2):
        await loop._append_loop_audit(_valid_parsed_response(), _active_stems(), i + 1)
    assert len(state.llm_interaction_buffer) == 2

    with patch("app.routes.utils.get_current_user_from_request", return_value=_owner()):
        stopped = client.post(f"/api/shows/{show_b}/stop")
    assert stopped.status_code == 200, stopped.text

    db = DatabaseManager.get_instance()
    with db.session() as session:
        assert session.query(LLMInteraction).filter(LLMInteraction.show_id == show_b).count() == 2
