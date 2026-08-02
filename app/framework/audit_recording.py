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


async def flush_recording_buffers() -> None:
    """Batch-write buffered interactions/actions to DB; re-queue on failure."""
    async with _flush_lock:
        async with state.lock:
            if not state.llm_interaction_buffer and not state.action_buffer:
                return
            if state.current_show_id is None:
                state.llm_interaction_buffer.clear()
                state.action_buffer.clear()
                return

            # Copy buffers under lock, then release lock before DB I/O.
            llm_buffer = state.llm_interaction_buffer[:]
            action_buffer = state.action_buffer[:]
            state.llm_interaction_buffer.clear()
            state.action_buffer.clear()

        if not llm_buffer and not action_buffer:
            return

        # Import here to avoid circular imports.
        from app.db import DatabaseManager
        from app.models import LLMInteraction, ShowAction

        db_manager = DatabaseManager.get_instance()
        try:
            with db_manager.session() as session:
                if llm_buffer:
                    session.bulk_insert_mappings(LLMInteraction, llm_buffer)
                if action_buffer:
                    session.bulk_insert_mappings(ShowAction, action_buffer)
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
        "action_description": _audit_action_description(a_type, idx, action, active_stems),
    }


def _audit_loop_meta(conductor_response: dict[str, Any], active_stems: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive the per-loop conductor context stored on an LLMInteraction row.

    bpm/key/set_name come straight from conductor_response; instruments is the
    active stem names; action_type is a single rollup (add > remove > retain)
    since one loop carries N actions but one interaction row.
    """
    action_types = {a.get("action") for a in (conductor_response.get("actions") or [])}
    return {
        "bpm": conductor_response.get("master_bpm"),
        "key": conductor_response.get("master_key"),
        "set_name": conductor_response.get("name"),
        "instruments": [s.get("instrument") for s in active_stems if s.get("instrument")],
        "action_type": next((t for t in ("add", "remove", "retain") if t in action_types), None),
    }


async def append_loop_audit(conductor_response, active_stems, loop_idx) -> None:
    """Buffer one LLMInteraction + N ShowAction rows for later DB flush (C1).

    No-op when no show is recording. Runs under ``state.lock`` so it cannot
    interleave with ``flush_recording_buffers``.
    """
    actions = conductor_response.get("actions", []) or []
    reasoning = conductor_response.get("reasoning", "")
    is_fallback = conductor_response.get("name") == "Fallback State"
    now = datetime.now(timezone.utc)
    async with state.lock:
        show_id = state.current_show_id
        if show_id is None:
            return
        relative_ms = _relative_show_ms()
        state.llm_interaction_buffer.append(
            {
                "show_id": show_id,
                "loop_index": loop_idx,
                "timestamp": now,
                "relative_time_ms": relative_ms,
                "prompt_messages": _audit_prompt_context(conductor_response, active_stems, loop_idx),
                "parsed_response": conductor_response,
                "reasoning": reasoning,
                "error": None,
                "was_fallback": is_fallback,
                **_audit_loop_meta(conductor_response, active_stems),
            }
        )
        for action in actions:
            state.action_buffer.append(_audit_action_row(show_id, loop_idx, now, relative_ms, action, active_stems))
