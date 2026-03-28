# tests/slop_harness/test_models.py
import pytest
from slop_harness.models import (
    FOUNDATION_1_MODEL,
    INFINITE_PIANOS_MODEL,
    VOCAL_TEXTURES_MODEL,
    ALL_MODELS,
    TIMBRE_TAGS_F1,
    FX_TAGS_F1,
    NOTATION_TAGS_F1,
    KEYS_ALL,
    BPMS_ALL,
    BARS_ALL,
)


def test_foundation_1_has_required_fields():
    assert "id" in FOUNDATION_1_MODEL
    assert "repo_id" in FOUNDATION_1_MODEL
    assert "description" in FOUNDATION_1_MODEL
    assert "major_families" in FOUNDATION_1_MODEL
    assert "sub_families" in FOUNDATION_1_MODEL
    assert "timbre_tags" in FOUNDATION_1_MODEL
    assert "notation_tags" in FOUNDATION_1_MODEL
    assert "fx_tags" in FOUNDATION_1_MODEL
    assert "keys" in FOUNDATION_1_MODEL
    assert "bpms" in FOUNDATION_1_MODEL
    assert "bars" in FOUNDATION_1_MODEL


def test_all_models_have_ids():
    for model in ALL_MODELS:
        assert "id" in model
        assert "description" in model


def test_timbre_tags_f1_match_hf_docs():
    expected = {"Warm", "Bright", "Wide", "Airy", "Thick", "Rich", "Tight", "Full",
                "Gritty", "Clean", "Retro", "Saw", "Crisp", "Focused", "Metallic",
                "Chiptune", "Dark", "303", "Shiny", "Analog", "Present", "Sparkly",
                "Ambient", "Soft", "Smooth", "Cold", "Buzzy", "Deep", "Formant Vocal",
                "Round", "Punchy", "Nasal", "Vintage", "Growl", "Breathy", "Glassy",
                "Noisy", "Synthetic Vox", "Supersaw", "Bitcrushed", "Dreamy"}
    assert set(TIMBRE_TAGS_F1) == expected


def test_fx_tags_f1_match_hf_docs():
    expected = {
        "Low Reverb", "Medium Reverb", "High Reverb", "Plate Reverb",
        "Low Delay", "Medium Delay", "High Delay", "Ping Pong Delay",
        "Stereo Delay", "Cross Delay", "Mono Delay",
        "Low Distortion", "Medium Distortion", "High Distortion",
        "Phaser", "Low Phaser", "Medium Phaser", "High Phaser",
        "Bitcrush", "High Bitcrush"
    }
    assert set(FX_TAGS_F1) == expected


def test_keys_all_has_24_keys():
    assert len(KEYS_ALL) == 24
    majors = [k for k in KEYS_ALL if "major" in k]
    minors = [k for k in KEYS_ALL if "minor" in k]
    assert len(majors) == 12
    assert len(minors) == 12
    assert "G# major" in KEYS_ALL
    assert "G# minor" in KEYS_ALL


def test_bpms_all_standard_values():
    assert set(BPMS_ALL) == {100, 110, 120, 128, 130, 140, 150}


def test_bars_all_standard_values():
    assert set(BARS_ALL) == {4, 8}
