"""Characterization tests for ``app.framework.framework_main_async``.

Phase 0 of the E1–E6 refactor (refactor/plan.md). These pin CURRENT behavior so
the extraction phases (2–7b) cannot silently regress it. Every test here MUST be
green at baseline (commit 906a49b, 542 passed). They are characterization, not
specification: if one would assert behavior the code does not have, the test is
wrong (or a latent bug was found) — never change production code in this phase.

Covers brief-04 §C gaps 1–8:
  Gap 1  process_actions retain/add/remove/dedup (incl. in-place _age mutation)
  Gap 2  _submit_job creates a pending GeneratorJob row with a ~24h TTL
  Gap 3  _fetch_audio swallows errors / empty bytes → None
  Gap 4  _run_loop loop-1 handoff records initial state
  Gap 5  _run_loop subsequent-loop set_next_loop kwargs
  Gap 6  _run_loop pregen-vs-fresh state shaping
  Gap 7  _run_loop populates state.last_actions
  Gap 8  flush_recording_buffers DB write + failure re-queue
"""

import asyncio
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from app.framework.framework_main_async import (
    AsyncFrameworkLoop,
    flush_recording_buffers,
    process_actions,
)
from app.framework.framework_state import state


# --------------------------------------------------------------------------- #
# State-singleton save/restore — broader than test_loop_fixes._reset_audit_state
# (covers the ~15 attrs a real loop mutates) so _run_loop tests don't leak.
# --------------------------------------------------------------------------- #

_LOOP_MUTATED_ATTRS = (
    "current_bpm",
    "current_key",
    "previous_stems",
    "active_stems",
    "next_stems",
    "stem_history",
    "current_set_name",
    "llm_reasoning",
    "last_actions",
    "loop_count",
    "stem_volumes",
    "muted_stems",
    "soloed_stems",
    "target_bpm_override",
    "target_key_override",
    "is_generating",
    "is_running",
    "should_reset",
    "user_override",
    "current_show_id",
    "current_show_start_time",
)


def _copy_attr(obj, attr):
    val = getattr(obj, attr)
    if isinstance(val, list):
        return list(val)
    if isinstance(val, dict):
        return dict(val)
    if isinstance(val, set):
        return set(val)
    return val


def _set_show_id(show_id):
    # GlobalState attrs are untyped (pyright infers None); setattr sidesteps the
    # assignment-type check while exercising real runtime behavior.
    setattr(state, "current_show_id", show_id)


def _job_row_values(row) -> dict:
    # Read ORM attribute values via getattr → Any, so `==` assertions don't trip
    # the SQLAlchemy ColumnElement conditional-operand check (pyright friction).
    return {k: getattr(row, k) for k in ("status", "instrument", "model_id", "bpm", "timbre_tags", "expires_at")}


def _snapshot_state():
    return {a: _copy_attr(state, a) for a in _LOOP_MUTATED_ATTRS}


def _restore_state(snap):
    for a, val in snap.items():
        setattr(state, a, val)
    state.llm_interaction_buffer.clear()
    state.action_buffer.clear()


@pytest.fixture(autouse=True)
def _reset_loop_state():
    """Snapshot/restore the state singleton around every test in this module."""
    state.shutdown_event.clear()
    snap = _snapshot_state()
    state.llm_interaction_buffer.clear()
    state.action_buffer.clear()
    state.current_show_id = None
    state.current_show_start_time = None
    state.is_generating = False
    state.is_running = True
    state.active_stems = []
    state.next_stems = []
    state.previous_stems = []
    state.stem_history = []
    state.last_actions = []
    state.loop_count = 0
    state.stem_volumes = {}
    state.muted_stems = set()
    state.soloed_stems = set()
    state.target_bpm_override = None
    state.target_key_override = None
    state.should_reset = False
    yield
    _restore_state(snap)
    state.shutdown_event.clear()


# --------------------------------------------------------------------------- #
# Fake mixer — records every handoff call + a stop hook so a full _run_loop
# drive terminates instead of hanging (brief-04 §D harness strengthening).
# --------------------------------------------------------------------------- #


class _FakeMixer:
    """Minimal mixer stand-in that records handoff calls for _run_loop tests."""

    def __init__(self, *, current_sample=0, stop_on=None, transition_events=None):
        self.sample_rate = 44100
        self.current_sample = current_sample
        self.current_loop_end_sample = 0
        self._current_loop_duration = None
        self.lock = threading.Lock()
        self.set_next_loop_calls = []
        self.add_track_internal_calls = []
        self.clear_calls = 0
        self.pop_calls = 0
        self.stop_calls = 0
        # stop hook: ("add", n) → stop after n-th _add_track_internal,
        #            ("setnext", n) → stop after n-th set_next_loop.
        self._stop_on = stop_on
        self._loop = None  # back-ref set by _seed_loop_for_run; needed to flip loop.running
        # Opt-in transition-event queue: when non-empty, pop_transition_event()
        # returns each idx in turn, then flips loop.running=False to terminate the
        # P13 await spin cleanly. When None/empty (default), always returns None —
        # identical to pre-extension behavior, so existing tests are unaffected.
        self._transition_events = list(transition_events) if transition_events else []
        self._transition_event_iter = iter(self._transition_events)

    def _maybe_stop(self, kind):
        if self._stop_on is None:
            return
        stop_kind, target = self._stop_on
        if kind != stop_kind:
            return
        count = len(self.add_track_internal_calls) if kind == "add" else len(self.set_next_loop_calls)
        if count >= target:
            # Terminate _run_loop cleanly: flip the loop's OWN running flag so
            # P13's `while self.running` spin exits immediately (setting only
            # state.is_running does NOT break P13), plus is_running for the
            # outer `while self.running and state.is_running`.
            if self._loop is not None:
                self._loop.running = False
            state.is_running = False

    def clear(self):
        self.clear_calls += 1

    def start(self):
        pass

    def stop(self):
        self.stop_calls += 1

    def pop_transition_event(self):
        self.pop_calls += 1
        if not self._transition_events:
            return None  # default: unchanged behavior for all existing tests
        try:
            return next(self._transition_event_iter)
        except StopIteration:
            # Transition queue exhausted: terminate the P13 await spin
            # (mirrors _maybe_stop's loop.running=False pattern).
            if self._loop is not None:
                self._loop.running = False
            return None

    def set_next_loop(self, tracks, next_loop_duration_samples=None, loop_idx=None):
        self.set_next_loop_calls.append(
            {
                "tracks": tracks,
                "next_loop_duration_samples": next_loop_duration_samples,
                "loop_idx": loop_idx,
            }
        )
        self._maybe_stop("setnext")

    def _add_track_internal(self, audio, start_sample, stem_idx):
        self.add_track_internal_calls.append({"audio": audio, "start_sample": start_sample, "stem_idx": stem_idx})
        self._maybe_stop("add")

    def _ensure_stereo(self, audio):
        return audio


def _patch_sleep_instant(monkeypatch):
    """Collapse every asyncio.sleep so loop waits/backoffs don't hang."""

    async def _instant(_delay=None):
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


def _wire_loop_common(loop, *, conductor_response=None, conductor_side_effect=None):
    """Attach a faked conductor + no-op audit/pregen so a loop drive has no I/O."""
    if conductor_side_effect is not None:
        loop.conductor.get_next_state_async = AsyncMock(side_effect=conductor_side_effect)
    else:
        loop.conductor.get_next_state_async = AsyncMock(return_value=conductor_response)
    loop._append_loop_audit = AsyncMock()
    loop._pre_generate_next_loop = AsyncMock()


def _wire_loop_no_io(loop, monkeypatch, *, response, pregen_sets_done=True):
    """Full no-I/O wiring for driving the real _run_loop.

    Fakes conductor + _submit_job + wait_for_multiple_jobs + _fetch_audio +
    audit + pregen + asyncio.sleep so a loop drive touches no network/GPU/DB.
    Job results are empty ({}), so no audio is fetched and stems fall back to
    silence — enough to exercise the mixer handoff paths.
    """
    import app.framework.loop_steps as steps

    loop.conductor.get_next_state_async = AsyncMock(return_value=response)
    loop._submit_job = AsyncMock(return_value=uuid.uuid4())
    loop._fetch_audio = AsyncMock(return_value=None)
    loop._append_loop_audit = AsyncMock()

    async def _fake_pregen(_idx, _snapshot):
        # The real pregen signals _pregen_done on completion; mirror that so the
        # step-11 wait loop breaks instead of spinning forever.
        if pregen_sets_done:
            loop._pregen_done.set()
        return None

    loop._pre_generate_next_loop = _fake_pregen
    monkeypatch.setattr(steps, "wait_for_multiple_jobs", AsyncMock(return_value={}))
    _patch_sleep_instant(monkeypatch)


def _seed_loop_for_run(loop, *, current_sample=12345, stop_on=None) -> _FakeMixer:
    """Set the loop + state flags a direct _run_loop drive needs."""
    fake = _FakeMixer(current_sample=current_sample, stop_on=stop_on)
    loop.mixer = fake  # type: ignore[assignment]  # intentional duck-typed fake
    fake._loop = loop
    loop.running = True
    state.is_generating = True
    state.is_running = True
    state.shutdown_event.clear()
    return fake


def _conductor_response_with_actions():
    """A response with retain + add so the action log is non-trivial."""
    return {
        "master_bpm": 128,
        "master_key": "A minor",
        "name": "Characterization Set",
        "reasoning": "layering a pad over the drums",
        "actions": [
            {"action_type": "retain", "stem_index": 0},
            {
                "action_type": "add",
                "major_family": "Synth",
                "sub_family": "Synth Pad",
                "model_id": "foundation-1",
                "timbre_tags": ["warm"],
                "notation_tag": "melody",
                "fx_tag": "Medium Reverb",
                "bars": 4,
            },
        ],
    }


def _active_stem_for_retain():
    """A single stem carrying _original_details so retain can age it in place."""
    return {
        "prompt": "Drums, Electronic Drums, 120 BPM",
        "instrument": "Electronic Drums",
        "model_id": "foundation-1",
        "major_family": "Drums",
        "sub_family": "Electronic Drums",
        "bpm": 120,
        "key": "A minor",
        "bars": 4,
        "_age": 1,
        "_original_details": {
            "model_id": "foundation-1",
            "major_family": "Drums",
            "sub_family": "Electronic Drums",
            "timbre_tags": ["hard", "punchy"],
            "notation_tag": "4/4",
            "fx_tag": "dry",
            "bars": 4,
            "_age": 1,
        },
    }


# --------------------------------------------------------------------------- #
# Gap 1 — process_actions retain/add/remove/dedup + in-place _age mutation
# --------------------------------------------------------------------------- #


def test_process_actions_retain_add_remove_dedup():
    """Gap 1: retain ages in place, add sets _age=0, remove drops, dup excluded.

    The retain mutation happens on the INPUT list's ``_original_details`` dict
    (verified L139) — a defensive deepcopy would pass a return-value check but
    silently break P6/P11 _age accounting.
    """
    stem0 = {
        "prompt": "Drums, Electronic Drums, 120 BPM",
        "_age": 2,
        "_original_details": {
            "model_id": "foundation-1",
            "major_family": "Drums",
            "sub_family": "Electronic Drums",
            "timbre_tags": ["hard", "punchy"],
            "notation_tag": "4/4",
            "fx_tag": "dry",
            "bars": 4,
            "_age": 2,
        },
    }
    stem1 = {
        "prompt": "Synth, Synth Lead, 120 BPM",
        "_age": 1,
        "_original_details": {
            "model_id": "foundation-1",
            "major_family": "Synth",
            "sub_family": "Synth Lead",
            "timbre_tags": ["bright"],
            "notation_tag": "melody",
            "fx_tag": "dry",
            "bars": 4,
            "_age": 1,
        },
    }
    active_stems = [stem0, stem1]

    actions = [
        {"action_type": "retain", "stem_index": 0},
        {
            "action_type": "add",
            "major_family": "Synth",
            "sub_family": "Synth Pad",
            "model_id": "foundation-1",
            "timbre_tags": ["warm"],
            "notation_tag": "melody",
            "fx_tag": "Medium Reverb",
            "bars": 4,
        },
        {"action_type": "remove", "stem_index": 1},
        # duplicate add (same dedup key) — must be excluded.
        {
            "action_type": "add",
            "major_family": "Synth",
            "sub_family": "Synth Pad",
            "model_id": "foundation-1",
            "timbre_tags": ["warm"],
            "notation_tag": "melody",
            "fx_tag": "Medium Reverb",
            "bars": 4,
        },
    ]

    result = process_actions(actions, active_stems)

    # Ordering: retain (idx0) first, then the single added Synth Pad.
    assert len(result) == 2, f"expected 2 after dedup, got {len(result)}"

    retained = result[0]
    assert retained["sub_family"] == "Electronic Drums"
    assert retained["_age"] == 3  # original stem _age (2) + 1

    added = result[1]
    assert added["sub_family"] == "Synth Pad"
    assert added["_age"] == 0  # new stems start at age 0

    # The remove excluded stem1 entirely.
    assert all(t.get("sub_family") != "Synth Lead" for t in result)

    # ── in-place mutation on the INPUT list ──────────────────────────────
    # process_actions writes _age onto active_stems[0]["_original_details"]
    # (orig is a reference, not a copy). A deepcopy refactor would leave the
    # input unchanged and break downstream _age accounting.
    assert active_stems[0]["_original_details"] is retained, (
        "retained track must be the SAME object as the input stem's _original_details (in-place mutation contract)"
    )
    assert active_stems[0]["_original_details"]["_age"] == 3


# --------------------------------------------------------------------------- #
# Gap 2 — _submit_job creates a pending GeneratorJob row with a ~24h TTL
# --------------------------------------------------------------------------- #


async def test_submit_job_creates_pending_row_with_ttl(monkeypatch):
    """Gap 2: _submit_job inserts a 'pending' GeneratorJob with expires_at≈+24h.

    Uses a capturing session (the real-DB row path is infeasible on SQLite:
    ``_submit_job`` passes the raw ``session_id`` UUID to ``GeneratorJob``, and
    SQLite cannot bind a UUID object — production runs on Postgres UUID columns).
    The capture verifies the exact row shape, status default, and ~24h TTL.
    """
    import app.db as db_mod

    class _CapturingSession:
        def __init__(self):
            self.added = []
            self.flushed = 0

        def add(self, obj):
            self.added.append(obj)

        def flush(self):
            # Simulate the DB assigning a primary key at flush time.
            self.flushed += 1
            for o in self.added:
                if getattr(o, "id", None) is None:
                    setattr(o, "id", uuid.uuid4())

        def refresh(self, _obj):
            pass

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    @contextmanager
    def _session_ctx():
        sess = _CapturingSession()
        captured["session"] = sess
        try:
            yield sess
        finally:
            sess.close()

    captured = {}
    fake_db = MagicMock()
    fake_db.session = lambda: _session_ctx()
    monkeypatch.setattr(db_mod.DatabaseManager, "get_instance", lambda: fake_db)

    loop = AsyncFrameworkLoop(uuid.uuid4())
    before = datetime.now(timezone.utc)

    job_id = await loop._submit_job(
        session_id=loop.session_id,
        instrument="Synth Lead",
        prompt="Synth, Synth Lead, warm, A minor, 120 BPM",
        major_family="Synth",
        model_id="foundation-1",
        key="A minor",
        bpm=120,
        timbre_tags=["warm"],
        bars=4,
    )
    after = datetime.now(timezone.utc)

    # Return value is the row id: a UUID on Postgres, UUID-shaped str on SQLite.
    assert uuid.UUID(str(job_id)) is not None  # raises if not UUID-shaped

    sess = captured["session"]
    assert sess.flushed >= 1, "expected session.flush() to be called"
    assert len(sess.added) == 1
    row = sess.added[0]
    vals = _job_row_values(row)
    assert vals["status"] == "pending"
    assert vals["instrument"] == "Synth Lead"
    assert vals["model_id"] == "foundation-1"
    assert vals["bpm"] == 120
    assert vals["timbre_tags"] == ["warm"]
    # TTL ≈ now + 24h (allow a few minutes of slack for test latency).
    ttl = vals["expires_at"]
    if ttl.tzinfo is None:
        ttl = ttl.replace(tzinfo=timezone.utc)
    lower = before + timedelta(hours=24) - timedelta(minutes=5)
    upper = after + timedelta(hours=24) + timedelta(minutes=5)
    assert lower <= ttl <= upper, f"expires_at {ttl} not within ~24h of now"


# --------------------------------------------------------------------------- #
# Gap 3 — _fetch_audio empty bytes and exception both return None (swallowed)
# --------------------------------------------------------------------------- #


async def test_fetch_audio_empty_bytes_and_exception_return_none():
    """Gap 3: empty bytes ⇒ None; garage raising ⇒ None (no propagation)."""
    loop = AsyncFrameworkLoop(uuid.uuid4())

    # Case A: get_object returns b"" → falsy → None (no decode attempted).
    garage_empty = MagicMock()
    garage_empty.get_object = AsyncMock(return_value=b"")
    loop._garage = garage_empty  # type: ignore[assignment]  # injecting a fake garage client
    assert await loop._fetch_audio("audio/x.aac") is None

    # Case B: get_object raises → swallowed → None (no propagation).
    # Reset the cached adapter so it rebuilds from the NEW garage client
    # (GarageAudioAdapter caches _garage_client after first use — without this
    # reset Case B would silently reuse garage_empty and the exception branch
    # would never run, leaving the test vacuous).
    loop._audio_adapter = None
    garage_err = MagicMock()
    garage_err.get_object = AsyncMock(side_effect=RuntimeError("s3 down"))
    loop._garage = garage_err  # type: ignore[assignment]  # injecting a fake garage client
    assert await loop._fetch_audio("audio/y.aac") is None
    # Prove Case B actually exercised the exception client (not Case A's).
    garage_err.get_object.assert_awaited()


# --------------------------------------------------------------------------- #
# Gap 8 — flush_recording_buffers DB write + failure re-queue
# --------------------------------------------------------------------------- #


class _FakeSession:
    """Records bulk_insert_mappings; optionally fails commit to exercise requeue."""

    def __init__(self, *, fail_commit=False):
        self.bulk_calls = []
        self._fail = fail_commit
        self.committed = False
        self.rolled_back = False

    def bulk_insert_mappings(self, model, mappings):
        self.bulk_calls.append((model, list(mappings)))

    def commit(self):
        if self._fail:
            raise RuntimeError("commit failed")
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


def _make_fake_db(captured, *, fail_commit):
    """Build a fake DatabaseManager whose session() mimics the real contextmgr."""

    @contextmanager
    def _session_ctx():
        sess = _FakeSession(fail_commit=fail_commit)
        captured["session"] = sess
        try:
            yield sess
            sess.commit()
        except Exception:
            sess.rollback()
            raise
        finally:
            sess.close()

    fake = MagicMock()
    fake.session = lambda: _session_ctx()
    return fake


async def test_flush_recording_buffers_success_writes_both_tables(monkeypatch):
    """Gap 8 (success): bulk_insert_mappings hits both tables + clears buffers."""
    import app.db as db_mod

    _set_show_id(7)
    state.llm_interaction_buffer = [{"show_id": 7, "loop_index": 1}]
    state.action_buffer = [{"show_id": 7, "action_type": "add"}]

    captured = {}
    monkeypatch.setattr(
        db_mod.DatabaseManager,
        "get_instance",
        lambda: _make_fake_db(captured, fail_commit=False),
    )

    await flush_recording_buffers()

    sess = captured["session"]
    assert sess.committed is True
    models_written = {m.__name__ for m, _ in sess.bulk_calls}
    assert "LLMInteraction" in models_written
    assert "ShowAction" in models_written
    assert state.llm_interaction_buffer == []
    assert state.action_buffer == []


async def test_flush_recording_buffers_failure_restores_buffers(monkeypatch):
    """Gap 8 (failure): a failing commit restores buffers (prepended back)."""
    import app.db as db_mod

    _set_show_id(100)
    llm_payload = [{"show_id": 100, "loop_index": 2}]
    act_payload = [{"show_id": 100, "action_type": "retain"}]
    state.llm_interaction_buffer = list(llm_payload)
    state.action_buffer = list(act_payload)

    captured = {}
    monkeypatch.setattr(
        db_mod.DatabaseManager,
        "get_instance",
        lambda: _make_fake_db(captured, fail_commit=True),
    )

    await flush_recording_buffers()

    sess = captured["session"]
    # bulk_insert_mappings ran for both tables before the commit failed.
    assert sess.rolled_back is True
    models_written = {m.__name__ for m, _ in sess.bulk_calls}
    assert "LLMInteraction" in models_written
    assert "ShowAction" in models_written
    # Buffers were restored (prepended back) after the failure.
    assert state.llm_interaction_buffer == llm_payload
    assert state.action_buffer == act_payload


# --------------------------------------------------------------------------- #
# _run_loop helpers (Gaps 4–7)
# --------------------------------------------------------------------------- #


def _state_view():
    """Read the loop-relevant state attrs as plain comparable values."""
    return {
        "current_bpm": state.current_bpm,
        "current_key": state.current_key,
        "current_set_name": state.current_set_name,
        "llm_reasoning": state.llm_reasoning,
        "last_actions": list(state.last_actions),
        "stem_sub_families": [
            s.get("_original_details", {}).get("sub_family", s.get("sub_family")) for s in state.active_stems
        ],
    }


def _add_only_response():
    """An add-only response (no retain/remove) so next_stems are input-agnostic."""
    return {
        "master_bpm": 128,
        "master_key": "A minor",
        "name": "Fresh Set",
        "reasoning": "starting with a pad",
        "actions": [
            {
                "action_type": "add",
                "major_family": "Synth",
                "sub_family": "Synth Pad",
                "model_id": "foundation-1",
                "timbre_tags": ["warm"],
                "notation_tag": "melody",
                "fx_tag": "Medium Reverb",
                "bars": 4,
            }
        ],
    }


# --------------------------------------------------------------------------- #
# Gap 4 — _run_loop loop-1 handoff records initial state
# --------------------------------------------------------------------------- #


async def test_run_loop_loop1_handoff_records_initial(monkeypatch):
    """Gap 4: loop 1 adds tracks at live current_sample + records loop 1.

    For loop_idx==1 the tracks are added at ``mixer.current_sample`` (not 0),
    ``current_loop_end_sample``/``_current_loop_duration`` are set, and
    ``record_loop_transition(1, ...)`` fires (no mixer transition event for
    loop 1).
    """
    from app.framework.framework_main_async import calc_duration

    loop = AsyncFrameworkLoop(uuid.uuid4())
    mixer = _seed_loop_for_run(loop, current_sample=12345, stop_on=("add", 1))
    state.active_stems = [_active_stem_for_retain()]
    state.current_bpm = 128
    state.current_key = "A minor"

    _wire_loop_no_io(loop, monkeypatch, response=_conductor_response_with_actions())

    record_calls = []
    monkeypatch.setattr(
        state,
        "record_loop_transition",
        lambda idx, stems, set_name, reason: record_calls.append((idx, list(stems), set_name, reason)),
    )

    await asyncio.wait_for(loop._run_loop(), timeout=5.0)

    # Tracks added at the live mixer.current_sample (not 0).
    assert len(mixer.add_track_internal_calls) >= 1
    assert mixer.add_track_internal_calls[0]["start_sample"] == 12345

    # _current_loop_duration + current_loop_end_sample set on the loop-1 path.
    # loop_bars = max(track bars + [8]) = 8; duration = calc_duration(128, 8).
    expected_duration = int(calc_duration(128, 8) * 44100)
    assert mixer._current_loop_duration == expected_duration
    assert mixer.current_loop_end_sample == 12345 + expected_duration

    # record_loop_transition(1, ...) fired for the first loop.
    assert record_calls, "expected record_loop_transition for loop 1"
    assert record_calls[0][0] == 1

    if loop._pregen_task is not None:
        loop._pregen_task.cancel()


# --------------------------------------------------------------------------- #
# Gap 5 — _run_loop subsequent-loop set_next_loop kwargs
# --------------------------------------------------------------------------- #


async def test_run_loop_subsequent_set_next_loop_kwargs(monkeypatch):
    """Gap 5: loop_idx>1 hands off via set_next_loop(tracks, kw, kw); no end set.

    Iteration 1 (fresh, loop_idx=1) runs and seeds _pregen_results for loop 2
    (the else-branch when needs_pregen is False). Iteration 2 (loop_idx=2) takes
    the pregen branch and calls ``mixer.set_next_loop(tracks,
    next_loop_duration_samples=…, loop_idx=2)``. The loop_idx>1 path does NOT
    write ``current_loop_end_sample`` (only reads it under lock).
    """
    from app.framework.framework_main_async import calc_duration

    loop = AsyncFrameworkLoop(uuid.uuid4())
    mixer = _seed_loop_for_run(loop, current_sample=12345, stop_on=("setnext", 1))
    state.active_stems = [_active_stem_for_retain()]
    state.current_bpm = 128
    state.current_key = "A minor"

    _wire_loop_no_io(loop, monkeypatch, response=_conductor_response_with_actions())

    await asyncio.wait_for(loop._run_loop(), timeout=5.0)

    # Iteration 2 (loop_idx=2) handed off via set_next_loop.
    assert len(mixer.set_next_loop_calls) == 1, f"expected 1 set_next_loop call, got {len(mixer.set_next_loop_calls)}"
    call = mixer.set_next_loop_calls[0]
    assert call["loop_idx"] == 2
    expected_duration = int(calc_duration(128, 8) * 44100)
    assert call["next_loop_duration_samples"] == expected_duration

    # The loop_idx>1 path does NOT set current_loop_end_sample: it still holds
    # iteration 1's value (current_sample + iter1 duration), proving iteration 2
    # only READ it (under lock) rather than writing it.
    assert mixer.current_loop_end_sample == 12345 + expected_duration

    if loop._pregen_task is not None:
        loop._pregen_task.cancel()


# --------------------------------------------------------------------------- #
# Gap 7 — _run_loop populates last_actions (fresh branch)
# --------------------------------------------------------------------------- #


async def test_run_loop_populates_last_actions(monkeypatch):
    """Gap 7: after one fresh-branch loop, state.last_actions has the parsed log.

    The fresh branch builds the action log from conductor_response actions:
    retain → "Retained <prompt sub-family>", add → "Added <sub_family>".
    """
    loop = AsyncFrameworkLoop(uuid.uuid4())
    _seed_loop_for_run(loop, current_sample=0, stop_on=("add", 1))
    state.active_stems = [_active_stem_for_retain()]
    state.current_bpm = 128
    state.current_key = "A minor"

    _wire_loop_no_io(loop, monkeypatch, response=_conductor_response_with_actions())

    await asyncio.wait_for(loop._run_loop(), timeout=5.0)

    # prompt "Drums, Electronic Drums, 120 BPM" → split(',')[1].strip() =
    # "Electronic Drums"; add sub_family "Synth Pad".
    assert state.last_actions == ["Retained Electronic Drums", "Added Synth Pad"], (
        f"unexpected last_actions: {state.last_actions}"
    )

    if loop._pregen_task is not None:
        loop._pregen_task.cancel()


# --------------------------------------------------------------------------- #
# Gap 6 — _run_loop pregen-vs-fresh state shaping
# (CHARACTERIZES A CURRENT DIVERGENCE — see note below.)
# --------------------------------------------------------------------------- #


async def test_run_loop_pregen_and_fresh_paths_state_shaping(monkeypatch):
    """Gap 6: characterize the fresh vs pregen-ready state delta.

    PLAN INTENT was "assert identical state deltas" for fresh vs pregen paths
    given the same conductor response. CURRENT behavior DIVERGES: the loop_idx>1
    pregen branch consumes ``_pregen_results`` seeded by the loop_idx==1 else
    branch, which carries ONLY {loop_idx, prepared_tracks,
    loop_duration_samples, next_stems} — NOT master_bpm/master_key/set_name/
    reasoning/actions. So the pregen branch falls back to defaults and CLOBBERS
    set_name/llm_reasoning/last_actions, while bpm/key/active_stems are
    preserved. This is exactly the divergence Phase 7a must reconcile. Asserting
    "identical" would be RED, so per the Phase-0 contract we assert the ACTUAL
    (divergent) behavior and report it.

    (The full-metadata pregen path — where ``_pre_generate_next_loop`` actually
    ran and populated all 9 keys — is only reachable from loop_idx>=3, since
    loop_idx==1 never starts a pregen task and loop_idx==2 consumes the
    else-branch's 4-key results. That path is out of reach for a robust
    single-_run_loop characterization here.)
    """
    # ── Run A: fresh path only (stop after loop 1) ──────────────────────
    loop_a = AsyncFrameworkLoop(uuid.uuid4())
    _seed_loop_for_run(loop_a, current_sample=0, stop_on=("add", 1))
    state.active_stems = []
    state.current_bpm = 120
    state.current_key = "C minor"
    _wire_loop_no_io(loop_a, monkeypatch, response=_add_only_response())
    await asyncio.wait_for(loop_a._run_loop(), timeout=5.0)
    fresh = _state_view()
    if loop_a._pregen_task is not None:
        loop_a._pregen_task.cancel()

    # ── Reset state to the same start, then run to loop 2 (pregen branch) ──
    state.active_stems = []
    state.current_bpm = 120
    state.current_key = "C minor"
    state.current_set_name = "Initial Vibe"
    state.llm_reasoning = "Waiting..."
    state.last_actions = []
    state.loop_count = 0
    state.previous_stems = []
    state.next_stems = []
    state.stem_history = []

    loop_b = AsyncFrameworkLoop(uuid.uuid4())
    _seed_loop_for_run(loop_b, current_sample=0, stop_on=("setnext", 1))
    state.active_stems = []
    state.current_bpm = 120
    state.current_key = "C minor"
    _wire_loop_no_io(loop_b, monkeypatch, response=_add_only_response())
    await asyncio.wait_for(loop_b._run_loop(), timeout=5.0)
    pregen = _state_view()
    if loop_b._pregen_task is not None:
        loop_b._pregen_task.cancel()

    # Preserved across both paths (bpm/key carried from state; stems identical):
    assert fresh["current_bpm"] == pregen["current_bpm"] == 128
    assert fresh["current_key"] == pregen["current_key"] == "A minor"
    assert fresh["stem_sub_families"] == pregen["stem_sub_families"] == ["Synth Pad"]

    # ── the divergence (current behavior): set_name/reasoning/last_actions ──
    # Fresh branch applied the response's metadata; the loop-2 pregen branch
    # (consuming the else-branch's metadata-less results) clobbered them.
    assert fresh["current_set_name"] == "Fresh Set"
    assert pregen["current_set_name"] == "Unknown Set"
    assert fresh["llm_reasoning"] == "starting with a pad"
    assert pregen["llm_reasoning"] == "No reasoning provided."
    assert fresh["last_actions"] == ["Added Synth Pad"]
    assert pregen["last_actions"] == []


# --------------------------------------------------------------------------- #
# Phase D0 (decomposition prep) — pin weakly-covered _run_loop phases.
# These characterize CURRENT behavior so the _step_* extraction cannot silently
# regress P3 (overrides), P4 (fallback), P7 (cache-HIT skip), P8 (foreground
# cache_stem). P13 transition-event recording is acknowledged-untested (its
# outside-lock invariant is already pinned by test_loop_lock_safety).
# --------------------------------------------------------------------------- #


async def test_d0_p3_override_applied_then_cleared(monkeypatch):
    """target_bpm/key_override are applied to current_bpm/key then cleared."""
    loop = AsyncFrameworkLoop(uuid.uuid4())
    _seed_loop_for_run(loop, current_sample=0, stop_on=("add", 1))
    state.active_stems = [_active_stem_for_retain()]
    state.target_bpm_override = 140
    state.target_key_override = "F major"
    _wire_loop_no_io(loop, monkeypatch, response=_conductor_response_with_actions())
    try:
        await asyncio.wait_for(loop._run_loop(), timeout=5.0)
        # Override wins over the conductor's master_bpm=128; overrides cleared.
        assert state.current_bpm == 140
        assert state.current_key == "F major"
        assert state.target_bpm_override is None
        assert state.target_key_override is None
    finally:
        state.target_bpm_override = None
        state.target_key_override = None


async def test_d0_p4_conductor_failure_uses_fallback(monkeypatch):
    """Conductor raising -> the retain-all fallback (not a crash); tracks commit."""
    loop = AsyncFrameworkLoop(uuid.uuid4())
    mixer = _seed_loop_for_run(loop, current_sample=0, stop_on=("add", 1))
    state.active_stems = [_active_stem_for_retain()]
    _wire_loop_no_io(loop, monkeypatch, response=_conductor_response_with_actions())
    # Override conductor to RAISE (exercises P4's nested try/except -> fallback).
    loop.conductor.get_next_state_async = AsyncMock(side_effect=RuntimeError("LLM down"))
    await asyncio.wait_for(loop._run_loop(), timeout=5.0)
    # Fallback retains all active stems -> at least one track handed to the mixer.
    assert len(mixer.add_track_internal_calls) >= 1


async def test_d0_p7_cached_stem_skips_job_submission(monkeypatch):
    """A stem already in stem_cache -> _submit_job NOT called for it (fresh path)."""
    from app.framework.conductor_interaction import process_actions

    loop = AsyncFrameworkLoop(uuid.uuid4())
    _seed_loop_for_run(loop, current_sample=0, stop_on=("add", 1))
    state.active_stems = []  # add-only -> exactly one stem to cache
    state.current_bpm = 128
    state.current_key = "A minor"

    resp = _add_only_response()
    _wire_loop_no_io(loop, monkeypatch, response=resp)
    submit = AsyncMock(return_value=uuid.uuid4())
    loop._submit_job = submit

    # Pre-seed the cache with the EXACT key the loop computes for the add stem
    # (model_id_prompt_bpm_key_bars), so P7's cache-HIT `continue` fires.
    add_track = process_actions(resp["actions"], [])[0]
    prompt = loop._build_prompt(add_track, "A minor", 128)
    cache_key = f"foundation-1_{prompt}_128_A minor_4"
    loop.stem_cache[cache_key] = {"audio_data": np.ones((10, 2), dtype=np.float32), "last_used": 0.0}

    await asyncio.wait_for(loop._run_loop(), timeout=5.0)
    submit.assert_not_called()


async def test_d0_p8_foreground_fetch_routes_through_cache_stem(monkeypatch):
    """Foreground _run_loop calls state.cache_stem when a stem's audio is fetched
    (the divergence complement to the pregen path, which never calls it)."""
    import app.framework.loop_steps as steps

    loop = AsyncFrameworkLoop(uuid.uuid4())
    _seed_loop_for_run(loop, current_sample=0, stop_on=("add", 1))
    state.active_stems = []  # add-only -> one submitted stem
    state.current_bpm = 128
    state.current_key = "A minor"

    job_id = uuid.uuid4()
    _wire_loop_no_io(loop, monkeypatch, response=_add_only_response())
    # Override the no-IO wiring so a real audio path runs end-to-end.
    loop._submit_job = AsyncMock(return_value=job_id)
    loop._fetch_audio = AsyncMock(return_value=np.ones((100, 2), dtype=np.float32))
    monkeypatch.setattr(steps, "wait_for_multiple_jobs", AsyncMock(return_value={job_id: "audio/x.aac"}))

    with patch.object(state, "cache_stem") as cs:
        await asyncio.wait_for(loop._run_loop(), timeout=5.0)
    assert cs.called, "foreground _run_loop must route fetched audio through state.cache_stem"


# --------------------------------------------------------------------------- #
# P13 transition-recording — _step_await_pregen dead-code branches
# (refactor/decomp/04_adversarial.md UNTESTED-PATHS, pre-existing gap)
# --------------------------------------------------------------------------- #


async def test_step_await_pregen_records_transition_from_mixer_event(monkeypatch):
    """Branch 1 (loop_steps.py:670-678): when pop_transition_event() returns a
    positive loop idx, record_loop_transition IS called with that idx + the
    snapshot of active_stems / current_set_name / llm_reasoning.

    Drives _step_await_pregen directly with a fake mixer whose
    pop_transition_event returns 2 once. current_ahead is kept >= 0.5 so the
    inner while iterates past the transition recording. _pregen_done is never
    set (proves the recording fires on a real await-spin iteration, not a
    pre-gen-done fast-path).
    """
    loop = AsyncFrameworkLoop(uuid.uuid4())
    mixer = _FakeMixer(current_sample=0, transition_events=[2])
    mixer.current_loop_end_sample = 44100  # current_ahead = 44100/44100 = 1.0s
    mixer._loop = loop
    loop.mixer = mixer
    loop.running = True
    loop._loop_idx = 1
    loop._pregen_done = asyncio.Event()  # unset -> enters while body

    state.active_stems = [_active_stem_for_retain()]
    state.current_set_name = "Test Set"
    state.llm_reasoning = "transition reasoning"

    record_calls = []
    monkeypatch.setattr(
        state,
        "record_loop_transition",
        lambda idx, stems, set_name, reason: record_calls.append((idx, list(stems), set_name, reason)),
    )
    _patch_sleep_instant(monkeypatch)

    await asyncio.wait_for(loop._step_await_pregen(), timeout=5.0)

    # WIRING: the idx from pop_transition_event flows through to the record call
    assert len(record_calls) == 1
    assert record_calls[0][0] == 2  # transitioned_loop_idx
    assert record_calls[0][1] == list(state.active_stems)  # t_stems snapshot
    assert record_calls[0][2] == "Test Set"  # t_set = current_set_name
    assert record_calls[0][3] == "transition reasoning"  # t_reason = llm_reasoning
    # Proves >= 2 iterations (transition consumed + exhausted flip)
    assert mixer.pop_calls >= 2
    # Loop terminated via running-flip, not pre-gen-done
    assert not loop._pregen_done.is_set()


async def test_step_await_pregen_breaks_when_current_ahead_below_half_second(monkeypatch):
    """Branch 2 (loop_steps.py:695-697): when the mixer is < 0.5s from its loop
    boundary, the await-spin breaks immediately (to avoid missing a transition).

    current_loop_end_sample == current_sample == 0 → current_ahead = 0.0 < 0.5.
    _pregen_done is unset (proves the break is from current_ahead, not pre-gen).
    No transition event is queued, so record_loop_transition is NOT called.
    """
    loop = AsyncFrameworkLoop(uuid.uuid4())
    mixer = _FakeMixer(current_sample=0)  # transition_events=None (default)
    mixer.current_loop_end_sample = 0  # current_ahead = (0-0)/44100 = 0.0
    mixer._loop = loop
    loop.mixer = mixer
    loop.running = True
    loop._loop_idx = 1
    loop._pregen_done = asyncio.Event()  # unset -> proves break is NOT from pre-gen

    record_calls = []
    monkeypatch.setattr(
        state,
        "record_loop_transition",
        lambda idx, stems, set_name, reason: record_calls.append((idx, list(stems), set_name, reason)),
    )
    _patch_sleep_instant(monkeypatch)

    await asyncio.wait_for(loop._step_await_pregen(), timeout=5.0)

    # Broke on the FIRST iteration via current_ahead < 0.5
    assert mixer.pop_calls == 1  # exactly one pop → one iteration
    assert record_calls == []  # no transition recorded
    assert not loop._pregen_done.is_set()  # break was NOT from pre-gen-done
    assert loop.running  # running still True (break is local)
