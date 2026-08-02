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
import os
from typing import Any

from app.framework.framework_state import state

_MODELS_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config", "models_config.json")


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


def process_actions(actions: list[dict[str, Any]], active_stems: list[dict]) -> list[dict]:
    """Process Conductor DJ actions and return a deduplicated track list.

    Actions:
    - retain: keep stem, ``_age+1`` (IN-PLACE mutation of ``_original_details``)
    - add:    new stem with ``_age=0``
    - remove: stem is excluded

    Dedup key: ``model_id_major_family_sub_family_timbre_tags_notation_tag_fx_tag``.

    NOTE: retain mutates ``active_stems[idx]["_original_details"]["_age"]`` on the
    LIVE input list (brief-01 risk #1). Callers must pass the same list the loop
    will later commit — do NOT pass a defensive copy or ``_age`` accounting breaks.
    """
    new_tracks: list[dict] = []

    for action in actions:
        a_type = action.get("action_type")
        idx = action.get("stem_index")

        if a_type == "retain" and idx is not None and 0 <= idx < len(active_stems):
            s = active_stems[idx]
            orig = s.get("_original_details", {})
            orig["_age"] = s.get("_age", 0) + 1
            new_tracks.append(orig)

        elif a_type == "add":
            major = action.get("major_family", "Synth")
            sub = action.get("sub_family", "Synth Lead")
            new_tracks.append(
                {
                    "model_id": action.get("model_id"),
                    "major_family": major,
                    "sub_family": sub,
                    "timbre_tags": action.get("timbre_tags", ["Warm"]),
                    "notation_tag": action.get("notation_tag", "melody"),
                    "fx_tag": action.get("fx_tag", "Medium Reverb"),
                    "bars": action.get("bars", 4),
                    "_age": 0,
                }
            )

        elif a_type == "remove" and idx is not None and 0 <= idx < len(active_stems):
            # Remove: excluded from new_tracks (no action needed).
            pass

    # Deduplicate.
    unique_tracks: dict[str, dict] = {}
    for t in new_tracks:
        if not t:
            continue
        m_id = t.get("model_id", "default")
        t_key = (
            f"{m_id}_{t.get('major_family')}_{t.get('sub_family')}_"
            f"{'_'.join(t.get('timbre_tags', []))}_{t.get('notation_tag')}_"
            f"{t.get('fx_tag')}"
        )
        if t_key not in unique_tracks:
            unique_tracks[t_key] = t

    return list(unique_tracks.values())


def format_action_log(actions: list[dict[str, Any]], stems: list[dict]) -> list[str]:
    """Build the human-readable Retained/Added/Removed audit log for a loop.

    Shared by the fresh path (``_step_parse_actions`` over ``active_stems``) and
    the pregen path (``_step_commit_state`` over ``state.previous_stems``) so the
    two near-identical loops can never drift. Pure: takes the action list and the
    stem list to resolve indices against, returns the log lines.

    >>> format_action_log(
    ...     [{"action_type": "add", "sub_family": "Pad"}], []
    ... )
    ['Added Pad']
    """
    log: list[str] = []
    for action in actions:
        a_type = action.get("action_type")
        idx = action.get("stem_index")
        if a_type == "retain" and idx is not None and 0 <= idx < len(stems):
            prompt = stems[idx].get("prompt", "")
            prompt_part = prompt.split(",")[1].strip() if len(prompt.split(",")) > 1 else prompt
            log.append(f"Retained {prompt_part}")
        elif a_type == "add":
            log.append(f"Added {action.get('sub_family', '')}")
        elif a_type == "remove" and idx is not None and 0 <= idx < len(stems):
            prompt = stems[idx].get("prompt", "")
            prompt_part = prompt.split(",")[1].strip() if len(prompt.split(",")) > 1 else prompt
            log.append(f"Removed {prompt_part}")
    return log
