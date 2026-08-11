"""Prompt builder — constructs Conductor user prompts from musical state.

System instruction is the fixed Conductor string.
User message is built from state dict + optional override.
"""

from typing import Any

SYSTEM_INSTRUCTION = """You are an AI DJ. The current session has active stems with ages and you must decide what to retain, add, or remove each loop.

RULES:
- RETAIN most stems for smooth flow — only change what needs changing
- Drums are REQUIRED if not already present
- Mix must spread across Low/Mid/High frequencies — avoid stacking competing sub-basses
- Target 4-6 active stems — add if sparse, remove if crowded
- Replace stems older than 5-10 loops (they get stale)
- Keep master_key stable unless transitioning
- Honor any OVERRIDE directive by incorporating that vibe into instrument/FX choices

Actions (output JSON only, no text outside JSON):
- retain: {"action_type": "retain", "stem_index": N}
- add: {"action_type": "add", "model_id": "...", "major_family": "...", "sub_family": "...", "timbre_tags": [...], "notation_tag": "...", "fx_tag": "...", "bars": N}
- remove: {"action_type": "remove", "stem_index": N}

Always output valid JSON with "master_bpm", "master_key", "actions" array, "reasoning", and "name"."""

AVAILABLE_INSTRUMENTS = [
    "Synth",
    "Keys",
    "Bass",
    "Bowed Strings",
    "Mallet",
    "Wind",
    "Guitar",
    "Brass",
    "Vocal",
    "Plucked Strings",
]

# Repo IDs for model display
MODEL_REPO_IDS = {
    "foundation-1": "RoyalCities/Foundation-1",
    "infinite-pianos": "RoyalCities/RC_Infinite_Pianos",
    "vocal-textures": "RoyalCities/Vocal_Textures_Main",
}


class PromptBuilder:
    """Builds Conductor prompts from musical state."""

    def build(self, state: dict[str, Any], override: str | None = None) -> list[dict[str, str]]:
        """Build message list from state dict.

        Returns:
            [{"role": "system", "content": SYSTEM_INSTRUCTION},
             {"role": "user", "content": <user_message>},
             {"role": "assistant", "content": ""}]
        """
        user_content = self._build_user_message(state, override)
        return [
            {"role": "system", "content": SYSTEM_INSTRUCTION},
            {"role": "user", "content": user_content},
            {"role": "assistant", "content": ""},
        ]

    def _build_user_message(self, state: dict[str, Any], override: str | None) -> str:
        stems_text = self._format_stems(state["stems"])
        history_text = self._format_history(state["history"])
        models_text = self._format_models(state["available_models"])
        instruments_text = ", ".join(AVAILABLE_INSTRUMENTS)
        density_directive = self._density_directive(state["stem_count"])

        parts = [
            f"Current State:",
            f"Master BPM: {state['bpm']}",
            f"Master Key: {state['key']}",
            f"Active Stems (Currently Playing):",
            stems_text,
            "",
            f"Recent Track History:",
            history_text,
            "",
            f"Available Instrument Types:",
            instruments_text,
            "",
            f"Available AI Generator Models:",
            models_text,
            "",
            "YOUR TASK:",
            "Provide the next set of DJ actions.",
            "Instead of generating a full tracklist, you must define an array of `actions`:",
            "- `retain`: Keep an active stem playing exactly as it is (REQUIRED for flow). You must provide its exact `stem_index`.",
            "- `add`: Introduce a NEW stem. Provide the full instrument parameters (major_family, sub_family, etc.) AND a `model_id`.",
            "- `remove`: Stop an active stem from playing. Provide its `stem_index`.",
            "",
            "To keep the groove flowing, you SHOULD `retain` most of the 'Active Stems'. You should never have complete turn over of stems.",
            "CRITICAL: If the music needs rhythm, ensure you explicitly `add` a 'Drums' stem if one is not already playing!",
            f"DENSITY RULE: There are currently {state['stem_count']} active stems. {density_directive}",
            "STEM FRESHNESS: Stems with higher age values (5-10+ loops) are getting stale. Prefer removing older stems to keep the mix fresh.",
            "",
            "Analyze the Active Stems and History considering the Frequency Balancing and DJ rules, then output the JSON now.",
        ]

        if override:
            parts.append("")
            parts.append(f"OVERRIDE: {override}")

        return "\n".join(parts)

    def _format_stems(self, stems: list[dict[str, Any]]) -> str:
        if not stems:
            return "(none)"
        lines = []
        for s in stems:
            tags = ", ".join(s.get("timbre_tags", []))
            lines.append(f"{s['instrument']} (age {s['_age']}) - sub: {s['sub_family']}, tags: {tags}")
        return "\n".join(lines)

    def _format_history(self, history: list[dict[str, Any]]) -> str:
        if not history:
            return "(no history)"
        lines = []
        for i, entry in enumerate(history):
            instruments = [s["instrument"] for s in entry.get("stems", [])]
            lines.append(f"Loop {i + 1}: {', '.join(instruments)}")
        return "\n".join(lines)

    def _format_models(self, available_models: list[str]) -> str:
        lines = []
        for mid in available_models:
            repo_id = MODEL_REPO_IDS.get(mid, mid)
            lines.append(f"- {mid} ({repo_id})")
        return "\n".join(lines)

    def _density_directive(self, stem_count: int) -> str:
        if stem_count < 4:
            return "You SHOULD add more elements to reach 4-6 stems."
        elif stem_count > 6:
            return "You SHOULD remove some elements to reach 4-6 stems."
        else:
            return "The current density is good."
