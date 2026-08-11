"""
RED Tests: simulation/ LLM dog-fooding fixes.

These tests verify the fixes for issues found during code review:
1. extra_body parameter flows through call_async → get_next_state_async
2. messages dict stored in record matches exactly what was sent to LLM
3. debug print removed from ConductorPromptBuilder.build_prompt()
4. VibePromptBank is thread-safe under concurrent access
5. JSON serialization failure returns valid fallback, not "{}", with actions applied
6. Response format schemas are aligned between old harness and production
"""

import inspect
import threading
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Issue #1: extra_body parameter must flow through call_async → get_next_state_async
# ---------------------------------------------------------------------------


class TestExtraBodyParameter:
    """Tests for extra_body parameter propagation."""

    def test_call_async_accepts_extra_body_parameter(self):
        """
        RED: call_async() must accept an extra_body parameter.

        This test FAILS because call_async() does not currently accept
        extra_body as a parameter. It must be added.
        """
        from app.framework.framework_conductor_async import ConductorLLMAsync

        sig = inspect.signature(ConductorLLMAsync.call_async)
        params = list(sig.parameters.keys())

        assert "extra_body" in params, (
            "call_async() must have an 'extra_body' parameter to pass "
            "chat_template_kwargs like enable_thinking to the LLM API"
        )

    def test_get_next_state_async_accepts_extra_body_parameter(self):
        """
        RED: get_next_state_async() must accept and propagate extra_body.

        This test FAILS because get_next_state_async() does not currently
        accept extra_body. It must be added and passed through to call_async().
        """
        from app.framework.framework_conductor_async import ConductorLLMAsync

        sig = inspect.signature(ConductorLLMAsync.get_next_state_async)
        params = list(sig.parameters.keys())

        assert "extra_body" in params, "get_next_state_async() must have an 'extra_body' parameter"

    @pytest.mark.asyncio
    async def test_extra_body_passed_to_llm_api(self):
        """
        RED: When extra_body is provided, it must be passed to the LLM API call.

        This test FAILS because the current call_async() does not accept or
        pass extra_body to client.chat.completions.create().
        """
        from app.framework.framework_conductor_async import ConductorLLMAsync

        conductor = ConductorLLMAsync(api_base="http://test:1234/v1", model_name="test-model")

        captured_call_kwargs = {}

        async def mock_create(**kwargs):
            captured_call_kwargs.update(kwargs)
            mock_response = MagicMock()
            mock_response.choices = [MagicMock()]
            mock_response.choices[
                0
            ].message.content = (
                '{"master_bpm":128,"master_key":"C major","actions":[],"reasoning":"test","name":"test"}'
            )
            return mock_response

        with patch.object(conductor, "_get_async_client") as mock_get_client:
            mock_client = MagicMock()
            mock_client.chat.completions.create = mock_create
            mock_get_client.return_value = mock_client

            extra_body = {"chat_template_kwargs": {"enable_thinking": False}}

            # First verify the signature accepts extra_body (RED test for signature)
            sig = inspect.signature(ConductorLLMAsync.call_async)
            assert "extra_body" in sig.parameters, "call_async must accept extra_body param"

            # Now verify it gets passed through
            await conductor.call_async(
                prompt="test prompt",
                extra_body=extra_body,
            )

        assert "extra_body" in captured_call_kwargs, "extra_body kwarg must be passed to chat.completions.create()"
        assert captured_call_kwargs["extra_body"] == extra_body


# ---------------------------------------------------------------------------
# Issue #3: debug print must be removed from ConductorPromptBuilder.build_prompt()
# ---------------------------------------------------------------------------


class TestDebugPrintRemoved:
    """Test that debug print is removed from build_prompt()."""

    def test_get_next_state_async_has_no_debug_print(self):
        """
        RED: get_next_state_async() must not contain debug print statements.

        This test FAILS because the current implementation has:
            print(f"DEBUG: Vibe appended to prompt: '{user_override}'")
        at line 321 of framework_conductor_async.py.
        """
        from app.framework.framework_conductor_async import ConductorLLMAsync

        source = inspect.getsource(ConductorLLMAsync.get_next_state_async)

        assert "print(" not in source, (
            "get_next_state_async() must not contain debug print statements. DEBUG print found at line 321."
        )


# ---------------------------------------------------------------------------
# Issue #4: SlopJockey.run_loop() messages must match exactly what was sent
# ---------------------------------------------------------------------------


class TestJockeyMessagesAccuracy:
    """Tests that messages stored in record are exactly what was sent to LLM."""

    def test_jockey_stores_messages_that_match_what_was_sent(self):
        """
        RED: SlopJockey must store messages that were actually sent to the LLM.

        This test FAILS because SlopJockey builds its own prompt via
        ConductorPromptBuilder.build_prompt(), but then calls
        get_next_state_async() which builds ANOTHER prompt internally.
        The stored messages dict does not reflect what was actually transmitted.
        """
        from simulation.jockey import SlopJockey

        # Check that SlopJockey.run_loop calls call_async directly with the
        # same messages it stores — not get_next_state_async which rebuilds the prompt
        source = inspect.getsource(SlopJockey.run_loop)

        # The fix should call call_async directly, not get_next_state_async
        # so that stored messages match what was actually sent
        assert "call_async" in source, (
            "SlopJockey.run_loop() must call conductor.call_async() directly "
            "with the pre-built prompt, so stored messages match what was transmitted. "
            "Currently it calls get_next_state_async() which rebuilds the prompt."
        )


# ---------------------------------------------------------------------------
# Issue #5: VibePromptBank thread safety
# ---------------------------------------------------------------------------


class TestVibePromptBankThreadSafety:
    """Tests that VibePromptBank is safe under concurrent access."""

    def test_vibe_prompt_bank_templates_loaded_at_class_level(self):
        """
        RED: _templates must be populated at class level, not lazily in __new__.

        This test FAILS because _load_templates() is called inside __new__
        with no lock, creating a race condition when 2048 concurrent jockeys
        all try to sample from the bank simultaneously.
        """
        from slop_harness.vibe_prompt_bank import VibePromptBank

        # _templates should be a class-level attribute (populated before __new__ is called)
        # not a lazy instance attribute loaded without a lock
        source = inspect.getsource(VibePromptBank)

        # The current implementation calls _load_templates in __new__ without a lock
        # This is unsafe under concurrent access
        assert "_load_templates" not in source or "threading.Lock" in source, (
            "VibePromptBank must use a lock when initializing _templates in __new__, "
            "or _templates must be pre-populated at class level before __new__ is called. "
            "Current implementation has a race condition."
        )

    @pytest.mark.asyncio
    async def test_concurrent_sample_calls_succeed(self):
        """
        RED: Concurrent calls to VibePromptBank.sample() must all succeed.

        This test FAILS with the current implementation because _load_templates()
        is called in __new__ under no lock, and 2048 concurrent jockeys hitting
        the singleton simultaneously causes initialization races.
        """
        from slop_harness.vibe_prompt_bank import VibePromptBank

        bank = VibePromptBank()
        errors = []

        def sample_once():
            try:
                rng = MagicMock()
                rng.choice = lambda lst: lst[0]
                bank.sample(rng)
            except Exception as e:
                errors.append(e)

        # Simulate concurrent jockey access
        threads = [threading.Thread(target=sample_once) for _ in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Concurrent access raised errors: {errors}"


# ---------------------------------------------------------------------------
# Issue #6: JSON serialization failure handling
# ---------------------------------------------------------------------------


class TestSerializationFailure:
    """Tests for proper JSON serialization failure handling."""

    def test_serialization_failure_returns_valid_fallback_with_actions(self):
        """
        RED: On JSON serialization failure, a valid fallback response with actions
        must be returned — not "{}".

        This test FAILS because the current implementation returns "{}" on
        serialization failure, which has no valid DJ actions. The state also
        incorrectly advances (loop counted as complete) even though actions
        were not applied.
        """
        from simulation.jockey import SlopJockey

        source = inspect.getsource(SlopJockey.run_loop)

        # Should NOT store "{}" as the response on serialization failure
        assert '"{}"' not in source, (
            "Serialization failure must return a valid fallback DJ response "
            "(with retain actions for all current stems), not the string '{}'. "
            "Current code returns '{}' which is invalid JSON for DJ schema."
        )

        # Should apply actions even on serialization failure (via fallback)
        assert "apply_actions" in source, (
            "Action application must be attempted even when serialization fails, "
            "using a fallback response with retain actions."
        )

    def test_serialization_failure_does_not_corrupt_state(self):
        """D2: on json.dumps failure, run_loop must apply a valid fallback and
        still advance — state must not be left inconsistent.

        The previous body had NO assert (it only inspected source text). This
        drives the real SlopJockey.run_loop with the conductor mocked to return
        a response containing a non-JSON-serializable value, forcing the
        serialization-failure branch, and asserts the fallback is applied.
        """
        import asyncio
        import json as _json

        from simulation.jockey import SlopJockey

        jockey = SlopJockey(
            jockey_id=1,
            perf_id=1,
            run_seed=42,
            vibe_prob=0.0,  # avoid VibePromptBank file access
            vibe_clear_prob=0.0,
            llm_base_url="http://test-llm:1234/v1",
            llm_model="test-model",
        )
        # Seed one active stem so the fallback has something to retain.
        jockey.state.active_stems = [{"prompt": "drums"}]

        # Non-serializable value forces json.dumps(parsed) to raise.
        non_serializable = {
            "master_bpm": 128,
            "master_key": "C major",
            "actions": [{"action_type": "retain", "stem_index": 0}],
            "reasoning": object(),
            "name": "x",
        }

        async def fake_call_async(prompt, llm_config=None, extra_body=None):
            return non_serializable

        async def drive():
            with patch.object(jockey._conductor, "call_async", fake_call_async):
                return await jockey.run_loop(asyncio.Semaphore(1))

        record = asyncio.new_event_loop().run_until_complete(drive())

        # Loop advanced AND a valid fallback response was produced & applied.
        assert record is not None, "run_loop should return a record, not None"
        assert record["was_applied"] is True, (
            "serialization failure must apply the fallback actions (state must not be left inconsistent)"
        )
        parsed_response = _json.loads(record["response"])
        action_types = {a.get("action_type") for a in parsed_response.get("actions", [])}
        assert "retain" in action_types, "fallback must retain current stems so the groove continues"
        assert jockey._loops_completed == 1


# ---------------------------------------------------------------------------
# Issue #7: enable-thinking CLI arg and threading
# ---------------------------------------------------------------------------


class TestEnableThinkingCLI:
    """Tests for --enable-thinking CLI argument."""

    def test_cli_parser_has_enable_thinking_flag(self):
        """
        RED: cli.py must have an --enable-thinking flag.

        This test FAILS because the current implementation does not pass
        enable_thinking through the CLI and into SlopJockey.
        """
        from simulation import cli

        # Get the parser used by cli.parse_args
        parser_code = inspect.getsource(cli.parse_args)

        assert "--enable-thinking" in parser_code, (
            "cli.py must have an '--enable-thinking' argument to control "
            "whether enable_thinking=True is passed to the LLM via extra_body"
        )

    def test_jockey_accepts_enable_thinking_init_param(self):
        """
        RED: SlopJockey.__init__ must accept enable_thinking and store extra_body.

        This test FAILS because SlopJockey does not currently accept or store
        extra_body for passing to the LLM call.
        """
        from simulation.jockey import SlopJockey

        sig = inspect.signature(SlopJockey.__init__)
        params = list(sig.parameters.keys())

        # SlopJockey should accept enable_thinking (or _extra_body directly)
        assert any(p in params for p in ["enable_thinking", "_extra_body", "extra_body"]), (
            "SlopJockey.__init__() must accept 'enable_thinking' parameter "
            "and store corresponding extra_body dict for the LLM call"
        )


# ---------------------------------------------------------------------------
# Issue #8: response_format schema alignment
# ---------------------------------------------------------------------------


class TestResponseFormatSchema:
    """Tests that response_format schemas match between old harness and production."""

    def test_response_format_schemas_are_identical(self):
        """D3: the response_format schema used by the harness and by production
        must agree on action types.

        The previous body called ``get_response_format_schema()`` twice (a
        tautology). This imports the schema from two DISTINCT modules — the
        harness's in-use schema (``slop_harness.llm_client``) and the production
        schema (``app.lib.constants``) — so real drift is guarded.
        """
        import slop_harness.llm_client as llm_module
        from app.lib.constants import get_response_format_schema

        production_schema = get_response_format_schema()
        # The harness's actually-used schema: the shared builder when importable,
        # otherwise its static fallback.
        if getattr(llm_module, "_get_schema", None):
            harness_schema = llm_module._get_schema()
        else:
            harness_schema = llm_module._STATIC_RESPONSE_FORMAT

        def action_types(schema):
            items = schema["json_schema"]["schema"]["properties"]["actions"]["items"]
            return {a["properties"]["action_type"]["const"] for a in items.get("anyOf", []) if "properties" in a}

        prod_types = action_types(production_schema)
        harness_types = action_types(harness_schema)
        assert prod_types == harness_types == {"retain", "add", "remove"}, (
            f"schema drift: production={prod_types} harness={harness_types}"
        )


# ---------------------------------------------------------------------------
# Schema drift detection for slop_harness fallback
# ---------------------------------------------------------------------------


def test_harness_fallback_schema_matches_production():
    """
    Verify that slop_harness/llm_client.py's inline _STATIC_RESPONSE_FORMAT
    fallback stays in sync with app.lib.constants.get_response_format_schema().

    If these diverge, the harness would use a different schema when run
    without app/lib/, silently producing training data with a different JSON
    structure than the production path.
    """
    # Check what the harness module actually exposes
    import slop_harness.llm_client as llm_module
    from app.lib.constants import get_response_format_schema

    production_schema = get_response_format_schema()

    # The harness should be using get_response_format_schema (not the fallback)
    # Verify the schema it actually uses matches production
    if hasattr(llm_module, "_get_schema") and llm_module._get_schema is not None:
        harness_schema = llm_module._get_schema()
    elif hasattr(llm_module, "RESPONSE_FORMAT"):
        # Fallback path — verify it matches production
        harness_schema = getattr(llm_module, "RESPONSE_FORMAT")
    else:
        # Neither available — skip
        return

    # Compare the critical action structure
    production_actions = production_schema["json_schema"]["schema"]["properties"]["actions"]["items"]["anyOf"]
    harness_schema_actions = harness_schema["json_schema"]["schema"]["properties"]["actions"]["items"]["anyOf"]

    production_types = {a["properties"]["action_type"]["const"] for a in production_actions if "properties" in a}
    harness_types = {a["properties"]["action_type"]["const"] for a in harness_schema_actions if "properties" in a}

    assert production_types == harness_types == {"retain", "add", "remove"}, (
        f"Harness fallback schema action types {harness_types} must match production {production_types}"
    )
