#!/usr/bin/env python3
"""
Tests for DPO/RL pipeline for Conductor schema enforcement.
Following TDD: RED first (tests fail), GREEN (implement), REFACTOR.
"""

import json
import random

import pytest

# Import from the real implementation
from training.dpo_pipeline import (
    compute_schema_reward,
    generate_preference_pairs,
    validate_conductor_schema,
)

# Set seed for reproducibility in tests
random.seed(42)


# ============================================================
# RED Phase: Tests that should FAIL initially
# ============================================================


class TestSchemaValidator:
    """Tests for schema validation logic."""

    def test_valid_conductor_response_passes(self):
        """A fully valid Conductor response should pass validation."""
        response = {
            "master_bpm": 130,
            "master_key": "F minor",
            "actions": [
                {"action_type": "retain", "stem_index": 0},
                {
                    "action_type": "add",
                    "model_id": "foundation-1",
                    "major_family": "Drums",
                    "sub_family": "Electronic Drums",
                    "timbre_tags": ["hard"],
                    "notation_tag": "4/4",
                    "fx_tag": "dry",
                    "bars": 4,
                },
            ],
            "reasoning": "Keeping the bass, adding drums for rhythm",
            "name": "DJ Loop Decision",
        }
        assert validate_conductor_schema(response) is True

    def test_missing_required_field_fails(self):
        """Missing master_bpm should fail."""
        response = {"master_key": "F minor", "actions": [], "reasoning": "test", "name": "test"}
        assert validate_conductor_schema(response) is False

    def test_invalid_action_type_fails(self):
        """Invalid action_type should fail."""
        response = {
            "master_bpm": 130,
            "master_key": "F minor",
            "actions": [{"action_type": "INVALID_ACTION"}],
            "reasoning": "test",
            "name": "test",
        }
        assert validate_conductor_schema(response) is False

    def test_retain_without_stem_index_fails(self):
        """retain action must have stem_index."""
        response = {
            "master_bpm": 130,
            "master_key": "F minor",
            "actions": [{"action_type": "retain"}],
            "reasoning": "test",
            "name": "test",
        }
        assert validate_conductor_schema(response) is False

    def test_add_without_required_fields_fails(self):
        """add action must have model_id and major_family."""
        response = {
            "master_bpm": 130,
            "master_key": "F minor",
            "actions": [{"action_type": "add", "stem_index": 0}],
            "reasoning": "test",
            "name": "test",
        }
        assert validate_conductor_schema(response) is False

    def test_remove_without_stem_index_fails(self):
        """remove action must have stem_index."""
        response = {
            "master_bpm": 130,
            "master_key": "F minor",
            "actions": [{"action_type": "remove"}],
            "reasoning": "test",
            "name": "test",
        }
        assert validate_conductor_schema(response) is False

    def test_bpm_out_of_range_fails(self):
        """BPM outside 60-200 range should fail."""
        response = {"master_bpm": 250, "master_key": "F minor", "actions": [], "reasoning": "test", "name": "test"}
        assert validate_conductor_schema(response) is False

    def test_invalid_json_string_fails(self):
        """Malformed JSON string should fail."""
        invalid_json = '{"master_bpm": 130, "actions": []'  # missing closing brace
        assert validate_conductor_schema(invalid_json) is False

    def test_empty_actions_array_is_valid(self):
        """Empty actions array is valid (all stems removed)."""
        response = {
            "master_bpm": 130,
            "master_key": "F minor",
            "actions": [],
            "reasoning": "Clearing the deck",
            "name": "DJ Loop Decision",
        }
        assert validate_conductor_schema(response) is True


class TestPreferencePairGenerator:
    """Tests for preference pair generation from data."""

    def test_generates_winner_and_loser_from_valid_sample(self):
        """Given a valid sample, creates a chosen (valid) and rejected (corrupted) pair."""
        sample = create_valid_sample(bpm=128, key="C minor", num_actions=2)
        pairs = generate_preference_pairs([sample])

        assert len(pairs) == 1
        chosen, rejected = pairs[0]

        # Chosen should be valid JSON
        chosen_data = json.loads(chosen)
        assert validate_conductor_schema(chosen_data) is True

        # Rejected should be corrupted version (still parseable but schema invalid)
        # Rejected is intentionally corrupted, so validation may fail or it has schema issues

    def test_preserves_bpm_and_key_in_chosen(self):
        """Chosen (correct) sample preserves original BPM and key."""
        sample = create_valid_sample(bpm=140, key="D major", num_actions=1)
        pairs = generate_preference_pairs([sample])

        chosen_data = json.loads(pairs[0][0])
        assert chosen_data["master_bpm"] == 140
        assert chosen_data["master_key"] == "D major"

    def test_rejected_corrupted_in_specific_ways(self):
        """Rejected samples should be corrupted in known ways for DPO learning."""
        sample = create_valid_sample(bpm=128, key="C minor", num_actions=2)
        pairs = generate_preference_pairs(
            [sample], corruption_types=["missing_field", "invalid_enum"], num_corruptions_per_sample=2
        )

        assert len(pairs) == 2  # Two corruption types = two pairs

        # Each rejected should be parseable but schema-invalid
        for chosen, rejected in pairs:
            # Both should be valid JSON strings
            assert isinstance(rejected, str)
            rejected_data = json.loads(rejected)
            # Chosen is valid, rejected is corrupted
            assert validate_conductor_schema(rejected_data) is False

    def test_handles_list_of_samples(self):
        """Can process multiple samples at once."""
        samples = [
            create_valid_sample(bpm=120, key="A minor", num_actions=1),
            create_valid_sample(bpm=130, key="G major", num_actions=2),
        ]
        pairs = generate_preference_pairs(samples)
        assert len(pairs) >= 2


class TestRewardFunctions:
    """Tests for reward computation during RL."""

    def test_valid_json_gets_high_reward(self):
        """Valid JSON matching schema gets positive reward."""
        valid_response = (
            '{"master_bpm": 130, "master_key": "F minor", "actions": [], "reasoning": "test", "name": "test"}'
        )
        reward = compute_schema_reward(valid_response)
        assert reward > 0

    def test_invalid_json_gets_negative_reward(self):
        """Invalid JSON gets negative reward."""
        invalid_response = '{"master_bpm": 130, "actions": []'  # malformed
        reward = compute_schema_reward(invalid_response)
        assert reward < 0

    def test_valid_but_wrong_schema_gets_negative_reward(self):
        """Valid JSON but wrong schema gets negative reward."""
        wrong_schema = '{"temperature": 72, "actions": []}'  # missing required fields
        reward = compute_schema_reward(wrong_schema)
        assert reward < 0

    def test_composite_reward_with_validity_weight(self):
        """Composite reward correctly weights validity vs quality."""
        valid_response = (
            '{"master_bpm": 130, "master_key": "F minor", '
            '"actions": [{"action_type": "retain", "stem_index": 0}], '
            '"reasoning": "Keeping bass", "name": "DJ Loop Decision"}'
        )
        reward = compute_schema_reward(valid_response, validity_weight=1.0, quality_weight=0.0)
        assert reward > 0

        # With quality weight, longer reasoning might score higher
        reward_with_quality = compute_schema_reward(valid_response, validity_weight=0.5, quality_weight=0.5)
        assert isinstance(reward_with_quality, float)


# ============================================================
# Helper functions for tests
# ============================================================


def create_valid_sample(bpm=128, key="C minor", num_actions=1):
    """Create a valid training sample."""
    actions = []
    for i in range(num_actions):
        actions.append(
            {
                "action_type": "retain" if i == 0 else "add",
                "stem_index": i,
                "model_id": "foundation-1",
                "major_family": "Drums",
                "sub_family": "Electronic Drums",
                "timbre_tags": ["hard", "punchy"],
                "notation_tag": "4/4",
                "fx_tag": "dry",
                "bars": 4,
            }
        )

    return {
        "messages": [
            {"role": "system", "content": "You are an AI DJ..."},
            {"role": "user", "content": "Current State...\nYOUR TASK..."},
            {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "master_bpm": bpm,
                        "master_key": key,
                        "actions": actions,
                        "reasoning": "Test reasoning",
                        "name": "DJ Loop Decision",
                    }
                ),
            },
        ]
    }


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
