"""
Async Conductor LLM Client for mc-clanker.

Provides async/await interface for the Conductor LLM calls,
using openai.AsyncOpenAI for non-blocking requests.

This is the async version of framework_conductor.py
"""

import json
import os
import re
from typing import Optional, Dict, Any, List
from openai import AsyncOpenAI


def parse_llm_json_response(content: str) -> Dict[str, Any]:
    """Parse JSON from LLM response, handling markdown wrapping.

    Tries direct json.loads first, then strips common markdown patterns:
    - ```json ... ``` code fences
    - Trailing text after the JSON block
    - Leading text before the JSON block
    """
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # Try stripping markdown code fences
    fence_pattern = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
    match = fence_pattern.search(content)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Try extracting first JSON object from content
    # Find the first { and last } and try to parse that substring
    first_brace = content.find("{")
    last_brace = content.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        candidate = content[first_brace:last_brace + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    # If all recovery attempts fail, raise the original error
    raise ValueError(f"Could not parse JSON from LLM response: {content}")


class ConductorLLMAsync:
    """
    Async version of the Conductor LLM client.

    Uses openai.AsyncOpenAI for non-blocking API calls,
    making it suitable for use in the async framework loop.
    """

    def __init__(
        self,
        api_base: str = None,
        model_name: str = "local-model",
        api_key: str = "not-needed"
    ):
        self.api_base = api_base or os.environ.get("LLM_BASE_URL", "http://localhost:1234/v1")
        self.model_name = model_name
        self.api_key = api_key
        self._async_client: Optional[AsyncOpenAI] = None
        self._cached_config: Optional[tuple] = None

        self.system_instruction = """You are an expert AI DJ and Electronic Music Producer. Your sole purpose is to guide an Automated DJ system by deciding the absolute best musical elements to generate or modify next.
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

Output a valid JSON object matching the requested schema EXACTLY. Do not output any thinking or extra text outside the JSON.
"""

        self.user_message_template = """Current State:
Master BPM: {bpm}
Master Key: {key}
Active Stems (Currently Playing):
{stems}

Recent Track History:
{history}

Available Instrument Types:
{instruments}

Available AI Generator Models:
{models}

YOUR TASK:
Provide the next set of DJ actions.
Instead of generating a full tracklist, you must define an array of `actions`:
- `retain`: Keep an active stem playing exactly as it is (REQUIRED for flow). You must provide its exact `stem_index`.
- `add`: Introduce a NEW stem. Provide the full instrument parameters (major_family, sub_family, etc.) AND a `model_id`.
- `remove`: Stop an active stem from playing. Provide its `stem_index`.

To keep the groove flowing, you SHOULD `retain` most of the 'Active Stems'. You should never have complete turn over of stems.
CRITICAL: If the music needs rhythm, ensure you explicitly `add` a 'Drums' stem if one are not already playing!
DENSITY RULE: There are currently {stem_count} active stems. {density_directive}
STEM FRESHNESS: Stems with higher age values (5-10+ loops) are getting stale. Prefer removing older stems to keep the mix fresh.

Analyze the Active Stems and History considering the Frequency Balancing and DJ rules, then output the JSON now.
"""

    def _get_async_client(self, config: Dict[str, str] = None) -> AsyncOpenAI:
        """Get or create async client based on config."""
        if config:
            config_key = (config.get('base_url'), config.get('api_key'), config.get('model'))
            if self._cached_config != config_key:
                self._async_client = AsyncOpenAI(
                    base_url=config.get('base_url', self.api_base),
                    api_key=config.get('api_key', self.api_key)
                )
                self._cached_config = config_key
            return self._async_client

        if self._async_client is None:
            self._async_client = AsyncOpenAI(
                base_url=self.api_base,
                api_key=self.api_key
            )
        return self._async_client

    async def call_async(
        self,
        prompt: str,
        llm_config: Dict[str, str] = None,
        max_retries: int = 3,
    ) -> Dict[str, Any]:
        """
        Make an async call to the LLM with JSON parse retry.

        Args:
            prompt: The user prompt to send
            llm_config: Optional config dict with base_url, api_key, model
            max_retries: Max LLM call retries on JSON parse failure (default 3)

        Returns:
            Parsed JSON response from LLM

        Raises:
            ValueError: If all retries fail to produce parseable JSON
        """
        client = self._get_async_client(llm_config)
        model_name = (llm_config or {}).get('model', self.model_name)

        last_error: str = ""

        for attempt in range(max_retries):
            try:
                response = await client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": self.system_instruction},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.7,
                    timeout=60.0,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "dj_action_state",
                            "strict": True,
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "reasoning": {"type": "string"},
                                    "master_bpm": {"type": "integer", "enum": [100, 110, 120, 128, 130, 140, 150]},
                                    "master_key": {"type": "string", "enum": ["C major", "C minor", "C# major", "C# minor", "D major", "D minor", "D# major", "D# minor", "E major", "E minor", "F major", "F minor", "F# major", "F# minor", "G major", "G minor", "G# major", "G# minor", "A major", "A minor", "A# major", "A# minor", "B major", "B minor"]},
                                    "actions": {
                                        "type": "array",
                                        "items": {
                                            "anyOf": [
                                                {
                                                    "type": "object",
                                                    "description": "Retain an existing stem to keep the groove flowing.",
                                                    "properties": {
                                                        "action_type": {"type": "string", "const": "retain"},
                                                        "stem_index": {"type": "integer", "description": "Index of the active stem to keep."}
                                                    },
                                                    "required": ["action_type", "stem_index"],
                                                    "additionalProperties": False
                                                },
                                                {
                                                    "type": "object",
                                                    "description": "Add a new musical element to the mix.",
                                                    "properties": {
                                                        "action_type": {"type": "string", "const": "add"},
                                                        "model_id": {"type": "string"},
                                                        "major_family": {"type": "string"},
                                                        "sub_family": {"type": "string"},
                                                        "timbre_tags": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                                                        "notation_tag": {"type": "string"},
                                                        "fx_tag": {"type": "string"},
                                                        "bars": {"type": "integer", "enum": [4, 8]}
                                                    },
                                                    "required": ["action_type", "model_id", "major_family", "sub_family", "timbre_tags", "notation_tag", "fx_tag", "bars"],
                                                    "additionalProperties": False
                                                },
                                                {
                                                    "type": "object",
                                                    "description": "Remove a stem to refresh the mix or change the arrangement.",
                                                    "properties": {
                                                        "action_type": {"type": "string", "const": "remove"},
                                                        "stem_index": {"type": "integer"}
                                                    },
                                                    "required": ["action_type", "stem_index"],
                                                    "additionalProperties": False
                                                }
                                            ]
                                        }
                                    },
                                    "name": {"type": "string"}
                                },
                                "required": ["reasoning", "master_bpm", "master_key", "actions", "name"],
                                "additionalProperties": False
                            }
                        }
                    }
                )

                content = response.choices[0].message.content
                if content is None:
                    raise ValueError("LLM returned empty content")
                return parse_llm_json_response(content)

            except (json.JSONDecodeError, ValueError) as e:
                last_error = str(e)
                if attempt < max_retries - 1:
                    continue
            except Exception as e:
                last_error = str(e)
                break

        raise ValueError(f"Could not parse JSON from LLM after {max_retries} attempts: {last_error}")

    async def get_next_state_async(
        self,
        current_bpm: int,
        current_key: str,
        active_stems: List[Dict],
        user_override: str = "",
        available_instruments: List[str] = None,
        stem_history: List[List[Dict]] = None,
        llm_config: Dict[str, str] = None,
        available_models: List[Dict] = None
    ) -> Dict[str, Any]:
        """
        Async version of get_next_state.

        Builds the prompt and calls the LLM asynchronously.
        """
        if available_instruments is None:
            available_instruments = ["Any"]
        if stem_history is None:
            stem_history = []

        # Build compact history
        simple_history = []
        for loop_stems in stem_history[-5:]:
            prompts = [s.get('prompt', '').split(',')[0] for s in loop_stems]
            simple_history.append("+".join(prompts))
        history_str = " | ".join(simple_history)

        # Build current stems with indices
        simple_stems = []
        for idx, s in enumerate(active_stems):
            age = s.get('_age', 0)
            simple_stems.append(f"Index {idx} (age {age}): {s.get('prompt', 'Unknown')}")

        models_str = "None provided"
        if available_models:
            models_list = [
                f"- {m['id']}: {m['description']} (Supported Families: {m.get('supported_families', ['Any'])})"
                for m in available_models
            ]
            models_str = "\n".join(models_list)

        stem_count = len(active_stems)
        density_directive = (
            "This mix is too sparse for a professional sound. Aim for 4-6 stems."
            if stem_count < 4
            else "The mix density is good. Maintain 4-6 stems for a full sound."
        )

        user_prompt = self.user_message_template.format(
            bpm=current_bpm,
            key=current_key,
            stems="\n".join(simple_stems) if simple_stems else "None",
            history=history_str if history_str else "None",
            instruments=", ".join(available_instruments),
            models=models_str,
            stem_count=stem_count,
            density_directive=density_directive
        )

        if user_override:
            user_prompt += f"\nOVERRIDE: {user_override}"
            print(f"DEBUG: Vibe appended to prompt: '{user_override}'")

        # Call LLM async
        return await self.call_async(user_prompt, llm_config)


class ConductorPromptBuilder:
    """
    Helper class for building Conductor prompts.

    Provides static methods for building prompts and parsing actions,
    making it easier to reuse Conductor logic in the async framework.
    """

    @staticmethod
    def build_prompt(
        current_bpm: int,
        current_key: str,
        active_stems: List[Dict],
        user_override: str = "",
        available_instruments: List[str] = None,
        stem_history: List[List[Dict]] = None,
        available_models: List[Dict] = None
    ) -> str:
        """Build a Conductor prompt from current state."""
        if available_instruments is None:
            available_instruments = ["Any"]
        if stem_history is None:
            stem_history = []

        # Compact history
        simple_history = []
        for loop_stems in stem_history[-5:]:
            prompts = [s.get('prompt', '').split(',')[0] for s in loop_stems]
            simple_history.append("+".join(prompts))
        history_str = " | ".join(simple_history)

        # Current stems
        simple_stems = []
        for idx, s in enumerate(active_stems):
            age = s.get('_age', 0)
            simple_stems.append(f"Index {idx} (age {age}): {s.get('prompt', 'Unknown')}")

        models_str = "None provided"
        if available_models:
            models_list = [
                f"- {m['id']}: {m['description']} (Supported Families: {m.get('supported_families', ['Any'])})"
                for m in available_models
            ]
            models_str = "\n".join(models_list)

        stem_count = len(active_stems)
        density_directive = (
            "This mix is too sparse for a professional sound. Aim for 4-6 stems."
            if stem_count < 4
            else "The mix density is good. Maintain 4-6 stems for a full sound."
        )

        template = """Current State:
Master BPM: {bpm}
Master Key: {key}
Active Stems (Currently Playing):
{stems}

Recent Track History:
{history}

Available Instrument Types:
{instruments}

Available AI Generator Models:
{models}

YOUR TASK:
Provide the next set of DJ actions.
Instead of generating a full tracklist, you must define an array of `actions`:
- `retain`: Keep an active stem playing exactly as it is (REQUIRED for flow). You must provide its exact `stem_index`.
- `add`: Introduce a NEW stem. Provide the full instrument parameters (major_family, sub_family, etc.) AND a `model_id`.
- `remove`: Stop an active stem from playing. Provide its `stem_index`.

To keep the groove flowing, you SHOULD `retain` most of the 'Active Stems'. You should never have complete turn over of stems.
CRITICAL: If the music needs rhythm, ensure you explicitly `add` a 'Drums' stem if one is not already playing!
DENSITY RULE: There are currently {stem_count} active stems. {density_directive}
STEM FRESHNESS: Stems with higher age values (5-10+ loops) are getting stale. Prefer removing older stems to keep the mix fresh.

Analyze the Active Stems and History considering the Frequency Balancing and DJ rules, then output the JSON now."""

        prompt = template.format(
            bpm=current_bpm,
            key=current_key,
            stems="\n".join(simple_stems) if simple_stems else "None",
            history=history_str if history_str else "None",
            instruments=", ".join(available_instruments),
            models=models_str,
            stem_count=stem_count,
            density_directive=density_directive
        )

        if user_override:
            prompt += f"\nOVERRIDE: {user_override}"

        return prompt

    @staticmethod
    def parse_actions(response: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Parse actions from LLM response."""
        return response.get("actions", [])

    @staticmethod
    def build(state, session_id) -> tuple[str, Dict[str, str]]:
        """
        Build prompt from GlobalState.

        This is the main entry point for the async framework loop.
        """
        with state.lock:
            current_bpm = state.current_bpm
            current_key = state.current_key
            active_stems = state.active_stems
            user_override = state.user_override
            available_instruments = state.available_instruments
            stem_history = state.stem_history

            llm_config = {
                'base_url': state.llm_base_url,
                'api_key': state.llm_api_key,
                'model': state.llm_model
            }

        # Get available models from generator if available
        available_models = []
        generator = getattr(state, 'generator', None)
        if generator and hasattr(generator, 'models'):
            import json as json_module
            import os
            desc = "No description"
            if os.path.exists("models_config.json"):
                with open("models_config.json") as f:
                    cfg = json_module.load(f)
                    for model_id in generator.models:
                        m_info = cfg.get("models", {}).get(model_id, {})
                        desc = m_info.get("description", desc)
                        supported_families = m_info.get("supported_families", ["Any"])
                        available_models.append({
                            "id": model_id,
                            "description": desc,
                            "supported_families": supported_families
                        })

        return ConductorPromptBuilder.build_prompt(
            current_bpm=current_bpm,
            current_key=current_key,
            active_stems=active_stems,
            user_override=user_override,
            available_instruments=available_instruments,
            stem_history=stem_history,
            available_models=available_models
        ), llm_config
