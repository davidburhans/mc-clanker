"""SlopJockey — async stateful DJ session using production ConductorLLMAsync.

One SlopJockey owns one SessionState and runs N loops:
  1. Read current state
  2. Call ConductorLLMAsync.get_next_state_async() (production LLM code)
  3. Parse response → apply_actions to SessionState
  4. Return JSONL record
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any

from app.framework.framework_conductor_async import ConductorLLMAsync, ConductorPromptBuilder

from simulation.session_state import SessionState, apply_actions


logger = logging.getLogger(__name__)


class SlopJockey:
    """Async stateful DJ session — reuses production ConductorLLMAsync."""

    def __init__(
        self,
        jockey_id: int,
        perf_id: int,
        run_seed: int,
        min_loops: int = 96,
        max_loops: int = 256,
        vibe_prob: float = 0.15,
        vibe_clear_prob: float = 0.05,
        llm_base_url: str = "http://localhost:1234/v1",
        llm_model: str = "local-model",
        enable_thinking: bool = False,
    ):
        self.jockey_id = jockey_id
        self.perf_id = perf_id
        self.session_id = jockey_id * 1000 + perf_id  # unique per performance
        self.run_seed = run_seed
        self.min_loops = min_loops
        self.max_loops = max_loops
        self.vibe_prob = vibe_prob
        self.vibe_clear_prob = vibe_clear_prob
        self._extra_body: dict | None = (
            {"chat_template_kwargs": {"enable_thinking": enable_thinking}}
            if enable_thinking else None
        )
        self._current_vibe: str | None = None  # persisted across loops in this session

        # Loop count varies per session but is deterministic per session+run
        loop_rng = random.Random(run_seed + self.session_id * 31337)
        self._total_loops = loop_rng.randint(min_loops, max_loops)

        # Per-jockey state — SessionState handles randomized BPM, key,
        # and instrument selection in __post_init__ (seeded by jockey_id + run_seed)
        self.state = SessionState(jockey_id=jockey_id, run_seed=run_seed)

        # Per-jockey ConductorLLMAsync (shares AsyncOpenAI connection pool per base_url)
        self._conductor = ConductorLLMAsync(
            api_base=llm_base_url,
            model_name=llm_model,
        )
        self._loops_completed = 0

    async def run_loop(
        self,
        semaphore: asyncio.Semaphore,
    ) -> dict[str, Any] | None:
        """Execute one loop. Returns record dict or None on failure."""
        # Persistent vibe: 15% chance to set new vibe each loop,
        # 5% chance to clear. Once set, persists until cleared/overridden.
        rng = random.Random(
            (self.run_seed << 20) + (self.session_id << 8) + self._loops_completed
        )
        if rng.random() < self.vibe_clear_prob:
            self._current_vibe = None
        elif rng.random() < self.vibe_prob:
            from slop_harness.vibe_prompt_bank import VibePromptBank
            self._current_vibe = VibePromptBank().sample(rng)

        user_override = self._current_vibe or ""

        # Build the user prompt once — store this exact text in the record
        user_prompt = ConductorPromptBuilder.build_prompt(
            current_bpm=self.state.bpm,
            current_key=self.state.key,
            active_stems=self.state.active_stems,
            user_override=user_override,
            available_instruments=self.state.available_instruments,
            stem_history=self.state.stem_history,
            available_models=self.state.available_models,
        )

        llm_config = {
            "base_url": self._conductor.api_base,
            "api_key": self._conductor.api_key,
            "model": self._conductor.model_name,
        }

        messages = [
            {"role": "system", "content": self._conductor.system_instruction},
            {"role": "user", "content": user_prompt},
        ]

        parsed: dict[str, Any] | None = None
        llm_failed = False

        # Call call_async directly with the built prompt so stored messages
        # match exactly what was transmitted
        async with semaphore:
            try:
                parsed = await self._conductor.call_async(
                    prompt=user_prompt,
                    llm_config=llm_config,
                    extra_body=self._extra_body,
                )
            except Exception as e:
                logger.warning(
                    f"Session jockey={self.jockey_id} perf={self.perf_id} "
                    f"loop {self._loops_completed} LLM error: {e}"
                )
                llm_failed = True

        # Production fallback: on LLM failure or None response, retain all current stems
        if llm_failed or parsed is None:
            fallback_actions = [
                {"action_type": "retain", "stem_index": i}
                for i in range(len(self.state.active_stems))
            ]
            parsed = {
                "master_bpm": self.state.bpm,
                "master_key": self.state.key,
                "actions": fallback_actions,
                "reasoning": "LLM failed. Retaining current groove.",
                "name": self.state.current_set_name,
            }

        # Serialize back to JSON string for the record
        response_text: str
        try:
            response_text = json.dumps(parsed)
        except Exception:
            logger.warning(
                f"Session jockey={self.jockey_id} perf={self.perf_id} "
                f"loop {self._loops_completed} failed to serialize response"
            )
            # Return a valid fallback DJ response — don't advance state incorrectly
            fallback_actions = [
                {"action_type": "retain", "stem_index": i}
                for i in range(len(self.state.active_stems))
            ]
            fallback = {
                "master_bpm": self.state.bpm,
                "master_key": self.state.key,
                "actions": fallback_actions,
                "reasoning": "Serialization failed. Retaining current groove.",
                "name": self.state.current_set_name,
            }
            response_text = json.dumps(fallback)
            parsed = fallback

        # Apply actions to evolve state
        try:
            actions = parsed.get("actions", [])
            apply_actions(self.state, actions)

            # Update state BPM/key from LLM response
            if parsed.get("master_bpm"):
                self.state.bpm = parsed["master_bpm"]
            if parsed.get("master_key"):
                self.state.key = parsed["master_key"]
            if parsed.get("name"):
                self.state.current_set_name = parsed["name"]
            if parsed.get("reasoning"):
                self.state.llm_reasoning = parsed["reasoning"]
        except Exception as e:
            logger.warning(
                f"Session jockey={self.jockey_id} perf={self.perf_id} "
                f"loop {self._loops_completed} action application error: {e}"
            )
            # On action application failure, still count the loop but return
            # a record with was_applied=False so the training pipeline knows
            # this LLM response was NOT applied to state
            self._loops_completed += 1
            return {"messages": messages, "response": response_text, "was_applied": False}

        self._loops_completed += 1

        return {"messages": messages, "response": response_text, "was_applied": True}

    async def run(
        self,
        semaphore: asyncio.Semaphore,
    ) -> list[dict[str, Any]]:
        """Run all loops. Returns list of successful record dicts."""
        records = []
        while self._loops_completed < self._total_loops:
            record = await self.run_loop(semaphore)
            if record is not None:
                records.append(record)
        return records
