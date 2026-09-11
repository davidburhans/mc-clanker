"""Round-3 lane C — Conductor parsing / action-semantics hardening.

Covers review ``round3/08_conductor.md`` findings C1-C6:

* C1 ``parse_llm_json_response`` accepted ANY valid JSON (top-level list/scalar
  escaped the P4 fallback and hot-retried the LLM forever).
* C2 an empty / fully-unrecognized ``actions`` list emptied the mix in silence.
* C3 type confusion inside ``process_actions`` (string/float/``None`` indices,
  ``timbre_tags: null``) raised out of P5 and wedged the loop.
* C4 ``retain`` + ``remove`` of the same index both survived AND both were audited.
* C5 a non-harmonic ``master_key`` bricked the conductor prompt builder.
* C6 prompt typo ("if one are not already playing!").
"""

from __future__ import annotations

import pytest

from app.framework.conductor_interaction import format_action_log, process_actions
from app.framework.framework_conductor_async import (
    ConductorLLMAsync,
    ConductorPromptBuilder,
    harmonic_neighbor_prompt_text,
    parse_llm_json_response,
)

# --------------------------------------------------------------------------- #
# Fakes (no network, no LLM)
# --------------------------------------------------------------------------- #


class _FakeMessage:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str | None) -> None:
        self.message = _FakeMessage(content)


class _FakeCompletion:
    def __init__(self, content: str | None) -> None:
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, owner: "_FakeAsyncLLMClient") -> None:
        self._owner = owner

    async def create(self, **kwargs):
        self._owner.calls.append(kwargs)
        return _FakeCompletion(self._owner.content)


class _FakeChat:
    def __init__(self, owner: "_FakeAsyncLLMClient") -> None:
        self.completions = _FakeCompletions(owner)


class _FakeAsyncLLMClient:
    """Named fake for ``openai.AsyncOpenAI`` — replays one fixed message content."""

    def __init__(self, content: str | None) -> None:
        self.content = content
        self.calls: list[dict] = []
        self.chat = _FakeChat(self)


def _wire_conductor(monkeypatch, content: str | None) -> tuple[ConductorLLMAsync, _FakeAsyncLLMClient]:
    """Return a conductor wired to a fake client plus the fake (for call inspection)."""
    conductor = ConductorLLMAsync(api_base="http://llm.invalid", model_name="test-model")
    fake = _FakeAsyncLLMClient(content)
    monkeypatch.setattr(conductor, "_get_async_client", lambda config=None: fake)
    return conductor, fake


def _stem(sub_family: str, age: int = 0) -> dict:
    return {
        "prompt": f"Drums, {sub_family}, 120 BPM",
        "_age": age,
        "_original_details": {
            "model_id": "foundation-1",
            "major_family": "Synth" if "Drum" not in sub_family else "Drums",
            "sub_family": sub_family,
            "timbre_tags": ["warm"],
            "notation_tag": "melody",
            "fx_tag": "dry",
            "bars": 4,
            "_age": age,
        },
    }


# --------------------------------------------------------------------------- #
# C1 — parse_llm_json_response must return a dict or raise
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "content",
    [
        '[{"action_type": "retain", "stem_index": 0}]',
        "null",
        "5",
        '"keep the pads"',
        "true",
    ],
)
def test_c1_non_object_json_raises_value_error(content: str) -> None:
    with pytest.raises(ValueError, match="Could not parse JSON from LLM response"):
        parse_llm_json_response(content)


def test_c1_fenced_list_raises_value_error() -> None:
    with pytest.raises(ValueError):
        parse_llm_json_response('```json\n[{"action_type": "retain"}]\n```')


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ('{"actions": []}', {"actions": []}),
        ('```json\n{"actions": []}\n```', {"actions": []}),
        ('Here you go:\n{"actions": [{"action_type": "retain", "stem_index": 1}]}\nbye', None),
    ],
)
def test_c1_object_responses_still_parse(content: str, expected: dict | None) -> None:
    parsed = parse_llm_json_response(content)
    assert isinstance(parsed, dict)
    if expected is not None:
        assert parsed == expected


async def test_c1_call_async_degrades_to_value_error_not_attribute_error(monkeypatch) -> None:
    """P4's fallback catches ValueError — a list response must reach it that way."""
    conductor, _fake = _wire_conductor(monkeypatch, '[{"action_type": "retain", "stem_index": 0}]')
    with pytest.raises(ValueError, match="Could not parse JSON from LLM"):
        await conductor.call_async("prompt")


async def test_c1_call_async_retries_are_bounded(monkeypatch) -> None:
    conductor, fake = _wire_conductor(monkeypatch, "null")
    with pytest.raises(ValueError):
        await conductor.call_async("prompt", max_retries=3)
    assert len(fake.calls) == 3


# --------------------------------------------------------------------------- #
# C2 — no decision must never mean "remove everything"
# --------------------------------------------------------------------------- #


def test_c2_empty_actions_list_retains_every_stem() -> None:
    stems = [_stem("Electronic Drums"), _stem("Synth Bass")]
    result = process_actions([], stems)
    assert [t["sub_family"] for t in result] == ["Electronic Drums", "Synth Bass"]
    assert [t["_age"] for t in result] == [1, 1]


def test_c2_empty_actions_list_logs_a_warning(caplog) -> None:
    with caplog.at_level("WARNING"):
        process_actions([], [_stem("Electronic Drums")])
    assert any("conductor_decision_left_no_stems" in record.getMessage() for record in caplog.records)


def test_c2_empty_actions_with_no_active_stems_stays_empty() -> None:
    assert process_actions([], []) == []


def test_c2_misnamed_discriminator_key_keeps_the_mix_playing() -> None:
    """CLAUDE.md documents ``{"action": ...}`` — it must not silence the set."""
    stems = [_stem("Electronic Drums"), _stem("Synth Bass")]
    result = process_actions([{"action": "retain", "stem_index": 0}], stems)
    assert len(result) == 2


def test_c2_unknown_action_type_is_skipped_with_warning(caplog) -> None:
    stems = [_stem("Electronic Drums"), _stem("Synth Bass")]
    actions = [
        {"action_type": "crossfade", "stem_index": 0},
        {"action_type": "retain", "stem_index": 1},
    ]
    with caplog.at_level("WARNING"):
        result = process_actions(actions, stems)
    assert [t["sub_family"] for t in result] == ["Synth Bass"]
    assert any("conductor_action_rejected" in record.getMessage() for record in caplog.records)


# --------------------------------------------------------------------------- #
# C3 — type confusion must degrade, never raise
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw_index", ["1", 1.0, True])
def test_c3_stem_index_types_never_raise(raw_index) -> None:
    stems = [_stem("Electronic Drums"), _stem("Synth Bass")]
    result = process_actions([{"action_type": "retain", "stem_index": raw_index}], stems)
    assert isinstance(result, list)


def test_c3_string_and_float_indices_are_coerced() -> None:
    stems = [_stem("Electronic Drums"), _stem("Synth Bass")]
    assert process_actions([{"action_type": "retain", "stem_index": "1"}], stems)[0]["sub_family"] == "Synth Bass"
    assert process_actions([{"action_type": "retain", "stem_index": 0.0}], stems)[0]["sub_family"] == "Electronic Drums"


def test_c3_junk_action_elements_are_skipped() -> None:
    stems = [_stem("Electronic Drums"), _stem("Synth Bass")]
    actions = [None, "retain", 7, {"action_type": "retain", "stem_index": 0}]
    result = process_actions(actions, stems)
    assert [t["sub_family"] for t in result] == ["Electronic Drums"]


def test_c3_invalid_remove_index_is_skipped_not_applied_to_everyone() -> None:
    stems = [_stem("Electronic Drums"), _stem("Synth Bass")]
    actions = [{"action_type": "remove", "stem_index": "banana"}, {"action_type": "retain", "stem_index": 0}]
    assert [t["sub_family"] for t in process_actions(actions, stems)] == ["Electronic Drums"]


def test_c3_bool_stem_index_is_rejected() -> None:
    stems = [_stem("Electronic Drums"), _stem("Synth Bass")]
    actions = [{"action_type": "remove", "stem_index": True}, {"action_type": "retain", "stem_index": 1}]
    assert [t["sub_family"] for t in process_actions(actions, stems)] == ["Synth Bass"]


def test_c3_add_with_null_timbre_tags_falls_back_to_defaults() -> None:
    action = {
        "action_type": "add",
        "major_family": "Synth",
        "sub_family": "Pad",
        "timbre_tags": None,
        "notation_tag": None,
        "fx_tag": None,
        "bars": None,
        "model_id": "foundation-1",
    }
    [track] = process_actions([action], [])
    assert track["timbre_tags"] == ["Warm"]
    assert track["notation_tag"] == "melody"
    assert track["fx_tag"] == "Medium Reverb"
    assert track["bars"] == 4
    assert track["_age"] == 0


def test_c3_bare_add_action_needs_no_instrument_fields() -> None:
    [track] = process_actions([{"action_type": "add"}], [])
    assert track["sub_family"] == "Synth Lead"
    assert track["major_family"] == "Synth"


def test_c3_dedup_never_raises_on_null_timbre_tags_in_retained_stems() -> None:
    stem = _stem("Electronic Drums")
    stem["_original_details"]["timbre_tags"] = None
    assert len(process_actions([{"action_type": "retain", "stem_index": 0}], [stem])) == 1


def test_c3_format_action_log_survives_junk_elements() -> None:
    stems = [_stem("Electronic Drums"), _stem("Synth Bass")]
    actions = [None, "retain", {"action_type": "retain", "stem_index": "1"}, {"action_type": "add", "sub_family": None}]
    assert format_action_log(actions, stems) == ["Retained Synth Bass", "Added Synth Lead"]


# --------------------------------------------------------------------------- #
# C4 — remove wins, and the audit says so (only)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "actions",
    [
        [{"action_type": "retain", "stem_index": 0}, {"action_type": "remove", "stem_index": 0}],
        [{"action_type": "remove", "stem_index": 0}, {"action_type": "retain", "stem_index": 0}],
    ],
)
def test_c4_remove_wins_over_retain_in_both_orders(actions) -> None:
    stems = [_stem("Electronic Drums"), _stem("Synth Bass")]
    actions = actions + [{"action_type": "retain", "stem_index": 1}]
    result = process_actions(actions, stems)
    assert [t["sub_family"] for t in result] == ["Synth Bass"]


def test_c4_conflicted_stem_is_not_aged_or_resurrected() -> None:
    stems = [_stem("Electronic Drums", age=3), _stem("Synth Bass")]
    actions = [{"action_type": "retain", "stem_index": 0}, {"action_type": "remove", "stem_index": 0}]
    result = process_actions(actions, stems)
    assert stems[0]["_original_details"]["_age"] == 3
    assert [t["sub_family"] for t in result] == ["Synth Bass"]


def test_c2_explicit_removal_of_every_stem_is_still_honored() -> None:
    stems = [_stem("Electronic Drums"), _stem("Synth Bass")]
    actions = [{"action_type": "remove", "stem_index": 0}, {"action_type": "remove", "stem_index": 1}]
    assert process_actions(actions, stems) == []


def test_c4_audit_log_reports_only_the_effective_action(caplog) -> None:
    stems = [_stem("Electronic Drums"), _stem("Synth Bass")]
    actions = [
        {"action_type": "retain", "stem_index": 0},
        {"action_type": "remove", "stem_index": 0},
        {"action_type": "retain", "stem_index": 1},
    ]
    lines = format_action_log(actions, stems)
    assert lines == ["Removed Electronic Drums", "Retained Synth Bass"]


# --------------------------------------------------------------------------- #
# C5 — a non-harmonic master_key must not brick the conductor
# --------------------------------------------------------------------------- #


def test_c5_valid_key_neighbors_are_unchanged() -> None:
    assert harmonic_neighbor_prompt_text("A minor") == "C major, D minor, or E minor"


def test_c5_unknown_master_key_does_not_raise_and_keeps_prompt_building() -> None:
    prompt = ConductorPromptBuilder.build_prompt(
        current_bpm=128,
        current_key="Potato",
        active_stems=[_stem("Electronic Drums")],
    )
    assert "C major, D minor, or E minor" in prompt
    assert "Master Key: Potato" in prompt


def test_c5_unknown_master_key_logs_a_warning(caplog) -> None:
    with caplog.at_level("WARNING"):
        harmonic_neighbor_prompt_text("not-a-key")
    assert any("conductor_master_key_not_harmonic" in record.getMessage() for record in caplog.records)


async def test_c5_get_next_state_async_survives_a_bricked_current_key(monkeypatch) -> None:
    conductor, fake = _wire_conductor(monkeypatch, '{"actions": [], "reasoning": "ok"}')
    response = await conductor.get_next_state_async(current_bpm=120, current_key="Potato", active_stems=[])
    # U4: get_next_state_async attaches the _request_messages transport key
    # (the exact chat it sent) alongside the parsed model output — compare the
    # model output only (the transport key itself is pinned by test_llm_capture T9).
    assert {k: v for k, v in response.items() if not k.startswith("_")} == {"actions": [], "reasoning": "ok"}
    prompt = fake.calls[0]["messages"][-1]["content"]
    assert "Master Key: Potato" in prompt


# --------------------------------------------------------------------------- #
# C6 — prompt typo
# --------------------------------------------------------------------------- #


def test_c6_drums_line_grammar_is_fixed() -> None:
    conductor = ConductorLLMAsync(api_base="http://llm.invalid")
    assert "if one are not" not in conductor.user_message_template
    assert "if one are not" not in conductor.system_instruction
    assert "if one is not already playing" in conductor.user_message_template
