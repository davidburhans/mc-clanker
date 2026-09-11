"""Conductor prompt/action shaping for the framework loop (Phase 4).

Pure-ish helpers that translate between Conductor decisions and the track
representation. Lifted out of ``framework_main_async.py`` so both the foreground
``_run_loop`` and the background ``_pre_generate_next_loop`` share ONE copy of:
- ``load_available_models``  — read generator + models_config.json (was duplicated inline)
- ``build_fallback_response`` — the retain-all fallback dict (was duplicated inline)
- ``build_track_prompt``     — format a track dict via the engine prompt_template
- ``process_actions``        — retain/add/remove + dedup (frozen public API)

``process_actions`` mutates ``active_stems[idx]["_original_details"]["_age"]`` in
place on purpose (brief-01 risk #1) — callers must pass the LIVE list, not a copy.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from app.framework.framework_state import state

log = logging.getLogger(__name__)

_MODELS_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config", "models_config.json")

# Round-3 fix C2/C3 (review 08 §2 + §4): the ONLY action types the shaper understands.
# Anything else used to be dropped in total silence, which — because `remove` is
# implemented as *absence* — silently emptied the mix.
VALID_ACTION_TYPES: frozenset[str] = frozenset({"retain", "add", "remove"})

# Defaults applied to a malformed/`null` add payload (review 08 §4: the system prompt
# itself invites `null` instrument fields, and `.get(k, default)` does not fire on
# present-null).
DEFAULT_MAJOR_FAMILY = "Synth"
DEFAULT_SUB_FAMILY = "Synth Lead"
DEFAULT_TIMBRE_TAGS: list[str] = ["Warm"]
DEFAULT_NOTATION_TAG = "melody"
DEFAULT_FX_TAG = "Medium Reverb"
DEFAULT_BARS = 4


def load_available_models() -> list[dict[str, Any]]:
    """Build the available-models descriptor list from the generator + config.

    Returns ``[]`` when there is no generator or no config file (matches the old
    inline block, which left ``available_models`` empty in both cases).
    """
    generator = getattr(state, "generator", None)
    if not (generator and hasattr(generator, "models")):
        return []
    if not os.path.exists(_MODELS_CONFIG_PATH):
        return []
    try:
        with open(_MODELS_CONFIG_PATH) as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        # Unreadable / malformed config degrades to "no models", matching the
        # missing-file branch above (the LLM still runs, just without descriptions).
        return []
    models: list[dict[str, Any]] = []
    for model_id in generator.models:
        m_info = cfg.get("models", {}).get(model_id, {})
        models.append(
            {
                "id": model_id,
                "description": m_info.get("description", "No description"),
                "supported_families": m_info.get("supported_families", ["Any"]),
            }
        )
    return models


def build_fallback_response(
    current_bpm: int, current_key: str, active_stems: list[dict], error: object
) -> dict[str, Any]:
    """Retain-all fallback used when the LLM call raises (name == 'Fallback State')."""
    return {
        "master_bpm": current_bpm,
        "master_key": current_key,
        "actions": [{"action_type": "retain", "stem_index": i} for i in range(len(active_stems))],
        "reasoning": f"LLM failed ({error}). Retaining current groove.",
        "name": "Fallback State",
    }


def build_track_prompt(track: dict[str, Any], key: str, bpm: int) -> str:
    """Build a generation prompt from track details via the engine prompt_template."""
    generator = getattr(state, "generator", None)
    m_id = track.get("model_id", "foundation-1")

    if generator and m_id in generator.models:
        engine = generator.models[m_id]
        prompt_template = getattr(engine, "prompt_template", None)
    else:
        prompt_template = None

    if not prompt_template:
        prompt_template = (
            "{major_family}, {sub_family}, {timbre_tags}, {notation_tag}, {fx_tag}, {key}, {bpm} BPM, {bars} Bars"
        )

    major = track.get("major_family", "Synth")
    sub = track.get("sub_family", "Synth Lead")
    timbres = " ".join(track.get("timbre_tags", ["Warm"]))
    notation = track.get("notation_tag", "melody")
    fx = track.get("fx_tag", "Medium Reverb")
    bars = track.get("bars", 8)

    return prompt_template.format(
        major_family=major,
        sub_family=sub,
        timbre_tags=timbres,
        notation_tag=notation,
        fx_tag=fx,
        key=key,
        bpm=bpm,
        bars=bars,
    )


def _coerce_stem_index(raw: Any) -> int | None:
    """Coerce a Conductor ``stem_index`` to ``int``, or ``None`` when unusable.

    Non-``response_format`` backends emit ``"1"`` / ``1.0`` for indices routinely;
    those are coerced. ``bool`` and anything non-integral is rejected.

    >>> _coerce_stem_index("1"), _coerce_stem_index(2.0), _coerce_stem_index(None)
    (1, 2, None)
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() else None
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return None
    return None


def _text_field(action: dict[str, Any], field: str, default: str) -> str:
    """Read a non-blank string field, falling back to ``default`` on null/junk."""
    value = action.get(field)
    if isinstance(value, str) and value.strip():
        return value
    return default


def _tag_list(raw: Any, default: list[str]) -> list[str]:
    """Normalize ``timbre_tags`` to a list of strings (never raises on null/junk)."""
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple)) and raw:
        return [str(tag) for tag in raw]
    return list(default)


def _warn_rejected_action(position: int, action: Any, reason: str) -> None:
    keys = sorted(action) if isinstance(action, dict) else None
    log.warning(
        {
            "event": "conductor_action_rejected",
            "position": position,
            "reason": reason,
            "action_type": action.get("action_type") if keys else None,
            "keys": keys,
            "type": type(action).__name__,
        }
    )


def _validated_actions(actions: Any, *, context: str) -> list[dict[str, Any]]:
    """Keep only well-formed actions, warning about every rejected one.

    Round-3 fix C2/C3 (review 08 §2 + §4): malformed elements (non-dict, unknown
    ``action_type``, non-integral ``stem_index``) used to either raise out of P5 —
    wedging the loop in the B1 hot retry — or vanish without a log line.
    """
    if not isinstance(actions, list):
        log.warning({"event": "conductor_actions_not_a_list", "context": context, "type": type(actions).__name__})
        return []
    valid: list[dict[str, Any]] = []
    for position, action in enumerate(actions):
        if not isinstance(action, dict):
            _warn_rejected_action(position, action, "not-an-object")
            continue
        a_type = action.get("action_type")
        if a_type not in VALID_ACTION_TYPES:
            _warn_rejected_action(position, action, "unknown-action-type")
            continue
        if a_type == "add":
            valid.append(action)
            continue
        idx = _coerce_stem_index(action.get("stem_index"))
        if idx is None:
            _warn_rejected_action(position, action, "invalid-stem-index")
            continue
        valid.append({**action, "stem_index": idx})
    return valid


def _removed_indices(valid_actions: list[dict[str, Any]]) -> set[int]:
    """Indices an explicit ``remove`` targets — remove wins over retain (review 08 §5)."""
    return {a["stem_index"] for a in valid_actions if a["action_type"] == "remove"}


def _retained_track(active_stems: list[dict], idx: int) -> dict[str, Any]:
    """Age ``active_stems[idx]`` IN PLACE and return its original details."""
    s = active_stems[idx]
    orig = s.get("_original_details", {})
    orig["_age"] = s.get("_age", 0) + 1
    return orig


def _added_track(action: dict[str, Any]) -> dict[str, Any]:
    """Build the next-loop track for an ``add`` action, null/junk fields defaulted."""
    return {
        "model_id": action.get("model_id"),
        "major_family": _text_field(action, "major_family", DEFAULT_MAJOR_FAMILY),
        "sub_family": _text_field(action, "sub_family", DEFAULT_SUB_FAMILY),
        "timbre_tags": _tag_list(action.get("timbre_tags"), DEFAULT_TIMBRE_TAGS),
        "notation_tag": _text_field(action, "notation_tag", DEFAULT_NOTATION_TAG),
        "fx_tag": _text_field(action, "fx_tag", DEFAULT_FX_TAG),
        "bars": action.get("bars") or DEFAULT_BARS,
        "_age": 0,
    }


def _track_dedup_key(track: dict[str, Any]) -> str:
    timbres = "_".join(_tag_list(track.get("timbre_tags"), []))
    return (
        f"{track.get('model_id', 'default')}_{track.get('major_family')}_"
        f"{track.get('sub_family')}_{timbres}_{track.get('notation_tag')}_{track.get('fx_tag')}"
    )


def _dedupe(tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique_tracks: dict[str, dict] = {}
    for t in tracks:
        if not t:
            continue
        t_key = _track_dedup_key(t)
        if t_key not in unique_tracks:
            unique_tracks[t_key] = t
    return list(unique_tracks.values())


def process_actions(actions: list[dict[str, Any]], active_stems: list[dict]) -> list[dict]:
    """Process Conductor DJ actions and return a deduplicated track list.

    Actions:
    - retain: keep stem, ``_age+1`` (IN-PLACE mutation of ``_original_details``)
    - add:    new stem with ``_age=0``
    - remove: stem is excluded

    Dedup key: ``model_id_major_family_sub_family_timbre_tags_notation_tag_fx_tag``.

    Round-3 hardening (review 08 §2/§4/§5):
    * malformed actions are skipped WITH a warning instead of raising or vanishing;
    * ``remove`` WINS over a ``retain`` of the same index (audit and audio agree);
    * a decision that yields nothing while stems are playing degrades to retain-all
      instead of committing a silent, empty loop (an explicit ``remove`` of every
      playing stem is still honored).

    NOTE: retain mutates ``active_stems[idx]["_original_details"]["_age"]`` on the
    LIVE input list (brief-01 risk #1). Callers must pass the same list the loop
    will later commit — do NOT pass a defensive copy or ``_age`` accounting breaks.
    """
    valid_actions = _validated_actions(actions, context="process_actions")
    removed = _removed_indices(valid_actions)

    new_tracks: list[dict] = []
    for action in valid_actions:
        a_type = action["action_type"]

        if a_type == "retain":
            idx = action["stem_index"]
            if idx in removed:
                log.warning(
                    {
                        "event": "conductor_retain_remove_conflict",
                        "stem_index": idx,
                        "resolution": "remove-wins",
                    }
                )
                continue
            if 0 <= idx < len(active_stems):
                new_tracks.append(_retained_track(active_stems, idx))

        elif a_type == "add":
            new_tracks.append(_added_track(action))
        # "remove" is exclusion: the index is simply never appended (see `removed`).

    unique_tracks = _dedupe(new_tracks)
    # C2 (review 08 §2): an empty decision must never mean "remove everything" — the
    # only way to empty the mix is an explicit `remove` of every stem still playing.
    still_playable = [i for i in range(len(active_stems)) if i not in removed]
    if not unique_tracks and still_playable:
        log.warning(
            {
                "event": "conductor_decision_left_no_stems",
                "actions_received": len(actions) if isinstance(actions, list) else 0,
                "active_stems": len(active_stems),
                "resolution": "retain-all",
            }
        )
        unique_tracks = _dedupe([_retained_track(active_stems, i) for i in still_playable])

    return unique_tracks


def format_action_log(actions: list[dict[str, Any]], stems: list[dict]) -> list[str]:
    """Build the human-readable Retained/Added/Removed audit log for a loop.

    Shared by the fresh path (``_step_parse_actions`` over ``active_stems``) and
    the pregen path (``_step_commit_state`` over ``state.previous_stems``) so the
    two near-identical loops can never drift. Pure: takes the action list and the
    stem list to resolve indices against, returns the log lines.

    Applies the SAME validation and remove-wins semantics as ``process_actions``, so
    the audit can never describe a mix that did not play (review 08 §5) and junk
    actions can never raise out of the loop (review 08 §4).

    >>> format_action_log(
    ...     [{"action_type": "add", "sub_family": "Pad"}], []
    ... )
    ['Added Pad']
    """
    lines: list[str] = []
    valid_actions = _validated_actions(actions, context="format_action_log")
    removed = _removed_indices(valid_actions)

    for action in valid_actions:
        a_type = action["action_type"]
        if a_type == "add":
            lines.append(f"Added {_text_field(action, 'sub_family', DEFAULT_SUB_FAMILY)}")
            continue
        idx = action["stem_index"]
        if a_type == "retain" and idx in removed:
            continue  # remove won — the stem is only reported as Removed
        if not 0 <= idx < len(stems):
            continue
        prompt = stems[idx].get("prompt", "")
        parts = prompt.split(",")
        prompt_part = parts[1].strip() if len(parts) > 1 else prompt
        lines.append(f"Retained {prompt_part}" if a_type == "retain" else f"Removed {prompt_part}")
    return lines
