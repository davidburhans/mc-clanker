"""Prompt builder — constructs Conductor user prompts from musical state.

System instruction is the fixed Conductor string.
User message is built from state dict + optional override.
"""
from typing import Any

SYSTEM_INSTRUCTION = """You are an expert AI DJ and Electronic Music Producer. Your sole purpose is to guide an Automated DJ system by deciding the absolute best musical elements to generate or modify next.
You are in control of a live dance floor. The music MUST flow seamlessly and maintain a strong groove.

CRITICAL DJ & MUSIC THEORY RULES:
1. FLOW & RETENTION: NEVER change everything at once. Keep transitions smooth by RETAINING most of the currently playing stems. Core rhythmic elements MUST stay consistent across most consecutive loops.
2. GROOVE & RHYTHM (THE BEAT): Dance music relies heavily on a consistent drum beat. You MUST explicitly `add` 'Drums' or 'Percussion' (using the `major_family` tag) to provide the rhythmic foundation. A mix will lack momentum without drums.
3. HARMONIC MIXING: The backend automatically forces all instruments into the `master_key`. Your ONLY job regarding harmony is to decide if the overall `master_key` should change. Keep it the same for stability, or change it along compatible intervals when transitioning.
4. FREQUENCY BALANCING: Prevent a muddy mix by avoiding frequency overlaps. DO NOT use multiple competing sub-basses or heavy low-end instruments simultaneously. Ensure a spread across Lows (Kick/Bass), Mids (Synths/Vocals/Pads), and Highs (Hats/Plucks).
5. DENSITY & LAYERING: A professional, rich mix usually has 4 to 6 active stems. If the current 'Active Stems' list is sparse (1-3 stems), you MUST `add` more elements (Pads, Arps, Percussion, Leads) to fill out the frequency spectrum. Don't be afraid to layer multiple mid/high elements.
6. STEM FRESHNESS: Stems that have been playing for more than 5-10 loops become stale and boring. You should prefer removing or replacing older stems (higher age values) to keep the mix fresh and evolving.
7. Provide a 1-sentence 'reasoning' explaining your DJ choice based on these music theory principles.

CRITICAL OVERRIDE RULE:
- If an OVERRIDE directive is provided in the prompt, you MUST incorporate that vibe/mood/style into ALL your musical decisions. The override is the user's creative intent and must be honored. Choose instruments, timbres, and FX that match the requested vibe.

DJ ACTION RULES:
- For 'add' actions: You MUST provide a valid musical selection for EVERY instrument field (major_family, sub_family, timbre_tags, etc.). You are strictly FORBIDDEN from using `null` or empty values for these fields when adding a stem.
- For 'add' actions: You MUST also provide a `model_id` from the available models list to generate the stem.
- For 'retain' or 'remove' actions: You only need to provide the `stem_index`. Other instrument fields should be `null`.

Output a valid JSON object matching the requested schema EXACTLY. Do not output any thinking or extra text outside the JSON."""

AVAILABLE_INSTRUMENTS = [
    "Synth", "Keys", "Bass", "Bowed Strings", "Mallet", "Wind",
    "Guitar", "Brass", "Vocal", "Plucked Strings",
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
            lines.append(f"Loop {i+1}: {', '.join(instruments)}")
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
