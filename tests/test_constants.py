"""Tests for app.lib.constants — single source of truth for schema enums."""

import pytest
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from app.lib.constants import (
    VALID_BPMS,
    VALID_KEYS,
    VALID_BARS,
    VALID_MODEL_IDS,
    VALID_MAJOR_FAMILIES,
    VALID_SUB_FAMILIES,
    TIMBRE_TAGS,
    NOTATION_TAGS,
    FX_TAGS,
    get_all_major_families,
    add_custom_major_family,
    get_response_format_schema,
)


class TestStaticEnums:
    def test_bpm_enum_values(self):
        assert VALID_BPMS == [100, 110, 120, 128, 130, 140, 150]
        assert len(VALID_BPMS) == 7

    def test_key_enum_count(self):
        assert len(VALID_KEYS) == 24
        # All 24 keys should be present
        assert "C major" in VALID_KEYS
        assert "C minor" in VALID_KEYS
        assert "B major" in VALID_KEYS
        assert "B minor" in VALID_KEYS
        assert "C# major" in VALID_KEYS
        assert "G# minor" in VALID_KEYS

    def test_bars_enum(self):
        assert VALID_BARS == [4, 8]

    def test_model_ids(self):
        assert VALID_MODEL_IDS == ["foundation-1", "infinite-pianos", "vocal-textures"]

    def test_major_families_not_empty(self):
        assert len(VALID_MAJOR_FAMILIES) > 10
        assert "Drums" in VALID_MAJOR_FAMILIES
        assert "Synth" in VALID_MAJOR_FAMILIES
        assert "Bass" in VALID_MAJOR_FAMILIES

    def test_sub_families_not_empty(self):
        assert len(VALID_SUB_FAMILIES) > 50
        assert "Synth Lead" in VALID_SUB_FAMILIES
        assert "Grand Piano" in VALID_SUB_FAMILIES

    def test_timbre_tags_not_empty(self):
        assert len(TIMBRE_TAGS) > 50
        assert "Warm" in TIMBRE_TAGS
        assert "Bright" in TIMBRE_TAGS

    def test_notation_tags_not_empty(self):
        assert len(NOTATION_TAGS) > 10
        assert "melody" in NOTATION_TAGS
        assert "chord progression" in NOTATION_TAGS

    def test_fx_tags_not_empty(self):
        assert len(FX_TAGS) > 10
        assert "Low Reverb" in FX_TAGS
        assert "Bitcrush" in FX_TAGS


class TestDynamicFamilyExtension:
    @pytest.fixture(autouse=True)
    def _custom_family_cleanup(self):
        """Remove any custom families added during a test."""
        families_added = []
        original_add = add_custom_major_family

        def tracked_add(family: str) -> None:
            original_add(family)
            families_added.append(family)

        # Monkey-patch during test
        import app.lib.constants as const_module

        const_module.add_custom_major_family = tracked_add
        yield
        const_module.add_custom_major_family = original_add
        # Clean up
        for family in families_added:
            const_module._custom_major_families.discard(family)

    def test_dynamic_family_extension(self):
        # Add a custom family
        add_custom_major_family("MyCustomFamily")
        families = get_all_major_families()
        assert "MyCustomFamily" in families

    def test_schema_includes_custom_family(self):
        add_custom_major_family("TestBrass")
        schema = get_response_format_schema()
        # Path: top-level key "json_schema" -> inner dict's "schema" key -> actions.items.anyOf[1].properties.major_family.enum
        major_family_enum = schema["json_schema"]["schema"]["properties"]["actions"]["items"]["anyOf"][1]["properties"][
            "major_family"
        ]["enum"]
        assert "TestBrass" in major_family_enum

    def test_schema_constrains_model_id(self):
        schema = get_response_format_schema()
        model_id_enum = schema["json_schema"]["schema"]["properties"]["actions"]["items"]["anyOf"][1]["properties"][
            "model_id"
        ]["enum"]
        assert model_id_enum == VALID_MODEL_IDS

    def test_schema_constrains_bars(self):
        schema = get_response_format_schema()
        bars_enum = schema["json_schema"]["schema"]["properties"]["actions"]["items"]["anyOf"][1]["properties"]["bars"][
            "enum"
        ]
        assert bars_enum == VALID_BARS

    def test_schema_constrains_timbre_tags(self):
        schema = get_response_format_schema()
        timbre_items_enum = schema["json_schema"]["schema"]["properties"]["actions"]["items"]["anyOf"][1]["properties"][
            "timbre_tags"
        ]["items"]["enum"]
        assert timbre_items_enum == TIMBRE_TAGS

    def test_schema_constrains_notation_tag(self):
        schema = get_response_format_schema()
        notation_enum = schema["json_schema"]["schema"]["properties"]["actions"]["items"]["anyOf"][1]["properties"][
            "notation_tag"
        ]["enum"]
        assert notation_enum == NOTATION_TAGS

    def test_schema_constrains_fx_tag(self):
        schema = get_response_format_schema()
        fx_enum = schema["json_schema"]["schema"]["properties"]["actions"]["items"]["anyOf"][1]["properties"]["fx_tag"][
            "enum"
        ]
        assert fx_enum == FX_TAGS
