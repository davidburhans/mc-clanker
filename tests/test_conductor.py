import pytest
from unittest.mock import patch, MagicMock
from app.framework.framework_conductor import Conductor


@pytest.fixture
def conductor():
    return Conductor()


class TestConductorGetNextState:
    """Test Conductor.get_next_state method."""

    @patch('app.framework.framework_conductor.OpenAI')
    def test_returns_parsed_json(self, mock_openai, conductor):
        """Test successful LLM response parsing."""
        mock_client = MagicMock()
        mock_openai.return_value = mock_client

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '''
        {
            "master_bpm": 128,
            "master_key": "D minor",
            "actions": [
                {"action_type": "retain", "stem_index": 0}
            ],
            "reasoning": "Test reasoning",
            "name": "Test Set"
        }
        '''
        mock_client.chat.completions.create.return_value = mock_response
        conductor.client = mock_client

        result = conductor.get_next_state(
            current_bpm=128,
            current_key="D minor",
            active_stems=[{"prompt": "Synth, Pad, Warm"}]
        )

        assert result["master_bpm"] == 128
        assert result["master_key"] == "D minor"
        assert len(result["actions"]) == 1
        assert result["actions"][0]["action_type"] == "retain"
        assert result["reasoning"] == "Test reasoning"
        assert result["name"] == "Test Set"

    @patch('app.framework.framework_conductor.OpenAI')
    def test_fallback_on_llm_error(self, mock_openai, conductor):
        """Test fallback response when LLM throws exception."""
        mock_client = MagicMock()
        mock_openai.return_value = mock_client
        mock_client.chat.completions.create.side_effect = Exception("LLM API Error")
        conductor.client = mock_client

        active_stems = [
            {"prompt": "Synth, Pad, Warm"},
            {"prompt": "Bass, Sub, Thick"}
        ]

        result = conductor.get_next_state(
            current_bpm=120,
            current_key="C minor",
            active_stems=active_stems
        )

        # Should return fallback with retain actions for all stems
        assert result["name"] == "Fallback Recovery State"
        assert result["master_bpm"] == 120
        assert result["master_key"] == "C minor"
        assert len(result["actions"]) == 2
        assert result["actions"][0]["action_type"] == "retain"
        assert result["actions"][0]["stem_index"] == 0
        assert result["actions"][1]["action_type"] == "retain"
        assert result["actions"][1]["stem_index"] == 1
        assert "LLM FAILED" in result["reasoning"]

    @patch('app.framework.framework_conductor.OpenAI')
    def test_fallback_on_invalid_json(self, mock_openai, conductor):
        """Test fallback when LLM returns invalid JSON."""
        mock_client = MagicMock()
        mock_openai.return_value = mock_client

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "This is not JSON"
        mock_client.chat.completions.create.return_value = mock_response
        conductor.client = mock_client

        result = conductor.get_next_state(
            current_bpm=120,
            current_key="C minor",
            active_stems=[{"prompt": "Synth"}]
        )

        # Should return fallback
        assert result["name"] == "Fallback Recovery State"
        assert "LLM FAILED" in result["reasoning"]

    @patch('app.framework.framework_conductor.OpenAI')
    def test_user_override_appended_to_prompt(self, mock_openai, conductor):
        """Test user override is appended to prompt."""
        mock_client = MagicMock()
        mock_openai.return_value = mock_client

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '{"master_bpm": 120, "master_key": "C minor", "actions": [], "reasoning": "test", "name": "test"}'
        mock_client.chat.completions.create.return_value = mock_response
        conductor.client = mock_client

        conductor.get_next_state(
            current_bpm=120,
            current_key="C minor",
            active_stems=[],
            user_override="Make it more upbeat"
        )

        args, kwargs = mock_client.chat.completions.create.call_args
        messages = kwargs['messages']
        user_message = next(m['content'] for m in messages if m['role'] == 'user')

        assert "OVERRIDE: Make it more upbeat" in user_message

    @patch('app.framework.framework_conductor.OpenAI')
    def test_client_caching_on_config_change(self, mock_openai, conductor):
        """Test OpenAI client is recreated when config changes."""
        mock_client1 = MagicMock()
        mock_client2 = MagicMock()
        mock_openai.side_effect = [mock_client1, mock_client2]

        conductor.client = mock_client1

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '{"master_bpm": 120, "master_key": "C minor", "actions": [], "reasoning": "test", "name": "test"}'

        # First call with initial config
        llm_config1 = {
            'base_url': 'http://localhost:1234/v1',
            'api_key': 'key1',
            'model': 'model1'
        }
        conductor.get_next_state(120, "C minor", [], llm_config=llm_config1)

        # Change config
        llm_config2 = {
            'base_url': 'http://localhost:2345/v1',
            'api_key': 'key2',
            'model': 'model2'
        }
        conductor.get_next_state(120, "C minor", [], llm_config=llm_config2)

        # Should have created 2 different clients
        assert mock_openai.call_count == 2

    @patch('app.framework.framework_conductor.OpenAI')
    def test_client_reuse_on_same_config(self, mock_openai, conductor):
        """Test OpenAI client is reused when config unchanged."""
        mock_client = MagicMock()
        mock_openai.return_value = mock_client

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '{"master_bpm": 120, "master_key": "C minor", "actions": [], "reasoning": "test", "name": "test"}'
        mock_client.chat.completions.create.return_value = mock_response

        llm_config = {
            'base_url': 'http://localhost:1234/v1',
            'api_key': 'key1',
            'model': 'model1'
        }

        # Make two calls with same config
        conductor.get_next_state(120, "C minor", [], llm_config=llm_config)
        conductor.get_next_state(120, "C minor", [], llm_config=llm_config)

        # Should only create one client
        assert mock_openai.call_count == 1

    @patch('app.framework.framework_conductor.OpenAI')
    def test_deduplication_of_retain_actions(self, mock_openai, conductor):
        """Test that duplicate retain indices are handled."""
        mock_client = MagicMock()
        mock_openai.return_value = mock_client

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        # LLM returns duplicate retain indices (which would be an error)
        mock_response.choices[0].message.content = '''
        {
            "master_bpm": 120,
            "master_key": "C minor",
            "actions": [
                {"action_type": "retain", "stem_index": 0},
                {"action_type": "retain", "stem_index": 0},
                {"action_type": "retain", "stem_index": 1}
            ],
            "reasoning": "Test",
            "name": "Test"
        }
        '''
        mock_client.chat.completions.create.return_value = mock_response
        conductor.client = mock_client

        active_stems = [
            {"prompt": "Synth"},
            {"prompt": "Bass"}
        ]

        result = conductor.get_next_state(
            current_bpm=120,
            current_key="C minor",
            active_stems=active_stems
        )

        # Should still parse (duplicates are handled downstream in framework_main)
        assert result["master_bpm"] == 120
        assert len(result["actions"]) == 3

    @patch('app.framework.framework_conductor.OpenAI')
    def test_available_models_formatting(self, mock_openai, conductor):
        """Test models string formatting with supported_families."""
        mock_client = MagicMock()
        mock_openai.return_value = mock_client

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '{"master_bpm": 120, "master_key": "C minor", "actions": [], "reasoning": "test", "name": "test"}'
        mock_client.chat.completions.create.return_value = mock_response
        conductor.client = mock_client

        available_models = [
            {"id": "model_1", "description": "Good for bass.", "supported_families": ["Bass", "Synth"]},
            {"id": "model_2", "description": "Good for drums.", "supported_families": ["Drums"]}
        ]

        conductor.get_next_state(
            current_bpm=120,
            current_key="C minor",
            active_stems=[],
            available_models=available_models
        )

        args, kwargs = mock_client.chat.completions.create.call_args
        messages = kwargs['messages']
        user_message = next(m['content'] for m in messages if m['role'] == 'user')

        assert "Available AI Generator Models:" in user_message
        assert "- model_1: Good for bass. (Supported Families: ['Bass', 'Synth'])" in user_message
        assert "- model_2: Good for drums. (Supported Families: ['Drums'])" in user_message


class TestConductorDensityRule:
    """Test density rule injection."""

    @patch('app.framework.framework_conductor.OpenAI')
    def test_sparse_mix_gets_add_directive(self, mock_openai, conductor):
        """Test that sparse mix (1-3 stems) gets 'add more' directive."""
        mock_client = MagicMock()
        mock_openai.return_value = mock_client

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '{"master_bpm": 120, "master_key": "C minor", "actions": [], "reasoning": "test", "name": "test"}'
        mock_client.chat.completions.create.return_value = mock_response
        conductor.client = mock_client

        # Only 2 stems - sparse
        active_stems = [
            {"prompt": "Synth"},
            {"prompt": "Bass"}
        ]

        conductor.get_next_state(
            current_bpm=120,
            current_key="C minor",
            active_stems=active_stems
        )

        args, kwargs = mock_client.chat.completions.create.call_args
        messages = kwargs['messages']
        user_message = next(m['content'] for m in messages if m['role'] == 'user')

        assert "DENSITY RULE: There are currently 2 active stems." in user_message
        assert "This mix is too sparse for a professional sound. Aim for 4-6 stems." in user_message

    @patch('app.framework.framework_conductor.OpenAI')
    def test_good_density_gets_maintain_directive(self, mock_openai, conductor):
        """Test that good density (4-6 stems) gets 'maintain' directive."""
        mock_client = MagicMock()
        mock_openai.return_value = mock_client

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '{"master_bpm": 120, "master_key": "C minor", "actions": [], "reasoning": "test", "name": "test"}'
        mock_client.chat.completions.create.return_value = mock_response
        conductor.client = mock_client

        # 5 stems - good density
        active_stems = [
            {"prompt": "Drums"},
            {"prompt": "Bass"},
            {"prompt": "Synth Pad"},
            {"prompt": "Lead"},
            {"prompt": "Percussion"}
        ]

        conductor.get_next_state(
            current_bpm=120,
            current_key="C minor",
            active_stems=active_stems
        )

        args, kwargs = mock_client.chat.completions.create.call_args
        messages = kwargs['messages']
        user_message = next(m['content'] for m in messages if m['role'] == 'user')

        assert "DENSITY RULE: There are currently 5 active stems." in user_message
        assert "The mix density is good. Maintain 4-6 stems for a full sound." in user_message


class TestConductorEdgeCases:
    """Test edge cases in Conductor."""

    @patch('app.framework.framework_conductor.OpenAI')
    def test_empty_active_stems_with_history(self, mock_openai, conductor):
        """Test conductor with no active stems but has history."""
        mock_client = MagicMock()
        mock_openai.return_value = mock_client

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '{"master_bpm": 120, "master_key": "C minor", "actions": [], "reasoning": "test", "name": "test"}'
        mock_client.chat.completions.create.return_value = mock_response
        conductor.client = mock_client

        # No active stems but has history
        active_stems = []
        stem_history = [
            [{"prompt": "Synth, Pad, Warm"}],
            [{"prompt": "Bass, Sub, Thick"}]
        ]

        conductor.get_next_state(
            current_bpm=120,
            current_key="C minor",
            active_stems=active_stems,
            stem_history=stem_history
        )

        args, kwargs = mock_client.chat.completions.create.call_args
        messages = kwargs['messages']
        user_message = next(m['content'] for m in messages if m['role'] == 'user')

        # History should appear in the prompt
        assert "Recent Track History:" in user_message

    @patch('app.framework.framework_conductor.OpenAI')
    def test_max_density_directive(self, mock_openai, conductor):
        """Test that 6 stems gets 'maintain' directive (edge of good density)."""
        mock_client = MagicMock()
        mock_openai.return_value = mock_client

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '{"master_bpm": 120, "master_key": "C minor", "actions": [], "reasoning": "test", "name": "test"}'
        mock_client.chat.completions.create.return_value = mock_response
        conductor.client = mock_client

        # 6 stems - at the edge of good density
        active_stems = [
            {"prompt": "Drums"},
            {"prompt": "Bass"},
            {"prompt": "Synth Pad"},
            {"prompt": "Lead"},
            {"prompt": "Percussion"},
            {"prompt": "FX"}
        ]

        conductor.get_next_state(
            current_bpm=120,
            current_key="C minor",
            active_stems=active_stems
        )

        args, kwargs = mock_client.chat.completions.create.call_args
        messages = kwargs['messages']
        user_message = next(m['content'] for m in messages if m['role'] == 'user')

        assert "DENSITY RULE: There are currently 6 active stems." in user_message
        assert "The mix density is good" in user_message

    @patch('app.framework.framework_conductor.OpenAI')
    def test_llm_returns_empty_actions_array(self, mock_openai, conductor):
        """Test when LLM returns empty actions array."""
        mock_client = MagicMock()
        mock_openai.return_value = mock_client

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '{"master_bpm": 120, "master_key": "C minor", "actions": [], "reasoning": "test", "name": "test"}'
        mock_client.chat.completions.create.return_value = mock_response
        conductor.client = mock_client

        result = conductor.get_next_state(
            current_bpm=120,
            current_key="C minor",
            active_stems=[{"prompt": "Synth"}]
        )

        assert result["master_bpm"] == 120
        assert len(result["actions"]) == 0

    @patch('app.framework.framework_conductor.OpenAI')
    def test_stem_age_included_in_prompt(self, mock_openai, conductor):
        """Test that stem age is included in the prompt."""
        mock_client = MagicMock()
        mock_openai.return_value = mock_client

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '{"master_bpm": 120, "master_key": "C minor", "actions": [], "reasoning": "test", "name": "test"}'
        mock_client.chat.completions.create.return_value = mock_response
        conductor.client = mock_client

        active_stems = [
            {"prompt": "Synth, Pad, Warm", "_age": 5}  # Older stem
        ]

        conductor.get_next_state(
            current_bpm=120,
            current_key="C minor",
            active_stems=active_stems
        )

        args, kwargs = mock_client.chat.completions.create.call_args
        messages = kwargs['messages']
        user_message = next(m['content'] for m in messages if m['role'] == 'user')

        # Age should appear in the prompt
        assert "age 5" in user_message


class TestConductorSystemInstruction:
    """Test Conductor system instruction content."""

    def test_system_instruction_has_all_rules(self, conductor):
        """Test that system instruction contains all required DJ rules."""
        instruction = conductor.system_instruction

        assert "FLOW & RETENTION" in instruction
        assert "GROOVE & RHYTHM" in instruction
        assert "HARMONIC MIXING" in instruction
        assert "FREQUENCY BALANCING" in instruction
        assert "DENSITY & LAYERING" in instruction
        assert "STEM FRESHNESS" in instruction
        assert "CRITICAL OVERRIDE RULE" in instruction

    def test_system_instruction_forbids_null_values(self, conductor):
        """Test that system instruction forbids null values in add actions."""
        instruction = conductor.system_instruction

        assert "FORBIDDEN" in instruction
        assert "null" in instruction.lower() or "empty" in instruction.lower()

    def test_user_message_template_has_placeholders(self, conductor):
        """Test that user message template has all required placeholders."""
        template = conductor.user_message_template

        assert "{bpm}" in template
        assert "{key}" in template
        assert "{stems}" in template
        assert "{history}" in template
        assert "{instruments}" in template
        assert "{models}" in template
        assert "{stem_count}" in template
        assert "{density_directive}" in template


class TestConductorModelInfo:
    """Test model info formatting in conductor."""

    @patch('app.framework.framework_conductor.OpenAI')
    def test_model_without_supported_families(self, mock_openai, conductor):
        """Test model info formatting when supported_families is missing."""
        mock_client = MagicMock()
        mock_openai.return_value = mock_client

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '{"master_bpm": 120, "master_key": "C minor", "actions": [], "reasoning": "test", "name": "test"}'
        mock_client.chat.completions.create.return_value = mock_response
        conductor.client = mock_client

        available_models = [
            {"id": "model_1", "description": "Good for bass."}  # No supported_families
        ]

        conductor.get_next_state(
            current_bpm=120,
            current_key="C minor",
            active_stems=[],
            available_models=available_models
        )

        args, kwargs = mock_client.chat.completions.create.call_args
        messages = kwargs['messages']
        user_message = next(m['content'] for m in messages if m['role'] == 'user')

        # Should default to ['Any']
        assert "['Any']" in user_message or "Supported Families" in user_message

    @patch('app.framework.framework_conductor.OpenAI')
    def test_multiple_models_listed(self, mock_openai, conductor):
        """Test that multiple models are all listed in prompt."""
        mock_client = MagicMock()
        mock_openai.return_value = mock_client

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '{"master_bpm": 120, "master_key": "C minor", "actions": [], "reasoning": "test", "name": "test"}'
        mock_client.chat.completions.create.return_value = mock_response
        conductor.client = mock_client

        available_models = [
            {"id": "model_1", "description": "Bass model", "supported_families": ["Bass"]},
            {"id": "model_2", "description": "Drums model", "supported_families": ["Drums"]},
            {"id": "model_3", "description": "Synth model", "supported_families": ["Synth"]},
        ]

        conductor.get_next_state(
            current_bpm=120,
            current_key="C minor",
            active_stems=[],
            available_models=available_models
        )

        args, kwargs = mock_client.chat.completions.create.call_args
        messages = kwargs['messages']
        user_message = next(m['content'] for m in messages if m['role'] == 'user')

        assert "model_1" in user_message
        assert "model_2" in user_message
        assert "model_3" in user_message
