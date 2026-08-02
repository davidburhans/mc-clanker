# tests/slop_harness/test_models.py
from slop_harness.models import (
    FOUNDATION_1_MODEL,
    ALL_MODELS,
    TIMBRE_TAGS_F1,
    FX_TAGS_F1,
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
    # From HF Master Tag Reference — exact timbre tags Foundation-1 was trained on
    expected = {
        # Frequency/Brightness
        "Upper Mids", "Mids", "Highs", "Low Mids",
        "Bright", "Dark", "Shiny", "Sparkly",
        "Warm", "Cold", "Silky", "Glass", "Glassy",
        # Texture/Character
        "Clean", "Gritty", "Retro", "Analog",
        "Crisp", "Focused", "Metallic", "Chiptune",
        "Woody", "Rubbery", "Buzzy", "Vintage",
        # Spatial/Dynamic
        "Wide", "Thick", "Thin", "Full",
        "Near", "Far", "Rich", "Tight",
        "Punchy", "Plucked", "Snappy", "Staccato",
        # Modulation/Effects
        "Saw", "Square", "Triangle",
        "Pitch Bend", "Filter", "Bitcrush", "Bitcrushed",
        "Growl", "Biting", "Harsh", "Overdriven",
        "Acid", "Reese", "Siren",
        # Environmental
        "Spacey", "Ambient", "Muffled", "Veiled",
        "Boomy", "Deep", "Rumble",
        "White Noise", "Laser", "FX",
        # Formant/Vocal
        "Formant Vocal", "Synthetic Vox",
        # Other
        "Round", "Nasal", "Noisy", "Dreamy", "Supersaw",
        "Airy", "Breathy", "Present",
        "303",
    }
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
