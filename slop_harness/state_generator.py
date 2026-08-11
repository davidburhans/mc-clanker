"""Deterministic musical state from seeds.

Each interaction is derived from (batch_id, interaction_id):
  loop_seed = hash((batch_id << 20) | interaction_id)

This produces:
  - song_age: 0-50 (loop count)
  - bpm: weighted by genre cluster
  - key: random from ALL_KEYS
  - stem_count: 1-7 (weighted toward 4-5)
  - stems: list of stem dicts with _age values
  - history: up to 5 previous stem sets
  - available_models: which models are "available" to the Conductor
"""

import hashlib
from random import Random
from typing import Any

from slop_harness.models import (
    ALL_BARS,
    ALL_BPMS,
    ALL_KEYS,
    FOUNDATION_1_MODEL,
    FX_TAGS_F1,
    NOTATION_TAGS_F1,
    TIMBRE_TAGS_F1,
)


class StateGenerator:
    """Deterministically generates musical state from batch_id + interaction_id."""

    # Genre cluster BPM ranges
    CLUSTER_BPM_RANGES = {
        "hiphop": (75, 95),
        "house": (118, 135),
        "dnb": (160, 175),
        "techno": (128, 152),
    }
    CLUSTER_WEIGHTS = {"hiphop": 0.3, "house": 0.35, "dnb": 0.15, "techno": 0.2}

    def __init__(self, batch_id: int, interaction_id: int):
        self.batch_id = batch_id
        self.interaction_id = interaction_id
        seed_bytes = str(batch_id << 20 | interaction_id).encode()
        self._seed = int(hashlib.sha256(seed_bytes).hexdigest(), 16) % (2**31)
        self._rng = Random(self._seed)

    def build(self) -> dict[str, Any]:
        """Build and return the full musical state dict."""
        song_age = (self.interaction_id * 7 + self.batch_id * 31) % 50

        cluster = self._rng.choices(
            list(self.CLUSTER_WEIGHTS.keys()),
            weights=list(self.CLUSTER_WEIGHTS.values()),
        )[0]
        bpm_min, bpm_max = self.CLUSTER_BPM_RANGES[cluster]
        bpm = self._rng.randint(bpm_min, bpm_max)
        # Snap to nearest valid BPM
        bpm = min(ALL_BPMS, key=lambda x: abs(x - bpm))

        key = self._rng.choice(ALL_KEYS)

        stem_count = max(1, min(7, int(self._rng.gauss(4, 1.5))))

        stems = self._generate_stems(stem_count, song_age, key, bpm)

        history = []
        for _ in range(min(5, song_age)):
            hist_age = self._rng.randint(0, song_age - 1) if song_age > 0 else 0
            hist_stems = self._generate_stems(self._rng.randint(1, 7), hist_age, key, bpm)
            history.append({"stems": hist_stems, "age": hist_age})

        available_models = self._select_available_models()

        return {
            "song_age": song_age,
            "bpm": bpm,
            "key": key,
            "stem_count": stem_count,
            "stems": stems,
            "history": history,
            "available_models": available_models,
        }

    def _generate_stems(self, count: int, base_age: int, key: str, bpm: int) -> list[dict[str, Any]]:
        """Generate 'count' stems with ages based on base_age."""
        stems = []
        for i in range(count):
            age = min(50, base_age + self._rng.randint(0, 3))
            stem = self._random_stem(age, key, bpm)
            stems.append(stem)
        return stems

    def _random_stem(self, age: int, key: str, bpm: int) -> dict[str, Any]:
        """Generate a single random stem."""
        major_family = self._rng.choice(FOUNDATION_1_MODEL["major_families"])
        sub_family = self._rng.choice(self._sub_families_for_major(major_family))
        timbre_tags = self._rng.sample(TIMBRE_TAGS_F1, min(3, len(TIMBRE_TAGS_F1)))
        notation_tag = self._rng.choice(NOTATION_TAGS_F1)
        fx_tag = self._rng.choice(FX_TAGS_F1)
        bars = self._rng.choice(ALL_BARS)

        prompt = f"{sub_family}, {', '.join(timbre_tags)}, {notation_tag}, {fx_tag}, {key}, {bpm} BPM, {bars} Bars"

        return {
            "instrument": sub_family,
            "major_family": major_family,
            "sub_family": sub_family,
            "timbre_tags": timbre_tags,
            "notation_tag": notation_tag,
            "fx_tag": fx_tag,
            "key": key,
            "bpm": bpm,
            "bars": bars,
            "model_id": "foundation-1",
            "prompt": prompt,
            "_age": age,
        }

    def _sub_families_for_major(self, major_family: str) -> list[str]:
        """Return sub_families for a major_family from Foundation-1."""
        sub_families = FOUNDATION_1_MODEL.get("sub_families", [])
        if not sub_families:
            return [major_family]
        return sub_families

    def _select_available_models(self) -> list[str]:
        """Select which models are 'available' to the Conductor.

        foundation-1 always present. Others randomly included.
        """
        available = ["foundation-1"]
        if self._rng.random() < 0.5:
            available.append("infinite-pianos")
        if self._rng.random() < 0.2:
            available.append("vocal-textures")
        return available
