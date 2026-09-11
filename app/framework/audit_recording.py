"""Show-audit persistence for the framework loop (C1/B13).

Lifted out of ``framework_main_async.py`` (Phase 3 of the E1-E6 refactor). Owns
the two things that touch the audit trail: buffering one loop's LLM interaction
+ actions (``append_loop_audit``) and the bulk flush to Postgres
(``flush_recording_buffers``), plus the pure row-shaping helpers.

All functions read the shared ``state`` singleton directly (show id, buffers,
``current_show_start_time``) and serialize the flush with the module-level
``_flush_lock``. The orchestrator keeps calling ``AsyncFrameworkLoop._append_loop_audit``
as a thin delegate so existing ``patch.object(loop, '_append_loop_audit')``
patches and direct test calls keep working (brief-02 ssD).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from app.framework.framework_state import state

# Serializes overlapping flush_recording_buffers calls (B13). Module-level so the
# lock IDENTITY is shared with callers that import it (tests, routes/shows.py).
_flush_lock = asyncio.Lock()

# U4 (REL-04): best-effort bound for the lifespan-shutdown flush (app_ui.py).
# A DB unreachable within this window costs at most the unflushed tail (<= the
# P12 threshold + one loop's rows) — never a hung shutdown. Module-level so
# tests can pin/monkeypatch it.
FLUSH_SHUTDOWN_FLUSH_TIMEOUT_SECONDS = 10.0


def _insert_audit_batches(llm_buffer: list[dict[str, Any]], action_buffer: list[dict[str, Any]]) -> None:
    """Sync bulk-insert of one flush's copied buffers (U4: runs in a worker thread).

    Raises on DB failure so ``flush_recording_buffers`` can re-queue the rows.
    """
    # Import here to avoid circular imports.
    from app.db import DatabaseManager
    from app.models import LLMInteraction, ShowAction

    db_manager = DatabaseManager.get_instance()
    with db_manager.session() as session:
        if llm_buffer:
            session.bulk_insert_mappings(LLMInteraction, llm_buffer)
        if action_buffer:
            session.bulk_insert_mappings(ShowAction, action_buffer)


async def flush_recording_buffers() -> None:
    """Batch-write buffered interactions/actions to DB; re-queue on failure.

    U4 (REL-04): the flush is now on the loop hot path (P12 threshold), so the
    DB I/O runs in a worker thread (``asyncio.to_thread``) — sync SQLAlchemy on
    the event loop would stall every route/WS for the duration of a slow DB
    (REL-09 class). Semantics unchanged for every caller: same copy-under-lock,
    same ``_flush_lock`` serialization, same re-prepend-on-failure. Test fakes
    patching ``DatabaseManager.get_instance`` keep working (module attr is
    resolved at call time, thread or no thread).
    """
    async with _flush_lock:
        async with state.lock:
            if not state.llm_interaction_buffer and not state.action_buffer:
                return

            # Buffered rows carry their own show_id, so they are persisted even
            # when current_show_id was already cleared by the caller. The old
            # discard-on-no-show branch deleted a whole show's audit trail here:
            # stop_show cleared the flag (shows.py _stop_show_recording) BEFORE
            # calling this flush, so every buffered row was dropped unflushed
            # (review DATA-2/CONC-1 — show_actions/llm_interactions stayed empty).

            # Copy buffers under lock, then release lock before DB I/O.
            llm_buffer = state.llm_interaction_buffer[:]
            action_buffer = state.action_buffer[:]
            state.llm_interaction_buffer.clear()
            state.action_buffer.clear()

        if not llm_buffer and not action_buffer:
            return

        try:
            await asyncio.to_thread(_insert_audit_batches, llm_buffer, action_buffer)
            print("Flushed recording buffers to DB")
        except Exception as e:  # noqa: BLE001  # intentional: restore buffers + keep the show alive on DB blip
            print(f"Error flushing recording buffers: {e}")
            # Put buffers back on failure.
            async with state.lock:
                state.llm_interaction_buffer = llm_buffer + state.llm_interaction_buffer
                state.action_buffer = action_buffer + state.action_buffer


def _relative_show_ms() -> int:
    """Milliseconds since the current show started (0 if not started)."""
    start = state.current_show_start_time
    return int((time.time() - start) * 1000) if start else 0


async def drop_buffered_rows_for_show(show_id: int) -> int:
    """Deliberately drop buffered audit rows referencing a deleted show (REL-14).

    The show row (and its audit history, by cascade) is gone, so these rows can
    never insert — they would FK-fail every future flush, and the failed batch
    re-prepends, poisoning flushes until restart. Caller logs the count
    (invariant 4: the drop must be loud, never silent). Lock section is pure
    list filtering — no I/O.

    Example::

        dropped = await drop_buffered_rows_for_show(show_id)  # -> 4
    """
    async with state.lock:
        llm_keep = [r for r in state.llm_interaction_buffer if r.get("show_id") != show_id]
        act_keep = [r for r in state.action_buffer if r.get("show_id") != show_id]
        dropped = (len(state.llm_interaction_buffer) - len(llm_keep)) + (
            len(state.action_buffer) - len(act_keep)
        )
        state.llm_interaction_buffer = llm_keep
        state.action_buffer = act_keep
    return dropped


def _audit_applied_actions(next_stems: list[dict[str, Any]], outcomes: dict[int, str]) -> list[dict[str, Any]]:
    """Build the per-stem U4 capture rows for the stems ACTUALLY enacted this loop.

    ``next_stems`` is the post-dedupe set (process_actions output), so this is
    what played — distinct from ``parsed_response.actions`` (what was requested).
    ``outcomes`` maps orig_idx -> "generated" | "failed" (P8 result); stems
    absent from the map were cache hits -> "cached".
    """
    rows: list[dict[str, Any]] = []
    for i, stem in enumerate(next_stems):
        details = stem.get("_original_details", {}) or {}
        rows.append(
            {
                "sub_family": details.get("sub_family"),
                "major_family": details.get("major_family"),
                "model_id": stem.get("model_id", details.get("model_id")),
                "bars": stem.get("bars", details.get("bars")),
                "age": stem.get("_age", details.get("_age")),
                "outcome": outcomes.get(i, "cached"),
            }
        )
    return rows


def _audit_prompt_context(
    conductor_response: dict[str, Any], active_stems: list[dict[str, Any]], loop_idx: int
) -> dict[str, Any]:
    """Summarize the request context (actual chat msgs live in the conductor)."""
    return {
        "loop_index": loop_idx,
        "bpm": conductor_response.get("master_bpm"),
        "key": conductor_response.get("master_key"),
        "set_name": conductor_response.get("name"),
        "active_stem_count": len(active_stems),
        "note": "request context; full prompt built in ConductorLLMAsync",
    }


def _audit_stem_details(a_type, idx, action, active_stems) -> dict[str, Any]:
    """Build a JSON-safe stem descriptor for stem_details."""
    if a_type == "add":
        return {
            "index": idx,
            "instrument": action.get("sub_family"),
            "major_family": action.get("major_family"),
            "sub_family": action.get("sub_family"),
            "model_id": action.get("model_id"),
            "bars": action.get("bars"),
        }
    stem = active_stems[idx] if idx is not None and 0 <= idx < len(active_stems) else {}
    return {
        "index": idx,
        "instrument": stem.get("instrument") or stem.get("prompt", ""),
        "prompt": stem.get("prompt", ""),
        "model_id": stem.get("model_id"),
        "bpm": stem.get("bpm"),
        "key": stem.get("key"),
        "bars": stem.get("bars"),
    }


def _audit_action_description(a_type, idx, action, active_stems) -> str:
    """Human-readable one-liner for action_description."""
    if a_type == "add":
        return f"Added {action.get('sub_family', action.get('major_family', 'stem'))}"
    stem = active_stems[idx] if idx is not None and 0 <= idx < len(active_stems) else {}
    label = stem.get("instrument") or stem.get("prompt", f"stem {idx}")
    if a_type == "retain":
        return f"Retained {label}"
    if a_type == "remove":
        return f"Removed {label}"
    return f"{a_type or 'Unknown'} {label}"


def _audit_action_row(show_id, loop_idx, ts, relative_ms, action, active_stems) -> dict[str, Any]:
    """Shape one action dict for bulk-insert into show_actions."""
    a_type = action.get("action_type")
    idx = action.get("stem_index")
    return {
        "show_id": show_id,
        "loop_index": loop_idx,
        "timestamp": ts,
        "relative_time_ms": relative_ms,
        "action_type": a_type,
        "stem_index": idx,
        "stem_details": _audit_stem_details(a_type, idx, action, active_stems),
        # Clamped to the String(500) column: one oversized LLM free-text row
        # used to raise StringDataRightTruncation for the whole bulk insert,
        # and the failed batch was re-prepended, so every later flush failed
        # identically and the entire audit trail was lost (review DATA-6).
        "action_description": _audit_action_description(a_type, idx, action, active_stems)[:500],
    }


def _audit_loop_meta(conductor_response: dict[str, Any], active_stems: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive the per-loop conductor context stored on an LLMInteraction row.

    bpm/key/set_name come straight from conductor_response; instruments is the
    active stem names; action_type is a single rollup (add > remove > retain)
    since one loop carries N actions but one interaction row.
    """
    # The conductor's strict json_schema only emits 'action_type' (see
    # app/lib/constants.py action schemas) — reading 'action' yielded {None}
    # for every loop, so llm_interactions.action_type was always NULL (DATA-3).
    action_types = {a.get("action_type") for a in (conductor_response.get("actions") or [])}
    return {
        "bpm": conductor_response.get("master_bpm"),
        "key": conductor_response.get("master_key"),
        "set_name": conductor_response.get("name"),
        "instruments": [s.get("instrument") for s in active_stems if s.get("instrument")],
        "action_type": next((t for t in ("add", "remove", "retain") if t in action_types), None),
    }


def _audit_interaction_row(
    show_id: int,
    loop_idx: int,
    ts: datetime,
    relative_ms: int,
    conductor_response: dict[str, Any],
    active_stems: list[dict[str, Any]],
) -> dict[str, Any]:
    """Shape one LLMInteraction dict for bulk-insert, incl. the U4 capture fields.

    Transport keys (underscore-prefixed, see the ports.py convention) are
    persisted in their dedicated columns and stripped from parsed_response so
    the stored response stays schema-pure model output. Rows without them
    (fallback paths, test fakes) keep the legacy context-summary prompt.
    """
    request_messages = conductor_response.get("_request_messages")
    applied = conductor_response.get("_applied_actions")
    parsed = {k: v for k, v in conductor_response.items() if not k.startswith("_")}
    # Clamped to the String(1000) column — see DATA-6 note in _audit_action_row.
    reasoning = (conductor_response.get("reasoning") or "")[:1000]
    return {
        "show_id": show_id,
        "loop_index": loop_idx,
        "timestamp": ts,
        "relative_time_ms": relative_ms,
        # U4: the exact system+user chat when the conductor attached it; the
        # legacy 5-key context summary stays as the fallback-path shape.
        "prompt_messages": request_messages
        if request_messages
        else _audit_prompt_context(conductor_response, active_stems, loop_idx),
        "parsed_response": parsed,
        # U4 (REL-04 + DPO field audit): the post-dedupe enacted stems + their
        # generation outcome; additive column, see migrations/003.
        "applied_actions": applied,
        "reasoning": reasoning,
        "error": None,
        "was_fallback": conductor_response.get("name") == "Fallback State",
        **_audit_loop_meta(conductor_response, active_stems),
    }


async def append_loop_audit(conductor_response, active_stems, loop_idx) -> None:
    """Buffer one LLMInteraction + N ShowAction rows for later DB flush (C1).

    No-op when no show is recording. Runs under ``state.lock`` so it cannot
    interleave with ``flush_recording_buffers``.
    """
    actions = conductor_response.get("actions", []) or []
    now = datetime.now(timezone.utc)
    async with state.lock:
        show_id = state.current_show_id
        if show_id is None:
            return
        relative_ms = _relative_show_ms()
        state.llm_interaction_buffer.append(
            _audit_interaction_row(show_id, loop_idx, now, relative_ms, conductor_response, active_stems)
        )
        for action in actions:
            state.action_buffer.append(_audit_action_row(show_id, loop_idx, now, relative_ms, action, active_stems))


class AuditAdapter:
    """Postgres audit-trail adapter: wraps the module append/flush functions.

    The only production ``AuditSinkPort`` implementation. Construction is a
    no-op (the DB session opens lazily inside ``flush_recording_buffers`` at
    call time), so a default ``AuditAdapter()`` may be eagerly stored in
    ``AsyncFrameworkLoop.__init__`` without touching the DB or acquiring locks.

    ``append_loop`` delegates to the module ``append_loop_audit`` (note the
    method-vs-module name skew: the port's ``append_loop`` maps to the module
    ``append_loop_audit``). ``flush`` delegates to ``flush_recording_buffers``.
    The adapter takes NO lock of its own — the module functions already own the
    ``_flush_lock`` + ``state.lock`` semantics (B13). Delegation is safe
    precisely because the lock lives in the module functions, not the adapter.

    Only ``append_loop`` used to be wired into the loop; U4 (REL-04) also wires
    ``flush`` — the loop calls it from P12 past ``AUDIT_FLUSH_THRESHOLD_ROWS``
    (loop_steps.py) while routes keep the stop/shutdown flushes. Both paths
    serialize on the shared module ``_flush_lock``, so ownership stays dual but
    lock-serialized. The adapter takes NO lock of its own — the module functions
    already own the ``_flush_lock`` + ``state.lock`` semantics (B13). Delegation
    is safe precisely because the lock lives in the module functions, not the
    adapter.
    """

    async def append_loop(
        self,
        conductor_response: dict[str, Any],
        active_stems: list[dict[str, Any]],
        loop_idx: int,
    ) -> None:
        """Buffer one loop's LLM interaction + per-action rows."""
        await append_loop_audit(conductor_response, active_stems, loop_idx)

    async def flush(self) -> None:
        """Bulk-insert buffered rows; re-queue on failure."""
        await flush_recording_buffers()
