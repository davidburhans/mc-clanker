from slop_harness.state_generator import StateGenerator


def test_same_seed_produces_identical_state():
    """The same (batch_id, interaction_id) always produces the same state."""
    a = StateGenerator(batch_id=0, interaction_id=42).build()
    b = StateGenerator(batch_id=0, interaction_id=42).build()
    assert a["bpm"] == b["bpm"]
    assert a["key"] == b["key"]
    assert a["stem_count"] == b["stem_count"]
    assert len(a["stems"]) == len(b["stems"])
    for i in range(len(a["stems"])):
        assert a["stems"][i]["instrument"] == b["stems"][i]["instrument"]
        assert a["stems"][i]["_age"] == b["stems"][i]["_age"]


def test_different_seeds_produce_different_states():
    """Different (batch_id, interaction_id) pairs produce different states."""
    a = StateGenerator(batch_id=0, interaction_id=0).build()
    b = StateGenerator(batch_id=0, interaction_id=1).build()
    assert a["bpm"] != b["bpm"] or a["key"] != b["key"] or a["stem_count"] != b["stem_count"]


def test_stem_count_range():
    """Stem count is always between 1 and 7."""
    for i in range(100):
        state = StateGenerator(batch_id=0, interaction_id=i).build()
        assert 1 <= state["stem_count"] <= 7


def test_bpm_in_valid_range():
    """BPM is always one of the valid values."""
    for i in range(200):
        state = StateGenerator(batch_id=0, interaction_id=i).build()
        assert state["bpm"] in [100, 110, 120, 128, 130, 140, 150]


def test_key_is_valid():
    """Key is always one of the valid keys."""
    from slop_harness.models import ALL_KEYS
    for i in range(200):
        state = StateGenerator(batch_id=0, interaction_id=i).build()
        assert state["key"] in ALL_KEYS


def test_history_depth_bounded():
    """History depth is at most 5 loops."""
    for i in range(50):
        state = StateGenerator(batch_id=0, interaction_id=i).build()
        assert len(state["history"]) <= 5


def test_stems_have_required_fields():
    """Each stem has all required generation fields."""
    state = StateGenerator(batch_id=0, interaction_id=99).build()
    for stem in state["stems"]:
        assert "instrument" in stem
        assert "major_family" in stem
        assert "sub_family" in stem
        assert "timbre_tags" in stem
        assert "notation_tag" in stem
        assert "fx_tag" in stem
        assert "key" in stem
        assert "bpm" in stem
        assert "bars" in stem
        assert "model_id" in stem
        assert "prompt" in stem
        assert "_age" in stem


def test_stems_use_valid_major_family():
    """Stem major_family is valid for Foundation-1's supported families."""
    state = StateGenerator(batch_id=0, interaction_id=77).build()
    valid_families = [
        "Synth", "Keys", "Bass", "Bowed Strings", "Mallet",
        "Wind", "Guitar", "Brass", "Vocal", "Plucked Strings",
    ]
    for stem in state["stems"]:
        assert stem["major_family"] in valid_families


def test_stems_use_valid_model_id():
    """Stem model_id is always foundation-1."""
    state = StateGenerator(batch_id=0, interaction_id=55).build()
    for stem in state["stems"]:
        assert stem["model_id"] == "foundation-1"


def test_available_models_always_includes_foundation_1():
    """available_models list always includes foundation-1."""
    for i in range(50):
        state = StateGenerator(batch_id=0, interaction_id=i).build()
        assert "foundation-1" in state["available_models"]


def test_available_models_only_valid_ids():
    """available_models only contains valid model IDs."""
    valid = {"foundation-1", "infinite-pianos", "vocal-textures"}
    for i in range(50):
        state = StateGenerator(batch_id=0, interaction_id=i).build()
        for m in state["available_models"]:
            assert m in valid


def test_song_age_range():
    """song_age is between 0 and 50."""
    for i in range(100):
        state = StateGenerator(batch_id=0, interaction_id=i).build()
        assert 0 <= state["song_age"] <= 50
