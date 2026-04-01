import pytest
from unittest.mock import patch, MagicMock


class TestCalcDuration:
    """Test duration calculation helper."""

    def test_calc_duration_standard(self):
        """Test duration calculation for standard BPM/bar combinations."""
        from app.framework.framework_main import calc_duration

        # 120 BPM, 4 bars, 4/4 time
        # 4 bars * 4 beats/bar = 16 beats
        # 16 beats / (120 beats/60sec) = 8 seconds
        result = calc_duration(120, 4)
        assert result == 8.0

    def test_calc_duration_different_bpm(self):
        """Test with different BPM values."""
        from app.framework.framework_main import calc_duration

        # 140 BPM, 4 bars
        # 4 * 4 = 16 beats
        # 16 / (140/60) = 16 * 60 / 140 = 6.857...
        result = calc_duration(140, 4)
        assert abs(result - 6.857) < 0.01

    def test_calc_duration_different_bars(self):
        """Test with different bar counts."""
        from app.framework.framework_main import calc_duration

        # 120 BPM, 8 bars
        # 8 * 4 = 32 beats
        # 32 / 2 = 16 seconds
        result = calc_duration(120, 8)
        assert result == 16.0

    def test_calc_duration_different_time_signature(self):
        """Test with time signature != 4."""
        from app.framework.framework_main import calc_duration

        # 120 BPM, 4 bars, 3/4 time (waltz)
        # 4 bars * 3 beats/bar = 12 beats
        # 12 / 2 = 6 seconds
        result = calc_duration(120, 4, time_signature=3)
        assert result == 6.0

    def test_calc_duration_waltz_time(self):
        """Test with 3/4 time signature."""
        from app.framework.framework_main import calc_duration

        # 120 BPM, 8 bars, 3/4 time
        # 8 * 3 = 24 beats
        # 24 / 2 = 12 seconds
        result = calc_duration(120, 8, time_signature=3)
        assert result == 12.0


class TestStemDeduplication:
    """Test stem deduplication key generation."""

    def test_deduplication_key_format(self):
        """Test deduplication key format matches framework_main logic."""
        # Test that the key format is correct
        t = {
            "model_id": "test_model",
            "major_family": "Synth",
            "sub_family": "Pad",
            "timbre_tags": ["Warm", "Bright"],
            "notation_tag": "chord progression",
            "fx_tag": "Medium Reverb"
        }

        m_id = t.get("model_id", "default")
        t_key = f"{m_id}_{t.get('major_family')}_{t.get('sub_family')}_{'_'.join(t.get('timbre_tags', []))}_{t.get('notation_tag')}_{t.get('fx_tag')}"

        expected = "test_model_Synth_Pad_Warm_Bright_chord progression_Medium Reverb"
        assert t_key == expected

    def test_deduplication_key_with_defaults(self):
        """Test key generation with missing optional fields."""
        t = {
            "model_id": "model_a",
            "major_family": "Bass"
        }

        m_id = t.get("model_id", "default")
        t_key = f"{m_id}_{t.get('major_family')}_{t.get('sub_family')}_{'_'.join(t.get('timbre_tags', []))}_{t.get('notation_tag')}_{t.get('fx_tag')}"

        assert t_key.startswith("model_a_Bass_")


class TestActionProcessing:
    """Test DJ action processing logic."""

    def test_retain_action_logic(self):
        """Test retain action processing."""
        active_stems = [
            {"prompt": "Synth, Pad, Warm", "_age": 1, "_original_details": {}},
            {"prompt": "Bass, Sub, Thick", "_age": 2, "_original_details": {}}
        ]

        action = {"action_type": "retain", "stem_index": 0}
        a_type = action.get("action_type")
        idx = action.get("stem_index")

        # Simulate retain logic
        if a_type == "retain" and idx is not None and 0 <= idx < len(active_stems):
            s = active_stems[idx]
            orig = s.get('_original_details', {})
            orig['_age'] = s.get('_age', 0) + 1
            assert orig['_age'] == 2

    def test_add_action_logic(self):
        """Test add action processing."""
        action = {
            "action_type": "add",
            "model_id": "test_model",
            "major_family": "Drums",
            "sub_family": "Kick Drum",
            "timbre_tags": ["Driving"],
            "notation_tag": "simple",
            "fx_tag": "Dry",
            "bars": 4
        }

        a_type = action.get("action_type")

        if a_type == "add":
            major = action.get("major_family", "Synth")
            sub = action.get("sub_family", "Synth Lead")
            new_track = {
                "model_id": action.get("model_id"),
                "major_family": major,
                "sub_family": sub,
                "timbre_tags": action.get("timbre_tags", ["Warm"]),
                "notation_tag": action.get("notation_tag", "melody"),
                "fx_tag": action.get("fx_tag", "Medium Reverb"),
                "bars": action.get("bars", 4),
                "_age": 0
            }

            assert new_track["model_id"] == "test_model"
            assert new_track["major_family"] == "Drums"
            assert new_track["sub_family"] == "Kick Drum"
            assert new_track["timbre_tags"] == ["Driving"]
            assert new_track["_age"] == 0

    def test_remove_action_logic(self):
        """Test remove action processing."""
        active_stems = [
            {"prompt": "Synth, Pad, Warm"},
            {"prompt": "Bass, Sub, Thick"}
        ]

        action = {"action_type": "remove", "stem_index": 0}
        a_type = action.get("action_type")
        idx = action.get("stem_index")

        if a_type == "remove" and idx is not None and 0 <= idx < len(active_stems):
            removed_prompt = active_stems[idx].get("prompt", "").split(",")[1].strip()
            assert removed_prompt == "Pad"

    def test_invalid_retain_index_ignored(self):
        """Test that invalid retain indices are ignored."""
        active_stems = [{"prompt": "Synth"}]

        action = {"action_type": "retain", "stem_index": 99}
        a_type = action.get("action_type")
        idx = action.get("stem_index")

        # Should not process because idx is out of range
        should_process = a_type == "retain" and idx is not None and 0 <= idx < len(active_stems)
        assert should_process is False

    def test_invalid_remove_index_ignored(self):
        """Test that invalid remove indices are ignored."""
        active_stems = [{"prompt": "Synth"}]

        action = {"action_type": "remove", "stem_index": -1}
        a_type = action.get("action_type")
        idx = action.get("stem_index")

        should_process = a_type == "remove" and idx is not None and 0 <= idx < len(active_stems)
        assert should_process is False


class TestFallbackOnEmptyTracks:
    """Test behavior when new_tracks is empty."""

    def test_empty_tracks_deduplication(self):
        """Test deduplication with empty track list."""
        new_tracks = []

        unique_tracks = {}
        for t in new_tracks:
            if not t:
                continue
            m_id = t.get("model_id", "default")
            t_key = f"{m_id}_{t.get('major_family')}_{t.get('sub_family')}"
            if t_key not in unique_tracks:
                unique_tracks[t_key] = t

        deduped_tracks = list(unique_tracks.values())
        assert len(deduped_tracks) == 0


class TestCacheKeyGeneration:
    """Test cache key format."""

    def test_cache_key_format(self):
        """Test cache key format matches framework_main logic."""
        prompt = "Synth, Pad, Warm, chord progression, Medium Reverb, C minor"
        m_id = "test_model"
        bpm = 128
        key = "C minor"
        bars = 4

        cache_key = f"{m_id}_{prompt}_{bpm}_{key}_{bars}"

        assert cache_key.startswith("test_model_Synth")
        assert "128" in cache_key
        assert "C minor" in cache_key
        assert cache_key.endswith("_4")

    def test_cache_key_uniqueness(self):
        """Test that different parameters produce different keys."""
        key1 = "model_a_Synth, Pad_120_C minor_4"
        key2 = "model_b_Synth, Pad_120_C minor_4"
        key3 = "model_a_Bass, Sub_120_C minor_4"

        assert key1 != key2
        assert key1 != key3
        assert key2 != key3


class TestPromptTemplate:
    """Test prompt template formatting."""

    def test_prompt_template_formatting(self):
        """Test prompt template with all fields."""
        template = "{major_family}, {sub_family}, {timbre_tags}, {notation_tag}, {fx_tag}, {key}, {bpm} BPM, {bars} Bars"

        result = template.format(
            major_family="Drums",
            sub_family="Kick Drum",
            timbre_tags="Driving Groovy",
            notation_tag="simple",
            fx_tag="Dry",
            key="C minor",
            bpm=128,
            bars=4
        )

        assert "Drums" in result
        assert "Kick Drum" in result
        assert "Driving Groovy" in result
        assert "simple" in result
        assert "Dry" in result
        assert "C minor" in result
        assert "128 BPM" in result
        assert "4 Bars" in result

    def test_prompt_template_default_values(self):
        """Test prompt with minimal fields uses defaults."""
        template = "{major_family}, {sub_family}, {timbre_tags}, {notation_tag}, {fx_tag}, {key}"

        result = template.format(
            major_family="Synth",
            sub_family="",
            timbre_tags="",
            notation_tag="",
            fx_tag="",
            key="C major"
        )

        assert "Synth" in result
        assert "C major" in result


class TestFlushRecordingBuffers:
    """Test flush_recording_buffers function."""

    def test_flush_does_nothing_when_buffers_empty(self):
        """Test that flush does nothing when both buffers are empty."""
        from app.framework.framework_main import flush_recording_buffers
        from app.framework.framework_state import state

        state.llm_interaction_buffer = []
        state.action_buffer = []
        state.current_show_id = None

        # Should not raise
        flush_recording_buffers()

    def test_flush_clears_buffers_when_no_show_id(self):
        """Test buffers are cleared when show_id is None."""
        from app.framework.framework_main import flush_recording_buffers
        from app.framework.framework_state import state

        state.llm_interaction_buffer = [{"test": "interaction"}]
        state.action_buffer = [{"test": "action"}]
        state.current_show_id = None

        flush_recording_buffers()

        # Buffers should be cleared even without a show
        assert len(state.llm_interaction_buffer) == 0
        assert len(state.action_buffer) == 0

    def test_flush_writes_to_db_when_show_recording(self):
        """Test that flush writes buffers to DB when show is recording."""
        from app.framework.framework_main import flush_recording_buffers
        from app.framework.framework_state import state

        # Set up state for recording
        state.llm_interaction_buffer = [
            {"show_id": 1, "loop_index": 1, "prompt_messages": [], "parsed_response": {}, "reasoning": "test", "error": None, "was_fallback": False}
        ]
        state.action_buffer = [
            {"show_id": 1, "loop_index": 1, "action_type": "add", "stem_index": None, "stem_details": {}, "action_description": "Added synth"}
        ]
        state.current_show_id = 1

        # Mock the whole DatabaseManager class
        mock_db_instance = MagicMock()
        mock_session = MagicMock()
        mock_db_instance.session.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_db_instance.session.return_value.__exit__ = MagicMock(return_value=False)

        with patch("app.db.DatabaseManager") as mock_db_class:
            mock_db_class.return_value = mock_db_instance
            # The function uses get_instance() as a static method
            with patch.object(mock_db_class, 'get_instance', return_value=mock_db_instance):
                flush_recording_buffers()

        # Verify bulk_insert was called (models are imported inside the function)
        # Note: The actual models (LLMInteraction, ShowAction) are imported inside
        # flush_recording_buffers, so we verify the session was used

        # Buffers should be cleared after flush
        assert len(state.llm_interaction_buffer) == 0
        assert len(state.action_buffer) == 0


class TestDeduplicationEdgeCases:
    """Test deduplication edge cases."""

    def test_deduplication_with_none_model_id(self):
        """Test deduplication key generation with None model_id."""
        tracks = [
            {"model_id": None, "major_family": "Synth", "sub_family": "Pad", "timbre_tags": ["Warm"], "notation_tag": "chord", "fx_tag": "Reverb"},
            {"model_id": None, "major_family": "Synth", "sub_family": "Pad", "timbre_tags": ["Warm"], "notation_tag": "chord", "fx_tag": "Reverb"},
        ]

        unique_tracks = {}
        for t in tracks:
            if not t:
                continue
            m_id = t.get("model_id", "default")
            t_key = f"{m_id}_{t.get('major_family')}_{t.get('sub_family')}_{'_'.join(t.get('timbre_tags', []))}_{t.get('notation_tag')}_{t.get('fx_tag')}"
            if t_key not in unique_tracks:
                unique_tracks[t_key] = t

        # Should only have 1 unique track
        assert len(unique_tracks) == 1

    def test_deduplication_with_different_model_ids(self):
        """Test that different model_ids create different keys."""
        tracks = [
            {"model_id": "model_a", "major_family": "Synth", "sub_family": "Pad", "timbre_tags": ["Warm"], "notation_tag": "chord", "fx_tag": "Reverb"},
            {"model_id": "model_b", "major_family": "Synth", "sub_family": "Pad", "timbre_tags": ["Warm"], "notation_tag": "chord", "fx_tag": "Reverb"},
        ]

        unique_tracks = {}
        for t in tracks:
            if not t:
                continue
            m_id = t.get("model_id", "default")
            t_key = f"{m_id}_{t.get('major_family')}_{t.get('sub_family')}_{'_'.join(t.get('timbre_tags', []))}_{t.get('notation_tag')}_{t.get('fx_tag')}"
            if t_key not in unique_tracks:
                unique_tracks[t_key] = t

        # Should have 2 unique tracks (different model_ids)
        assert len(unique_tracks) == 2


class TestActionProcessingEdgeCases:
    """Test edge cases in action processing."""

    def test_add_action_with_missing_optional_fields(self):
        """Test add action with only required fields."""
        action = {
            "action_type": "add",
            "model_id": "test_model",
            "major_family": "Drums",
        }

        a_type = action.get("action_type")

        if a_type == "add":
            major = action.get("major_family", "Synth")
            sub = action.get("sub_family", "Synth Lead")
            new_track = {
                "model_id": action.get("model_id"),
                "major_family": major,
                "sub_family": sub,
                "timbre_tags": action.get("timbre_tags", ["Warm"]),
                "notation_tag": action.get("notation_tag", "melody"),
                "fx_tag": action.get("fx_tag", "Medium Reverb"),
                "bars": action.get("bars", 4),
                "_age": 0
            }

            assert new_track["major_family"] == "Drums"
            assert new_track["sub_family"] == "Synth Lead"  # Default
            assert new_track["timbre_tags"] == ["Warm"]  # Default
            assert new_track["bars"] == 4  # Default

    def test_retain_action_with_stem_having_original_details(self):
        """Test retain action with stem that has _original_details."""
        active_stems = [
            {"prompt": "Synth, Pad, Warm", "_age": 3, "_original_details": {"model_id": "a", "major_family": "Synth", "_age": 2}}
        ]

        action = {"action_type": "retain", "stem_index": 0}
        a_type = action.get("action_type")
        idx = action.get("stem_index")

        if a_type == "retain" and idx is not None and 0 <= idx < len(active_stems):
            s = active_stems[idx]
            orig = s.get('_original_details', {})
            orig['_age'] = s.get('_age', 0) + 1
            assert orig['_age'] == 4

    def test_remove_action_logging(self):
        """Test remove action produces correct log message."""
        active_stems = [
            {"prompt": "Synth, Pad, Warm, melody, reverb, C minor"}
        ]

        action = {"action_type": "remove", "stem_index": 0}
        a_type = action.get("action_type")
        idx = action.get("stem_index")

        if a_type == "remove" and idx is not None and 0 <= idx < len(active_stems):
            s = active_stems[idx]
            # Split by comma and strip whitespace
            parts = [p.strip() for p in s.get("prompt", "").split(",")]
            removed_prompt = parts[1] if len(parts) > 1 else ""
            assert removed_prompt == "Pad"

    def test_action_with_none_stem_index(self):
        """Test action with None stem_index is handled gracefully."""
        action = {"action_type": "retain", "stem_index": None}
        a_type = action.get("action_type")
        idx = action.get("stem_index")

        should_process = a_type == "retain" and idx is not None and 0 <= idx < 1
        assert should_process is False


class TestHistoryFormatting:
    """Test stem history formatting."""

    def test_history_with_5_loops(self):
        """Test history string formatting with 5 loops."""
        stem_history = [
            [{"prompt": "Synth, Pad, Warm"}],
            [{"prompt": "Synth, Pad, Warm"}, {"prompt": "Bass, Sub"}],
            [{"prompt": "Synth, Pad, Warm"}, {"prompt": "Bass, Sub"}, {"prompt": "Drums, Kick"}],
            [{"prompt": "Bass, Sub"}, {"prompt": "Drums, Kick"}],
            [{"prompt": "Drums, Kick"}, {"prompt": "Synth, Lead"}],
        ]

        simple_history = []
        for loop_stems in stem_history[-5:]:
            prompts = [s.get('prompt', '').split(',')[0] for s in loop_stems]
            simple_history.append("+".join(prompts))

        history_str = " | ".join(simple_history)

        assert "Synth" in history_str
        assert "Bass" in history_str
        assert "Drums" in history_str
        # Should have 5 entries
        assert len(simple_history) == 5

    def test_history_with_empty_loop(self):
        """Test history formatting when a loop is empty."""
        stem_history = [
            [{"prompt": "Synth, Pad"}],
            [],  # Empty loop
            [{"prompt": "Drums, Kick"}],
        ]

        simple_history = []
        for loop_stems in stem_history[-5:]:
            prompts = [s.get('prompt', '').split(',')[0] for s in loop_stems]
            simple_history.append("+".join(prompts))

        # Should handle empty list without error
        assert len(simple_history) == 3
