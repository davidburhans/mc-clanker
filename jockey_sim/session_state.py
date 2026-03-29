"""SessionState — isolated per-jockey DJ session state.

Mirrors the state fields used by framework_main_async.py:_run_loop().
Each jockey owns one SessionState; no shared state, no locks needed.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from typing import Any

from framework_main_async import process_actions
from framework_state import DEFAULT_INSTRUMENTS
from slop_harness.models import BPMS_ALL, KEYS_ALL


MAX_HISTORY = 8


def _flatten_instruments() -> list[str]:
    flat = []
    for cat, items in DEFAULT_INSTRUMENTS.items():
        flat.extend(items)
    return flat


def _select_instruments_for_jockey(rng: random.Random) -> list[str]:
    """Select available instruments for a jockey.

    75% of jockeys get all instruments (full diversity).
    25% get restricted subsets:
      - 10%: one random instrument group
      - 10%: 2-3 random instrument groups
      - 5%: 3-6 specific instruments drawn from across groups
    """
    roll = rng.random()
    if roll < 0.75:
        # Full instrument set
        return _flatten_instruments()
    elif roll < 0.85:
        # 10%: one random group
        groups = list(DEFAULT_INSTRUMENTS.keys())
        groups.remove("Custom")
        group = rng.choice(groups)
        return list(DEFAULT_INSTRUMENTS[group])
    elif roll < 0.95:
        # 10%: 2-3 random groups
        groups = list(DEFAULT_INSTRUMENTS.keys())
        groups.remove("Custom")
        count = rng.randint(2, 3)
        selected = rng.sample(groups, min(count, len(groups)))
        instruments = []
        for g in selected:
            instruments.extend(DEFAULT_INSTRUMENTS[g])
        return instruments
    else:
        # 5%: 3-6 specific individual instruments from across all groups
        all_instruments = _flatten_instruments()
        count = rng.randint(3, 6)
        return rng.sample(all_instruments, min(count, len(all_instruments)))


@dataclass
class SessionState:
    """Isolated mutable state for one jockey's session."""

    jockey_id: int
    run_seed: int
    # run_seed is required so taste is deterministic per run
    bpm: int = 128
    key: str = "C major"
    active_stems: list[dict[str, Any]] = field(default_factory=list)
    stem_history: list[list[dict[str, Any]]] = field(default_factory=list)
    loop_count: int = 0
    available_instruments: list[str] = field(default_factory=list)
    available_models: list[dict[str, Any]] = field(default_factory=list)
    current_set_name: str = "Initial Vibe"
    llm_reasoning: str = ""
    user_override: str = ""
    _rng: random.Random = field(init=False)

    def __post_init__(self) -> None:
        # Taste seed: run_seed + jockey_id — varies per run AND per jockey,
        # so the same jockey has different taste in different runs (but reproducible
        # within a run for the same jockey)
        taste_seed_bytes = str((self.run_seed * 9999) + self.jockey_id).encode()
        taste_seed = int(hashlib.sha256(taste_seed_bytes).hexdigest(), 16) % (2**31)
        self._rng = random.Random(taste_seed)

        # Randomized starting BPM (not always 128)
        self.bpm = self._rng.choice(BPMS_ALL)
        # Randomized starting key (not always C major)
        self.key = self._rng.choice(KEYS_ALL)

        # Randomized instrument selection (75% full, 25% restricted)
        self.available_instruments = _select_instruments_for_jockey(self._rng)

        # Initialize available_models deterministically
        self.available_models = [
            {
                "id": "foundation-1",
                "description": "General purpose electronic sounds. Excellent for driving drums, thick bass, and sharp leads.",
                "supported_families": ["Synth", "Keys", "Bass", "Bowed Strings", "Mallet", "Wind",
                                        "Guitar", "Brass", "Vocal", "Plucked Strings"],
            }
        ]
        if self._rng.random() < 0.5:
            self.available_models.append({
                "id": "infinite-pianos",
                "description": "Specialized piano model.",
                "supported_families": ["Keys", "Piano", "Mallet"],
            })
        if self._rng.random() < 0.2:
            self.available_models.append({
                "id": "vocal-textures",
                "description": "Specialized vocal textures.",
                "supported_families": ["Vocal", "Choir", "Pad", "Atmosphere"],
            })


def apply_actions(state: SessionState, actions: list[dict]) -> None:
    """Apply LLM-returned DJ actions to session state.

    Uses production's process_actions from framework_main_async.py,
    then builds next_stems with generation prompts (matching _run_loop step 5).
    """
    # Use production's action processing (handles retain/add/remove, deduplication)
    deduped_tracks = process_actions(actions, state.active_stems)

    # Build next_stems (mirrors _run_loop step 5)
    next_stems: list[dict] = []
    for t in deduped_tracks:
        next_stems.append({
            "prompt": _build_prompt(t, state.key, state.bpm),
            "model_id": t.get("model_id", "foundation-1"),
            "bpm": state.bpm,
            "key": state.key,
            "bars": t.get("bars", 4),
            "_original_details": t,
            "_age": t.get("_age", 0),
            **t,
        })

    # Snapshot to history (max MAX_HISTORY)
    state.stem_history.append(list(state.active_stems))
    if len(state.stem_history) > MAX_HISTORY:
        state.stem_history.pop(0)

    state.active_stems = next_stems
    state.loop_count += 1


def _build_prompt(track: dict, key: str, bpm: int) -> str:
    """Build generation prompt from track details (mirrors production _build_prompt).

    Production's fallback template (when no model-specific template is available):
    "{major_family}, {sub_family}, {timbre_tags}, {notation_tag}, {fx_tag}, {key}, {bpm} BPM, {bars} Bars"
    """
    major = track.get("major_family", "Synth")
    sub = track.get("sub_family", "Synth Lead")
    timbres = " ".join(track.get("timbre_tags", ["Warm"]))
    notation = track.get("notation_tag", "melody")
    fx = track.get("fx_tag", "Medium Reverb")
    bars = track.get("bars", 4)
    return (
        f"{major}, {sub}, {timbres}, {notation}, "
        f"{fx}, {key}, {bpm} BPM, {bars} Bars"
    )
