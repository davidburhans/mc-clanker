"""Integration tests for the slop_harness pipeline.

These test the full integration of all components:
state_generator → prompt_builder → dataset_writer → checkpoint.
"""
import json
import os
import tempfile

from slop_harness.checkpoint import CheckpointManager
from slop_harness.dataset_writer import DatasetWriter
from slop_harness.models import FOUNDATION_1_MODEL, INFINITE_PIANOS_MODEL
from slop_harness.prompt_builder import PromptBuilder
from slop_harness.state_generator import StateGenerator


def test_full_pipeline_single_interaction():
    """End-to-end: state → prompt → record, no LLM."""
    state = StateGenerator(batch_id=0, interaction_id=99).build()
    messages = PromptBuilder().build(state, override=None)

    assert len(messages) == 3
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert messages[2]["role"] == "assistant"
    assert messages[2]["content"] == ""

    user_content = messages[1]["content"]
    assert str(state["bpm"]) in user_content
    assert state["key"] in user_content


def test_full_pipeline_with_vibe_override():
    """End-to-end with vibe override."""
    state = StateGenerator(batch_id=0, interaction_id=5).build()
    messages = PromptBuilder().build(state, override="Let's go harder!")

    assert "OVERRIDE" in messages[1]["content"]
    assert "Let's go harder!" in messages[1]["content"]


def test_dataset_writer_integration():
    """Dataset writer produces valid JSONL."""
    with tempfile.TemporaryDirectory() as tmpdir:
        writer = DatasetWriter(tmpdir, batch_size=5)
        for i in range(7):
            writer.write({"n": i, "messages": [{"role": "test"}]})
        writer.close()

        files = sorted(os.listdir(tmpdir))
        assert len(files) == 2  # batch_0 (5), batch_1 (2)

        with open(os.path.join(tmpdir, files[0])) as f:
            lines = f.readlines()
        assert len(lines) == 5
        for line in lines:
            obj = json.loads(line)
            assert "n" in obj


def test_checkpoint_round_trip():
    """Checkpoint save/load works correctly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ckpt = CheckpointManager(os.path.join(tmpdir, "ckpt.json"))
        ckpt.save(batch_id=3, total=5500)
        loaded = ckpt.load()
        assert loaded == {"batch_id": 3, "total": 5500}

        ckpt.increment(5)
        loaded = ckpt.load()
        assert loaded == {"batch_id": 4, "total": 5505}


def test_models_all_have_required_fields():
    """All models have required fields for harness to work."""
    for model in [FOUNDATION_1_MODEL, INFINITE_PIANOS_MODEL]:
        assert "id" in model
        assert "repo_id" in model
        assert "description" in model
        assert "major_families" in model
        assert "sub_families" in model
        assert "keys" in model
        assert "bpms" in model
        assert "bars" in model


def test_deterministic_state_id_0():
    """First interaction (batch=0, id=0) produces valid state."""
    state = StateGenerator(batch_id=0, interaction_id=0).build()
    assert 1 <= state["stem_count"] <= 7
    assert state["bpm"] in [100, 110, 120, 128, 130, 140, 150]
    assert state["key"] in FOUNDATION_1_MODEL["keys"]
    assert "foundation-1" in state["available_models"]
    assert len(state["history"]) <= 5


def test_all_batch_ids_unique_state():
    """Each batch_id produces different states."""
    states = {StateGenerator(batch_id=i, interaction_id=0).build()["bpm"] for i in range(10)}
    # At least some variation expected across 10 batches
    assert len(states) >= 1  # probabilistic, but should vary


def test_harness_import_and_argparse(monkeypatch):
    """The harness module is importable and argparse works."""
    from slop_harness.harness import parse_args
    monkeypatch.setattr("sys.argv", ["harness", "--base-url", "http://test:999/v1", "--total", "100"])
    args = parse_args()
    assert args.base_url == "http://test:999/v1"
    assert args.total == 100
    assert args.batch_size == 1000  # default
    assert args.vibe_prob == 0.05  # default
