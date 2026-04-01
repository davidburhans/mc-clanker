#!/usr/bin/env python3
"""
DPO/RL Pipeline for Conductor Schema Enforcement.

This module provides:
1. Schema validation for Conductor JSON responses
2. Preference pair generation for DPO training
3. Reward functions for RLHF

Reusable pipeline: SFT -> DPO -> RL with schema validation rewards.
"""

import json
import random
from typing import List, Tuple, Dict, Any, Optional


# ============================================================
# Schema Constants
# ============================================================

VALID_ACTION_TYPES = {"retain", "add", "remove"}
VALID_MAJOR_FAMILIES = {
    "Drums", "Bass", "Synth", "Keys",
    "Bowed Strings", "Mallet", "Wind", "Guitar",
    "Brass", "Vocal", "Plucked Strings"
}
VALID_BPM_RANGE = (60, 200)
VALID_BARS_RANGE = (1, 16)

REQUIRED_RESPONSE_FIELDS = {"master_bpm", "master_key", "actions", "reasoning", "name"}


# ============================================================
# Schema Validation
# ============================================================

def validate_conductor_schema(response) -> bool:
    """
    Validate a Conductor response against the DJ schema.

    Args:
        response: Either a dict or a JSON string

    Returns:
        True if valid, False otherwise
    """
    # Handle string input
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except json.JSONDecodeError:
            return False

    # Must be a dict
    if not isinstance(response, dict):
        return False

    # Check required fields
    if not REQUIRED_RESPONSE_FIELDS.issubset(response.keys()):
        return False

    # Validate master_bpm
    bpm = response.get("master_bpm")
    if not isinstance(bpm, int) or not VALID_BPM_RANGE[0] <= bpm <= VALID_BPM_RANGE[1]:
        return False

    # Validate master_key
    key = response.get("master_key")
    if not isinstance(key, str) or not key.strip():
        return False

    # Validate actions array
    actions = response.get("actions", [])
    if not isinstance(actions, list):
        return False

    for action in actions:
        if not _validate_action(action):
            return False

    # Validate reasoning
    reasoning = response.get("reasoning")
    if not isinstance(reasoning, str):
        return False

    # Validate name
    name = response.get("name")
    if not isinstance(name, str):
        return False

    return True


def _validate_action(action: dict) -> bool:
    """Validate a single action object."""
    if not isinstance(action, dict):
        return False

    action_type = action.get("action_type")
    if action_type not in VALID_ACTION_TYPES:
        return False

    # retain and remove require stem_index
    if action_type in ("retain", "remove"):
        stem_index = action.get("stem_index")
        if not isinstance(stem_index, int) or stem_index < 0:
            return False

    # add requires additional fields
    if action_type == "add":
        if not isinstance(action.get("model_id"), str):
            return False
        if not isinstance(action.get("major_family"), str):
            return False
        if not isinstance(action.get("sub_family"), str):
            return False
        if not isinstance(action.get("timbre_tags"), list):
            return False
        if not isinstance(action.get("notation_tag"), str):
            return False
        if not isinstance(action.get("fx_tag"), str):
            return False
        bars = action.get("bars")
        if not isinstance(bars, int) or not VALID_BARS_RANGE[0] <= bars <= VALID_BARS_RANGE[1]:
            return False

    return True


# ============================================================
# Corruption Functions for Preference Pairs
# ============================================================

def _corrupt_response(response: dict, corruption_type: str) -> dict:
    """Apply a corruption strategy to a response."""
    # Deep copy to avoid modifying original
    corrupted = json.loads(json.dumps(response))

    strategy = CORRUPTION_STRATEGIES.get(corruption_type)
    if strategy:
        return strategy(corrupted)
    return corrupted


def _corrupt_missing_field(response: dict) -> dict:
    """Remove a required field."""
    fields_to_remove = ["master_bpm", "master_key", "reasoning", "name"]
    field = random.choice(fields_to_remove)
    response.pop(field, None)
    return response


def _corrupt_invalid_enum(response: dict) -> dict:
    """Replace action_type with invalid value."""
    if not response.get("actions"):
        response["actions"].append({"action_type": "retain", "stem_index": 0})

    action = random.choice(response["actions"])
    action["action_type"] = "INVALID_ACTION"
    return response


def _corrupt_invalid_bpm(response: dict) -> dict:
    """Set BPM out of valid range."""
    invalid_bpms = [250, 30, 0, -10, 500]
    response["master_bpm"] = random.choice(invalid_bpms)
    return response


def _corrupt_truncated_json(response: dict) -> dict:
    """Return a truncated JSON string representation."""
    json_str = json.dumps(response)
    # Remove last 20-50% of characters
    truncate_at = random.randint(len(json_str) // 2, int(len(json_str) * 0.8))
    try:
        return json.loads(json_str[:truncate_at])
    except json.JSONDecodeError:
        # Return invalid state by removing closing brace
        return json.loads(json_str[:truncate_at] + '"}')


def _corrupt_extra_field(response: dict) -> dict:
    """Add an invalid field that conflicts with schema."""
    # Replace valid action_type with one that has wrong fields
    if response.get("actions"):
        action = response["actions"][0]
        if action.get("action_type") == "add":
            action.pop("model_id", None)
            action.pop("major_family", None)
    return response


# Corruption strategies dict - defined after all functions
CORRUPTION_STRATEGIES = {
    "missing_field": _corrupt_missing_field,
    "invalid_enum": _corrupt_invalid_enum,
    "invalid_bpm": _corrupt_invalid_bpm,
    "truncated_json": _corrupt_truncated_json,
    "extra_field": _corrupt_extra_field,
}


# ============================================================
# Preference Pair Generation for DPO
# ============================================================

def generate_preference_pairs(
    samples: List[Dict],
    corruption_types: Optional[List[str]] = None,
    num_corruptions_per_sample: int = 1
) -> List[Tuple[str, str]]:
    """
    Generate chosen/rejected pairs for DPO training.

    Given a sample with valid Conductor output, creates:
    - chosen: the original (valid) response
    - rejected: a corrupted version with schema violations

    Args:
        samples: List of samples from the SFT dataset
        corruption_types: List of corruption strategies to apply
        num_corruptions_per_sample: Number of pairs per sample

    Returns:
        List of (chosen_json_str, rejected_json_str) tuples
    """
    if corruption_types is None:
        corruption_types = ["missing_field", "invalid_enum"]

    pairs = []
    for sample in samples:
        assistant_msg = _extract_assistant_message(sample)
        if not assistant_msg:
            continue

        try:
            original = json.loads(assistant_msg)
        except json.JSONDecodeError:
            continue

        if not validate_conductor_schema(original):
            continue  # Skip already invalid samples

        # Create multiple corruption pairs per sample
        used_corruptions = set()
        for _ in range(num_corruptions_per_sample):
            corruption_type = random.choice(corruption_types)
            if len(used_corruptions) >= len(corruption_types):
                used_corruptions.clear()

            corrupted = _corrupt_response(original, corruption_type)
            pairs.append((json.dumps(original), json.dumps(corrupted)))
            used_corruptions.add(corruption_type)

    return pairs


def _extract_assistant_message(sample: Dict) -> Optional[str]:
    """Extract the assistant message content from a sample."""
    messages = sample.get("messages", [])
    for msg in messages:
        if msg.get("role") == "assistant":
            return msg.get("content", "")
    return None


# ============================================================
# Reward Functions for RL
# ============================================================

def compute_schema_reward(
    response: str,
    validity_weight: float = 1.0,
    quality_weight: float = 0.0
) -> float:
    """
    Compute reward for a Conductor response.

    Args:
        response: JSON string response
        validity_weight: Weight for schema validity (0.0 to 1.0)
        quality_weight: Weight for quality signals (0.0 to 1.0)

    Returns:
        Reward float (positive for good, negative for bad)
    """
    # Parse JSON
    try:
        data = json.loads(response)
    except json.JSONDecodeError:
        return -1.0

    # Validity reward
    is_valid = validate_conductor_schema(data)
    validity_reward = 1.0 if is_valid else -1.0

    # Quality reward (simple heuristics)
    quality_reward = _compute_quality_reward(data)

    # Composite reward
    total = validity_weight + quality_weight
    if total == 0:
        total = 1.0

    reward = (validity_reward * validity_weight + quality_reward * quality_weight) / total

    # If validity_weight dominates, ensure proper scaling
    if validity_weight > quality_weight:
        reward *= validity_weight  # Scale up when validity is prioritized

    return reward


def _compute_quality_reward(data: dict) -> float:
    """
    Compute quality-based reward signals.

    Heuristics:
    - Has reasoning: +0.1
    - Reasoning is non-trivial length: +0.1
    - Has 1-6 actions (good density): +0.2
    - Action diversity (mix of retain/add/remove): +0.1
    """
    reward = 0.0

    # Has reasoning
    reasoning = data.get("reasoning", "")
    if reasoning and len(reasoning) > 5:
        reward += 0.1
    if len(reasoning) > 30:
        reward += 0.1

    # Action density
    num_actions = len(data.get("actions", []))
    if 1 <= num_actions <= 6:
        reward += 0.2

    # Action diversity
    action_types = {a.get("action_type") for a in data.get("actions", [])}
    if len(action_types) > 1:
        reward += 0.1

    return reward


# ============================================================
# CLI for generating DPO dataset
# ============================================================

def generate_dpo_dataset_from_sft(
    sft_dataset_path: str,
    output_path: str,
    corruption_types: Optional[List[str]] = None,
    num_corruptions_per_sample: int = 1,
    max_samples: Optional[int] = None
) -> int:
    """
    Generate a DPO dataset from an SFT dataset.

    Args:
        sft_dataset_path: Path to the SFT dataset (from_disk)
        output_path: Path to save the DPO dataset
        corruption_types: List of corruption strategies to use
        num_corruptions_per_sample: Number of pairs per sample
        max_samples: Maximum number of samples to process

    Returns:
        Number of pairs generated
    """
    from datasets import load_from_disk, Dataset

    dataset = load_from_disk(sft_dataset_path)
    samples = dataset["train"]

    if max_samples:
        samples = samples.select(range(min(max_samples, len(samples))))

    pairs = generate_preference_pairs(
        list(samples),
        corruption_types=corruption_types,
        num_corruptions_per_sample=num_corruptions_per_sample
    )

    # Convert to DPO format
    dpo_data = []
    for chosen, rejected in pairs:
        dpo_data.append({
            "chosen": chosen,
            "rejected": rejected,
        })

    # Save
    dpo_dataset = Dataset.from_list(dpo_data)
    dpo_dataset.save_to_disk(output_path)

    return len(pairs)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate DPO dataset from SFT data")
    parser.add_argument("--sft-path", required=True, help="Path to SFT dataset")
    parser.add_argument("--output-path", required=True, help="Output path for DPO dataset")
    parser.add_argument("--corruption-types", nargs="+", default=["missing_field", "invalid_enum"])
    parser.add_argument("--max-samples", type=int, default=None)
    args = parser.parse_args()

    num_pairs = generate_dpo_dataset_from_sft(
        args.sft_path,
        args.output_path,
        args.corruption_types,
        max_samples=args.max_samples
    )
    print(f"Generated {num_pairs} preference pairs")
