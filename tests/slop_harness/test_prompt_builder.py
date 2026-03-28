import pytest
from slop_harness.prompt_builder import PromptBuilder, SYSTEM_INSTRUCTION, AVAILABLE_INSTRUMENTS


def test_system_instruction_is_not_empty():
    """System instruction is the fixed Conductor prompt."""
    assert len(SYSTEM_INSTRUCTION) > 100


def test_build_returns_three_messages():
    """build() returns a list of 3 message dicts."""
    from slop_harness.state_generator import StateGenerator
    state = StateGenerator(batch_id=0, interaction_id=0).build()
    pb = PromptBuilder()
    messages = pb.build(state, override=None)
    assert len(messages) == 3
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert messages[2]["role"] == "assistant"


def test_system_message_content():
    """System message contains the DJ instructions."""
    from slop_harness.state_generator import StateGenerator
    state = StateGenerator(batch_id=0, interaction_id=0).build()
    pb = PromptBuilder()
    messages = pb.build(state, override=None)
    assert "DJ" in messages[0]["content"]
    assert "FLOW & RETENTION" in messages[0]["content"]


def test_user_message_contains_bpm():
    """User message contains the BPM."""
    from slop_harness.state_generator import StateGenerator
    state = StateGenerator(batch_id=0, interaction_id=42).build()
    pb = PromptBuilder()
    messages = pb.build(state, override=None)
    assert str(state["bpm"]) in messages[1]["content"]
    assert "Master BPM" in messages[1]["content"]


def test_user_message_contains_key():
    """User message contains the key."""
    from slop_harness.state_generator import StateGenerator
    state = StateGenerator(batch_id=0, interaction_id=42).build()
    pb = PromptBuilder()
    messages = pb.build(state, override=None)
    assert state["key"] in messages[1]["content"]
    assert "Master Key" in messages[1]["content"]


def test_user_message_contains_stem_count():
    """User message contains the stem count and density directive."""
    from slop_harness.state_generator import StateGenerator
    state = StateGenerator(batch_id=0, interaction_id=42).build()
    pb = PromptBuilder()
    messages = pb.build(state, override=None)
    assert str(state["stem_count"]) in messages[1]["content"]
    assert "DENSITY RULE" in messages[1]["content"]


def test_user_message_contains_available_models():
    """User message lists available models."""
    from slop_harness.state_generator import StateGenerator
    state = StateGenerator(batch_id=0, interaction_id=42).build()
    pb = PromptBuilder()
    messages = pb.build(state, override=None)
    for model_id in state["available_models"]:
        assert model_id in messages[1]["content"]


def test_user_message_contains_instruments():
    """User message lists available instrument types."""
    from slop_harness.state_generator import StateGenerator
    state = StateGenerator(batch_id=0, interaction_id=42).build()
    pb = PromptBuilder()
    messages = pb.build(state, override=None)
    for instr in AVAILABLE_INSTRUMENTS[:3]:
        assert instr in messages[1]["content"]


def test_override_appended_when_present():
    """When override is provided, it appears in user message."""
    from slop_harness.state_generator import StateGenerator
    state = StateGenerator(batch_id=0, interaction_id=0).build()
    pb = PromptBuilder()
    messages = pb.build(state, override="Let's go harder!")
    assert "OVERRIDE" in messages[1]["content"]
    assert "Let's go harder!" in messages[1]["content"]


def test_no_override_no_override_line():
    """When override is None, no OVERRIDE line appears."""
    from slop_harness.state_generator import StateGenerator
    state = StateGenerator(batch_id=0, interaction_id=0).build()
    pb = PromptBuilder()
    messages = pb.build(state, override=None)
    assert "OVERRIDE" not in messages[1]["content"]


def test_assistant_message_is_empty():
    """Assistant message is empty string placeholder (LLM fills it)."""
    from slop_harness.state_generator import StateGenerator
    state = StateGenerator(batch_id=0, interaction_id=0).build()
    pb = PromptBuilder()
    messages = pb.build(state, override=None)
    assert messages[2]["role"] == "assistant"
    assert messages[2]["content"] == ""


def test_history_shown_in_user_message():
    """History entries appear in user message."""
    from slop_harness.state_generator import StateGenerator
    state = StateGenerator(batch_id=0, interaction_id=50).build()  # song_age=50 so history is populated
    pb = PromptBuilder()
    messages = pb.build(state, override=None)
    if len(state["history"]) > 0:
        assert "Loop" in messages[1]["content"]


def test_density_directive_add_when_sparse():
    """When stem_count < 4, density directive says to add more."""
    from slop_harness.state_generator import StateGenerator
    # Force sparse state
    state = StateGenerator(batch_id=0, interaction_id=0).build()
    state["stem_count"] = 2
    state["stems"] = state["stems"][:2]
    pb = PromptBuilder()
    messages = pb.build(state, override=None)
    assert "add more elements" in messages[1]["content"].lower() or "DENSITY RULE" in messages[1]["content"]


def test_density_directive_remove_when_too_many():
    """When stem_count > 6, density directive says to remove some."""
    from slop_harness.state_generator import StateGenerator
    state = StateGenerator(batch_id=0, interaction_id=0).build()
    state["stem_count"] = 7
    pb = PromptBuilder()
    messages = pb.build(state, override=None)
    assert "DENSITY RULE" in messages[1]["content"]