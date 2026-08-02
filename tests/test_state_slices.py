"""Phase 8 / E3 pass-1: GlobalState additive slice views.

Pins the pass-1 contract (brief-03 ssB): slices are READ VIEWS over the same
state.__dict__ — storage unmoved, no rename, legacy access unchanged, boundaries
enforced (out-of-slice attrs raise), and the 3 dead ModelMgmt attrs are gone.
"""

from __future__ import annotations

import pytest

from app.framework.framework_state import GlobalState


def test_slice_is_a_live_view_of_the_same_attribute() -> None:
    s = GlobalState()
    # Same object (forwarded read, not a copy) -> mutations are visible.
    assert s.musical.current_bpm is s.current_bpm
    assert s.musical.active_stems is s.active_stems
    s.current_bpm = 140
    assert s.musical.current_bpm == 140


def test_each_slice_exposes_its_documented_members() -> None:
    s = GlobalState()
    # Spot-check one member per slice (proves forwarding + that the slice exists).
    _ = s.musical.current_key
    _ = s.generation.is_generating
    _ = s.llm.llm_model
    _ = s.levels.stem_volumes
    _ = s.loop_coord.loop_count
    _ = s.recording.is_recording
    _ = s.playback.is_playback_active
    _ = s.stem_cache_view.last_generated_stems
    _ = s.catalog.available_instruments
    _ = s.session.dj_password


def test_out_of_slice_attribute_raises_attribute_error() -> None:
    """Slice boundaries are real: is_generating is NOT on the musical slice."""
    s = GlobalState()
    with pytest.raises(AttributeError):
        _ = s.musical.is_generating  # is_generating belongs to GenerationControl
    with pytest.raises(AttributeError):
        _ = s.levels.current_bpm  # current_bpm belongs to MusicalParams


def test_levels_view_named_not_mixer_to_avoid_clash() -> None:
    """state.levels exists; state.mixer must NOT (clashes with framework_mixer.Mixer)."""
    s = GlobalState()
    assert isinstance(s.levels, object)
    assert not hasattr(s, "mixer"), "state.mixer would clash with framework_mixer.Mixer"


def test_cache_stem_forwards_through_the_view() -> None:
    s = GlobalState()
    audio = object()
    s.stem_cache_view.cache_stem("prompt-x", audio)
    assert "prompt-x" in s.last_generated_stems


def test_dead_model_mgmt_attrs_removed() -> None:
    """The 3 vestigial ModelMgmt dicts (never read) are gone (brief-03 ssA)."""
    s = GlobalState()
    assert not hasattr(s, "model_states")
    assert not hasattr(s, "model_errors")
    assert not hasattr(s, "download_progress")
    assert hasattr(s, "generator")  # generator is NOT dead — kept


def test_legacy_attribute_access_unchanged() -> None:
    """Pass-1 is additive: nothing about state.X access changed."""
    s = GlobalState()
    s.current_bpm = 100
    s.active_stems.append({"instrument": "Drums"})
    assert s.current_bpm == 100
    assert len(s.active_stems) == 1
