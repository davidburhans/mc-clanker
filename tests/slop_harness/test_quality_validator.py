"""Tests for slop_harness.quality_validator — automated dataset quality validation.

Covers all 5 validation checks:
1) JSON schema validation
2) Diversity metrics (BPM, keys, instrument coverage)
3) Duplicate detection
4) Action validity (bounds & type checking)
5) Vibe override persistence
"""

import json

import pytest

# The production module `slop_harness/quality_validator.py` imports
# `ALL_BPMS, ALL_INSTRUMENTS, ALL_KEYS` from `slop_harness/models.py`, but that
# module only exports `ALL_MODELS` — so the import itself raises ImportError.
# This is a source-side bug OUTSIDE the test suite's scope (slop_harness/ is not
# test-owned). Guarding here lets the WHOLE suite collect & run instead of the
# single broken import aborting pytest collection for every file.
# See synthesis D-section (collection error).
from typing import Any

QualityValidator: Any
QualityThresholdError: Any
ValidationResult: Any
try:
    from slop_harness.quality_validator import (
        QualityThresholdError,
        QualityValidator,
        ValidationResult,
    )

    _QV_IMPORT_ERROR = None
except Exception as exc:  # noqa: BLE001 — surface the real cause in the skip
    QualityThresholdError = QualityValidator = ValidationResult = None
    _QV_IMPORT_ERROR = exc

pytestmark = pytest.mark.skipif(
    QualityValidator is None,
    reason=(
        "slop_harness.quality_validator cannot be imported: "
        f"{_QV_IMPORT_ERROR!r}. Source bug: slop_harness/models.py does not "
        "export ALL_BPMS/ALL_INSTRUMENTS/ALL_KEYS (required by quality_validator)."
    ),
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def validator():
    return QualityValidator()


@pytest.fixture
def valid_record():
    """Minimum valid record."""
    return {
        "messages": [
            {"role": "system", "content": "You are an AI DJ."},
            {"role": "user", "content": "Current state: ..."},
            {"role": "assistant", "content": ""},
        ],
        "response": json.dumps(
            {
                "master_bpm": 120,
                "master_key": "C major",
                "actions": [
                    {"action_type": "retain", "stem_index": 0},
                ],
                "reasoning": "Keeping the groove.",
                "name": "Test",
            }
        ),
    }


# ── 1) JSON Schema Validation ────────────────────────────────────────────────


class TestSchemaValidation:
    def test_valid_record_passes(self, validator, valid_record):
        result = validator._validate_single(valid_record)
        assert result.valid
        assert not result.errors

    def test_missing_messages_field(self, validator):
        record = {"response": "{}"}
        result = validator._validate_single(record)
        assert not result.valid
        assert any("missing 'messages'" in e for e in result.errors)

    def test_messages_not_list(self, validator):
        record = {"messages": "not-a-list", "response": "{}"}
        result = validator._validate_single(record)
        assert not result.valid
        assert any("not a list" in e for e in result.errors)

    def test_too_few_messages(self, validator):
        record = {
            "messages": [{"role": "system", "content": "hi"}],
            "response": "{}",
        }
        result = validator._validate_single(record)
        assert not result.valid
        assert any("expected at least 3" in e for e in result.errors)

    def test_missing_response(self, validator):
        record = {
            "messages": [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "u"},
                {"role": "assistant", "content": ""},
            ]
        }
        result = validator._validate_single(record)
        assert not result.valid
        assert any("missing 'response'" in e for e in result.errors)

    def test_response_invalid_json(self, validator):
        record = {
            "messages": [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "u"},
                {"role": "assistant", "content": ""},
            ],
            "response": "{not valid json!!!",
        }
        result = validator._validate_single(record)
        assert not result.valid
        assert any("not valid JSON" in e for e in result.errors)

    def test_response_missing_required_fields(self, validator):
        record = {
            "messages": [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "u"},
                {"role": "assistant", "content": ""},
            ],
            "response": json.dumps({"master_bpm": 120}),
        }
        result = validator._validate_single(record)
        assert not result.valid
        # Should be missing master_key, actions, reasoning, name
        assert len([e for e in result.errors if "missing" in e]) >= 4

    def test_empty_actions_array(self, validator):
        record = {
            "messages": [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "u"},
                {"role": "assistant", "content": ""},
            ],
            "response": json.dumps(
                {
                    "master_bpm": 120,
                    "master_key": "C major",
                    "actions": [],
                    "reasoning": "test",
                    "name": "test",
                }
            ),
        }
        result = validator._validate_single(record)
        assert not result.valid
        assert any("actions array is empty" in e for e in result.errors)

    def test_response_as_dict(self, validator):
        record = {
            "messages": [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "u"},
                {"role": "assistant", "content": ""},
            ],
            "response": {
                "master_bpm": 120,
                "master_key": "C major",
                "actions": [{"action_type": "retain", "stem_index": 0}],
                "reasoning": "test",
                "name": "test",
            },
        }
        result = validator._validate_single(record)
        assert result.valid


# ── 2) Diversity Metrics ─────────────────────────────────────────────────────


class TestDiversityMetrics:
    def test_all_bpms_present(self, validator):
        records = []
        for i, bpm in enumerate([100, 110, 120, 128, 130, 140, 150]):
            records.append(
                {
                    "messages": [
                        {"role": "system", "content": "s"},
                        {"role": "user", "content": "u"},
                        {"role": "assistant", "content": ""},
                    ],
                    "response": json.dumps(
                        {
                            "master_bpm": bpm,
                            "master_key": "C major",
                            "actions": [{"action_type": "retain", "stem_index": 0}],
                            "reasoning": "test",
                            "name": "test",
                        }
                    ),
                }
            )
        report = validator.validate_batch(records)
        assert report.bpm_coverage_ratio == 1.0

    def test_low_bpm_coverage(self, validator):
        records = []
        for i in range(100):
            records.append(
                {
                    "messages": [
                        {"role": "system", "content": "s"},
                        {"role": "user", "content": "u"},
                        {"role": "assistant", "content": ""},
                    ],
                    "response": json.dumps(
                        {
                            "master_bpm": 120,
                            "master_key": "C major",
                            "actions": [{"action_type": "retain", "stem_index": 0}],
                            "reasoning": "test",
                            "name": "test",
                        }
                    ),
                }
            )
        report = validator.validate_batch(records)
        # Only 1 BPM out of 7 = ~14% coverage
        assert report.bpm_coverage_ratio < 0.2

    def test_all_keys_present(self, validator):
        records = []
        keys = [
            "A# major",
            "A# minor",
            "B major",
            "B minor",
            "C major",
            "C minor",
            "C# major",
            "C# minor",
            "D major",
            "D minor",
            "D# major",
            "D# minor",
            "E major",
            "E minor",
            "F major",
            "F minor",
            "F# major",
            "F# minor",
            "G major",
            "G minor",
            "G# major",
            "G# minor",
            "A major",
            "A minor",
        ]
        for i, key in enumerate(keys):
            records.append(
                {
                    "messages": [
                        {"role": "system", "content": "s"},
                        {"role": "user", "content": "u"},
                        {"role": "assistant", "content": ""},
                    ],
                    "response": json.dumps(
                        {
                            "master_bpm": 120,
                            "master_key": key,
                            "actions": [{"action_type": "retain", "stem_index": 0}],
                            "reasoning": "test",
                            "name": "test",
                        }
                    ),
                }
            )
        report = validator.validate_batch(records)
        assert report.key_coverage_ratio == 1.0

    def test_instrument_coverage_from_add_actions(self, validator):
        records = [
            {
                "messages": [
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "u"},
                    {"role": "assistant", "content": ""},
                ],
                "response": json.dumps(
                    {
                        "master_bpm": 120,
                        "master_key": "C major",
                        "actions": [
                            {
                                "action_type": "add",
                                "major_family": "Synth",
                                "sub_family": "Synth Lead",
                                "model_id": "foundation-1",
                            }
                        ],
                        "reasoning": "test",
                        "name": "test",
                    }
                ),
            },
            {
                "messages": [
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "u"},
                    {"role": "assistant", "content": ""},
                ],
                "response": json.dumps(
                    {
                        "master_bpm": 120,
                        "master_key": "C major",
                        "actions": [
                            {
                                "action_type": "add",
                                "major_family": "Keys",
                                "sub_family": "Grand Piano",
                                "model_id": "foundation-1",
                            }
                        ],
                        "reasoning": "test",
                        "name": "test",
                    }
                ),
            },
        ]
        report = validator.validate_batch(records)
        assert len(report.instruments_coverage) >= 2
        assert "Synth Lead" in report.instruments_coverage
        assert "Grand Piano" in report.instruments_coverage

    def test_batch_report_summary(self, validator, valid_record):
        report = validator.validate_batch([valid_record])
        summary = report.summary()
        assert "Quality Report" in summary
        assert "valid" in summary.lower()


# ── 3) Duplicate Detection ────────────────────────────────────────────────────


class TestDuplicateDetection:
    def test_no_duplicates(self, validator):
        records = []
        for i in range(10):
            records.append(
                {
                    "messages": [
                        {"role": "system", "content": "s"},
                        {"role": "user", "content": f"prompt {i}"},
                        {"role": "assistant", "content": ""},
                    ],
                    "response": json.dumps(
                        {
                            "master_bpm": 120 + i,
                            "master_key": "C major",
                            "actions": [{"action_type": "retain", "stem_index": 0}],
                            "reasoning": f"reasoning {i}",
                            "name": f"set {i}",
                        }
                    ),
                }
            )
        report = validator.validate_batch(records)
        assert report.duplicate_count == 0
        assert report.duplicate_ratio == 0.0
        assert report.unique_record_hashes == 10

    def test_all_duplicates(self, validator):
        template = {
            "messages": [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "same"},
                {"role": "assistant", "content": ""},
            ],
            "response": json.dumps(
                {
                    "master_bpm": 120,
                    "master_key": "C major",
                    "actions": [{"action_type": "retain", "stem_index": 0}],
                    "reasoning": "same",
                    "name": "same",
                }
            ),
        }
        records = [dict(template) for _ in range(20)]
        report = validator.validate_batch(records)
        # All 20 have same hash, so 19 duplicates
        assert report.duplicate_count == 19
        assert report.duplicate_ratio == 0.95
        assert report.unique_record_hashes == 1

    def test_partial_duplicates(self, validator):
        dup = {
            "messages": [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "dup"},
                {"role": "assistant", "content": ""},
            ],
            "response": json.dumps(
                {
                    "master_bpm": 120,
                    "master_key": "C major",
                    "actions": [{"action_type": "retain", "stem_index": 0}],
                    "reasoning": "dup",
                    "name": "dup",
                }
            ),
        }
        records = [dict(dup) for _ in range(5)]
        # Add 5 unique records
        for j in range(5):
            records.append(
                {
                    "messages": [
                        {"role": "system", "content": "s"},
                        {"role": "user", "content": f"unique {j}"},
                        {"role": "assistant", "content": ""},
                    ],
                    "response": json.dumps(
                        {
                            "master_bpm": 100 + j,
                            "master_key": "C major",
                            "actions": [{"action_type": "retain", "stem_index": 0}],
                            "reasoning": f"unique {j}",
                            "name": f"unique {j}",
                        }
                    ),
                }
            )

        report = validator.validate_batch(records)
        # 5 identical → 4 duplicates out of 10 total
        assert report.duplicate_count == 4
        assert 0.3 < report.duplicate_ratio < 0.5


# ── 4) Action Validity ────────────────────────────────────────────────────────


class TestActionValidity:
    def test_valid_retain_action(self, validator):
        response = {
            "master_bpm": 120,
            "master_key": "C major",
            "actions": [{"action_type": "retain", "stem_index": 0}],
            "reasoning": "test",
            "name": "test",
        }
        stems = [{"prompt": "synth"}]
        result = validator._validate_actions(response, stems)
        assert result["total"] == 1
        assert result["invalid"] == 0

    def test_retain_out_of_bounds(self, validator):
        response = {
            "master_bpm": 120,
            "master_key": "C major",
            "actions": [{"action_type": "retain", "stem_index": 99}],
            "reasoning": "test",
            "name": "test",
        }
        stems = [{"prompt": "synth"}]
        result = validator._validate_actions(response, stems)
        assert result["invalid"] == 1
        assert "retain" in result["oob_types"]

    def test_remove_out_of_bounds(self, validator):
        response = {
            "master_bpm": 120,
            "master_key": "C major",
            "actions": [{"action_type": "remove", "stem_index": 5}],
            "reasoning": "test",
            "name": "test",
        }
        stems = [{"prompt": "synth"}]  # only 1 stem, index 5 is OOB
        result = validator._validate_actions(response, stems)
        assert result["invalid"] == 1

    def test_add_with_required_fields(self, validator):
        response = {
            "master_bpm": 120,
            "master_key": "C major",
            "actions": [
                {
                    "action_type": "add",
                    "major_family": "Synth",
                    "sub_family": "Synth Lead",
                    "model_id": "foundation-1",
                }
            ],
            "reasoning": "test",
            "name": "test",
        }
        result = validator._validate_actions(response, [])
        assert result["total"] == 1
        assert result["invalid"] == 0

    def test_add_missing_fields(self, validator):
        response = {
            "master_bpm": 120,
            "master_key": "C major",
            "actions": [{"action_type": "add"}],
            "reasoning": "test",
            "name": "test",
        }
        result = validator._validate_actions(response, [])
        assert result["invalid"] >= 2  # missing major_family, sub_family, model_id

    def test_invalid_action_type(self, validator):
        response = {
            "master_bpm": 120,
            "master_key": "C major",
            "actions": [{"action_type": "INVALID", "stem_index": 0}],
            "reasoning": "test",
            "name": "test",
        }
        result = validator._validate_actions(response, [])
        assert result["invalid"] == 1
        assert "INVALID" in result["oob_types"]

    def test_retain_missing_stem_index(self, validator):
        response = {
            "master_bpm": 120,
            "master_key": "C major",
            "actions": [{"action_type": "retain"}],
            "reasoning": "test",
            "name": "test",
        }
        result = validator._validate_actions(response, [])
        assert result["invalid"] == 1

    def test_negative_stem_index(self, validator):
        response = {
            "master_bpm": 120,
            "master_key": "C major",
            "actions": [{"action_type": "retain", "stem_index": -1}],
            "reasoning": "test",
            "name": "test",
        }
        stems = [{"prompt": "synth"}]
        # stem_index < 0 is OOB
        result = validator._validate_actions(response, stems)
        assert result["invalid"] == 1

    def test_negative_artist_index(self, validator):
        """Stem index of -1 is treated as out-of-bounds."""
        response = {
            "master_bpm": 120,
            "master_key": "C major",
            "actions": [{"action_type": "remove", "stem_index": -1}],
            "reasoning": "test",
            "name": "test",
        }
        stems = [{"prompt": "synth"}]
        result = validator._validate_actions(response, stems)
        assert result["invalid"] == 1

    def test_mixed_valid_invalid_actions(self, validator):
        response = {
            "master_bpm": 120,
            "master_key": "C major",
            "actions": [
                {"action_type": "retain", "stem_index": 0},
                {"action_type": "retain", "stem_index": 99},  # OOB
                {"action_type": "add", "major_family": "Bass", "sub_family": "Sub Bass", "model_id": "foundation-1"},
                {"action_type": "INVALID", "stem_index": 5},  # bad type
            ],
            "reasoning": "test",
            "name": "test",
        }
        stems = [{"prompt": "synth"}]
        result = validator._validate_actions(response, stems)
        assert result["total"] == 4
        assert result["invalid"] == 2  # OOB retain + INVALID action_type

    def test_empty_actions(self, validator):
        result = validator._validate_actions({"actions": []}, [])
        assert result["total"] == 0
        assert result["invalid"] == 0


# ── 5) Threshold checking ────────────────────────────────────────────────────


class TestThresholdChecking:
    def test_all_pass_with_good_data(self, validator):
        records = []
        # Use all keys and many instruments to exceed the 60% and 50% thresholds
        all_keys = [
            "C major",
            "C# major",
            "D major",
            "D# major",
            "E major",
            "F major",
            "F# major",
            "G major",
            "G# major",
            "A major",
            "A# major",
            "B major",
            "C minor",
            "C# minor",
            "D minor",
            "D# minor",
            "E minor",
            "F minor",
            "F# minor",
            "G minor",
            "G# minor",
            "A minor",
            "A# minor",
            "B minor",
        ]
        many_instruments = [
            "Synth Lead",
            "Synth Bass",
            "FM Synth",
            "Wavetable Synth",
            "Analog Synth",
            "Supersaw",
            "Digital Organ",
            "Hammond Organ",
            "Church Organ",
            "Grand Piano",
            "Digital Piano",
            "Rhodes Piano",
            "Wurlitzer Piano",
            "CP Piano",
            "Clavinet",
            "Celesta",
            "Harpsichord",
            "Pad",
            "Atmosphere",
            "Texture",
            "Bell",
            "Church Bell",
            "Tubular Bells",
            "Marimba",
            "Vibraphone",
            "Glockenspiel",
            "Xylophone",
            "Steel Drums",
            "Kalimba",
            "Ocarina",
            "Pluck",
            "Music Box",
            "Tack Piano",
            "Violin",
            "Viola",
            "Cello",
            "Digital Strings",
            "Harp",
            "Celtic Harp",
            "Concert Harp",
            "Koto",
            "Sitar",
            "Fiddle",
            "Acoustic Guitar",
            "Nylon Guitar",
            "Electric Guitar",
            "Trumpet",
            "French Horn",
            "Flugelhorn",
            "Bass Trombone",
            "Tenor Trombone",
            "Tuba",
            "Flute",
            "Piccolo",
            "Clarinet",
            "Oboe",
            "Bassoon",
            "Irish Flute",
            "World Winds",
            "Saxophone",
            "Choir",
            "Synthetic Choir",
            "Synthetic Vox",
            "Sub Bass",
            "Reese Bass",
            "Analog Bass",
            "Wavetable Bass",
            "Picked Bass",
            "Digital Bass",
            "FM Bass",
            "Pan Flute",
        ]
        bpm_options = [100, 110, 120, 128, 130, 140, 150]
        for i in range(500):
            records.append(
                {
                    "messages": [
                        {"role": "system", "content": "s"},
                        {"role": "user", "content": f"u{i}"},
                        {"role": "assistant", "content": ""},
                    ],
                    "response": json.dumps(
                        {
                            "master_bpm": bpm_options[i % len(bpm_options)],
                            "master_key": all_keys[i % len(all_keys)],
                            "actions": [
                                {"action_type": "retain", "stem_index": 0},
                                {
                                    "action_type": "add",
                                    "major_family": "Synth",
                                    "sub_family": many_instruments[i % len(many_instruments)],
                                    "model_id": "foundation-1",
                                },
                            ],
                            "reasoning": f"r{i}",
                            "name": f"n{i}",
                        }
                    ),
                }
            )
        report = validator.validate_batch(records)
        assert report.passed, f"Failed: {report.threshold_failures}"

    def test_fails_on_bad_diversity(self, validator):
        records = []
        for i in range(50):
            records.append(
                {
                    "messages": [
                        {"role": "system", "content": "s"},
                        {"role": "user", "content": "u"},
                        {"role": "assistant", "content": ""},
                    ],
                    "response": json.dumps(
                        {
                            "master_bpm": 120,
                            "master_key": "C major",
                            "actions": [{"action_type": "retain", "stem_index": 0}],
                            "reasoning": "r",
                            "name": "n",
                        }
                    ),
                }
            )
        report = validator.validate_batch(records)
        # Should fail BPM coverage, key coverage, instrument coverage, duplicates
        assert not report.passed
        assert len(report.threshold_failures) >= 3

    def test_fails_on_high_duplicates(self, validator):
        records = []
        # 5 unique + 5 identical to first = 50% dups
        for i in range(5):
            records.append(
                {
                    "messages": [
                        {"role": "system", "content": "s"},
                        {"role": "user", "content": f"u{i}"},
                        {"role": "assistant", "content": ""},
                    ],
                    "response": json.dumps(
                        {
                            "master_bpm": 100 + i,
                            "master_key": "C major",
                            "actions": [{"action_type": "retain", "stem_index": 0}],
                            "reasoning": f"r{i}",
                            "name": f"n{i}",
                        }
                    ),
                }
            )
        for _ in range(5):
            records.append(
                {
                    "messages": [
                        {"role": "system", "content": "s"},
                        {"role": "user", "content": "u0"},
                        {"role": "assistant", "content": ""},
                    ],
                    "response": json.dumps(
                        {
                            "master_bpm": 100,
                            "master_key": "C major",
                            "actions": [{"action_type": "retain", "stem_index": 0}],
                            "reasoning": "r0",
                            "name": "n0",
                        }
                    ),
                }
            )
        report = validator.validate_batch(records)
        dup_ratio = report.duplicate_ratio
        # 5 duplicates out of 10 = 50% > 5% threshold
        assert dup_ratio > 0.05

    def test_assert_thresholds_raises(self, validator):
        records = [
            {
                "messages": [
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "u"},
                    {"role": "assistant", "content": ""},
                ],
                "response": json.dumps(
                    {
                        "master_bpm": 120,
                        "master_key": "C major",
                        "actions": [{"action_type": "retain", "stem_index": 0}],
                        "reasoning": "r",
                        "name": "n",
                    }
                ),
            }
        ]
        report = validator.validate_batch(records)
        with pytest.raises(QualityThresholdError):
            report.assert_thresholds()

    def test_custom_thresholds(self, validator):
        strict = QualityValidator(
            thresholds={
                "min_bpm_coverage": 1.0,
                "min_key_coverage": 1.0,
                "min_instrument_coverage": 1.0,
            }
        )
        records = [
            {
                "messages": [
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "u"},
                    {"role": "assistant", "content": ""},
                ],
                "response": json.dumps(
                    {
                        "master_bpm": 120,
                        "master_key": "C major",
                        "actions": [{"action_type": "retain", "stem_index": 0}],
                        "reasoning": "r",
                        "name": "n",
                    }
                ),
            }
        ]
        report = strict.validate_batch(records)
        assert not report.passed

    def test_report_to_dict_json_serializable(self, validator, valid_record):
        report = validator.validate_batch([valid_record])
        d = report.to_dict()
        assert isinstance(d, dict)
        # Should be JSON-serializable
        json.dumps(d)


# ── 6) Vibe Override Persistence ──────────────────────────────────────────────


class TestVibePersistence:
    def test_no_vibes_returns_perfect_ratio(self, validator):
        records = [
            {
                "messages": [
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "no vibe here"},
                    {"role": "assistant", "content": ""},
                ],
                "response": json.dumps(
                    {
                        "master_bpm": 120,
                        "master_key": "C major",
                        "actions": [{"action_type": "retain", "stem_index": 0}],
                        "reasoning": "r",
                        "name": "n",
                    }
                ),
            }
        ]
        result = validator.validate_vibe_persistence(records)
        assert result["persistence_ratio"] == 1.0

    def test_vibe_persistence_in_subsequent_records(self, validator):
        vibe_text = "Let's go harder!"
        records = [
            {
                "messages": [
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": f"OVERRIDE: {vibe_text}"},
                    {"role": "assistant", "content": ""},
                ],
                "response": json.dumps(
                    {
                        "master_bpm": 120,
                        "master_key": "C major",
                        "actions": [{"action_type": "retain", "stem_index": 0}],
                        "reasoning": "r",
                        "name": "n",
                    }
                ),
            },
            {
                "messages": [
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": f"OVERRIDE: {vibe_text}"},  # persists
                    {"role": "assistant", "content": ""},
                ],
                "response": json.dumps(
                    {
                        "master_bpm": 128,
                        "master_key": "C major",
                        "actions": [
                            {
                                "action_type": "add",
                                "major_family": "Bass",
                                "sub_family": "Sub Bass",
                                "model_id": "foundation-1",
                            }
                        ],
                        "reasoning": "r",
                        "name": "n",
                    }
                ),
            },
        ]
        result = validator.validate_vibe_persistence(records)
        assert result["transitions"] >= 1
        assert result["persistence_ratio"] > 0.0


# ── 7) Integration: validate_batch with active_stems ─────────────────────────


class TestBatchWithStems:
    def test_bounds_check_with_stems(self, validator):
        records = [
            {
                "messages": [
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "u"},
                    {"role": "assistant", "content": ""},
                ],
                "response": json.dumps(
                    {
                        "master_bpm": 120,
                        "master_key": "C major",
                        "actions": [{"action_type": "retain", "stem_index": 5}],
                        "reasoning": "r",
                        "name": "n",
                    }
                ),
            }
        ]
        stems = [{"prompt": "synth"}, {"prompt": "bass"}]  # 2 stems, index 5 is OOB
        report = validator.validate_batch(records, active_stems_per_record=[stems])
        assert report.invalid_actions >= 1
        assert report.action_validity_ratio < 1.0

    def test_bounds_check_without_stems_no_oob(self, validator):
        """Without stems, bounds checking is skipped (can't determine bounds)."""
        records = [
            {
                "messages": [
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "u"},
                    {"role": "assistant", "content": ""},
                ],
                "response": json.dumps(
                    {
                        "master_bpm": 120,
                        "master_key": "C major",
                        "actions": [{"action_type": "retain", "stem_index": 5}],
                        "reasoning": "r",
                        "name": "n",
                    }
                ),
            }
        ]
        report = validator.validate_batch(records)
        # Without stems, no bounds check → no invalid actions from bounds
        assert report.invalid_actions == 0


# ── 8) Edge cases ─────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_batch(self, validator):
        report = validator.validate_batch([])
        assert report.total_records == 0
        assert report.valid_records == 0
        assert report.passed  # no data = no failures

    def test_batch_with_all_invalid_records(self, validator):
        records = [{"bad": "data"} for _ in range(10)]
        report = validator.validate_batch(records)
        assert report.valid_records == 0
        assert report.invalid_records == 10

    def test_response_is_dict_not_string(self, validator):
        record = {
            "messages": [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "u"},
                {"role": "assistant", "content": ""},
            ],
            "response": {
                "master_bpm": 120,
                "master_key": "C major",
                "actions": [{"action_type": "retain", "stem_index": 0}],
                "reasoning": "r",
                "name": "n",
            },
        }
        result = validator._validate_single(record)
        assert result.valid

    def test_large_batch_performance(self, validator):
        """Ensure 1000 records validate in reasonable time."""
        import time

        records = []
        for i in range(1000):
            records.append(
                {
                    "messages": [
                        {"role": "system", "content": "s"},
                        {"role": "user", "content": f"u{i}"},
                        {"role": "assistant", "content": ""},
                    ],
                    "response": json.dumps(
                        {
                            "master_bpm": 100 + (i % 7) * 10,
                            "master_key": "C major",
                            "actions": [{"action_type": "retain", "stem_index": 0}],
                            "reasoning": f"r{i}",
                            "name": f"n{i}",
                        }
                    ),
                }
            )
        start = time.time()
        report = validator.validate_batch(records)
        elapsed = time.time() - start
        assert elapsed < 10.0  # should be fast
        assert report.total_records == 1000
