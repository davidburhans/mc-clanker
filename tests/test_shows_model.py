import os
from datetime import datetime

# Set test environment before importing modules
os.environ["DATABASE_URL"] = ""  # Force SQLite for tests


class TestShowModel:
    """Tests for Show model."""

    def test_show_to_dict_without_password(self):
        from app.models.show import Show

        show = Show(
            id=1,
            user_id=10,
            title="Test Show",
            description="A test show",
            status="draft",
            audio_file_path=None,
            audience_password_hash="$2b$12$hashed",
            config_snapshot={"bpm": 128},
            created_at=datetime(2024, 1, 1, 12, 0, 0),
        )

        result = show.to_dict(include_audience_password=False)

        assert result["id"] == 1
        assert result["user_id"] == 10
        assert result["title"] == "Test Show"
        assert result["status"] == "draft"
        assert "has_audience_password" not in result  # Not included when False

    def test_show_to_dict_with_password(self):
        from app.models.show import Show

        show = Show(
            id=1,
            user_id=10,
            title="Test Show",
            description="A test show",
            status="live",
            audio_file_path="/path/to/audio.wav",
            audience_password_hash="$2b$12$hashed",
            config_snapshot={"bpm": 128},
            created_at=datetime(2024, 1, 1, 12, 0, 0),
        )

        result = show.to_dict(include_audience_password=True)

        assert result["has_audience_password"] is True
        assert "audience_password_hash" not in result  # Never exposed

    def test_show_status_values(self):
        from app.models.show import Show

        # Valid statuses should be stored as-is
        show = Show(
            id=1,
            user_id=10,
            title="Test",
            status="draft",
        )
        assert show.status == "draft"

        show.status = "live"
        assert show.status == "live"

        show.status = "ended"
        assert show.status == "ended"

        show.status = "archived"
        assert show.status == "archived"


class TestShowActionModel:
    """Tests for ShowAction model."""

    def test_show_action_to_dict(self):
        from app.models.show_action import ShowAction

        action = ShowAction(
            id=1,
            show_id=5,
            loop_index=3,
            timestamp=datetime(2024, 1, 1, 12, 0, 0),
            relative_time_ms=5000,
            action_type="add",
            stem_index=None,
            stem_details={"model_id": "test", "major_family": "Drums"},
            action_description="Added Drums",
        )

        result = action.to_dict()

        assert result["id"] == 1
        assert result["show_id"] == 5
        assert result["loop_index"] == 3
        assert result["action_type"] == "add"
        assert result["stem_details"]["model_id"] == "test"


class TestLLMInteractionModel:
    """Tests for LLMInteraction model."""

    def test_llm_interaction_to_dict(self):
        from app.models.llm_interaction import LLMInteraction

        interaction = LLMInteraction(
            id=1,
            show_id=5,
            loop_index=2,
            timestamp=datetime(2024, 1, 1, 12, 0, 0),
            relative_time_ms=3000,
            prompt_messages=[{"role": "system", "content": "You are a DJ"}],
            parsed_response={"master_bpm": 128, "actions": []},
            reasoning="Keeping the groove",
            error=None,
            was_fallback=False,
        )

        result = interaction.to_dict()

        assert result["id"] == 1
        assert result["show_id"] == 5
        assert result["loop_index"] == 2
        assert result["reasoning"] == "Keeping the groove"
        assert result["was_fallback"] is False

    def test_llm_interaction_to_llm_dump_dict(self):
        from app.models.llm_interaction import LLMInteraction

        interaction = LLMInteraction(
            id=1,
            show_id=5,
            loop_index=2,
            relative_time_ms=3000,
            prompt_messages=[{"role": "user", "content": "Hello"}],
            parsed_response={"answer": 42},
            reasoning="Test",
            error=None,
            was_fallback=False,
        )

        result = interaction.to_llm_dump_dict()

        # Should only contain messages and response
        assert "messages" in result
        assert "response" in result
        assert "reasoning" not in result  # Not in dump format
        assert "error" not in result


class TestShowModelEdgeCases:
    """Test Show model edge cases."""

    def test_show_to_dict_with_all_fields(self):
        from datetime import datetime

        from app.models.show import Show

        show = Show(
            id=1,
            user_id=10,
            title="Test Show",
            description="A detailed description",
            status="live",
            audio_file_path="/path/to/audio.wav",
            audience_password_hash="$2b$12$hashed",
            config_snapshot={"bpm": 128, "key": "C minor"},
            created_at=datetime(2024, 1, 1, 12, 0, 0),
            started_at=datetime(2024, 1, 1, 12, 0, 0),
            ended_at=None,
            duration_seconds=None,
        )

        result = show.to_dict(include_audience_password=True)

        assert result["id"] == 1
        assert result["title"] == "Test Show"
        assert result["status"] == "live"
        assert result["config_snapshot"]["bpm"] == 128
        assert "has_audience_password" in result

    def test_show_to_dict_includes_timestamps(self):
        from datetime import datetime

        from app.models.show import Show

        show = Show(
            id=1,
            user_id=10,
            title="Test",
            status="ended",
            started_at=datetime(2024, 1, 1, 12, 0, 0),
            ended_at=datetime(2024, 1, 1, 13, 0, 0),
            duration_seconds=3600,
        )

        result = show.to_dict(include_audience_password=False)

        assert result["duration_seconds"] == 3600


class TestShowActionModelEdgeCases:
    """Test ShowAction model edge cases."""

    def test_show_action_with_none_stem_details(self):
        from datetime import datetime

        from app.models.show_action import ShowAction

        action = ShowAction(
            id=1,
            show_id=5,
            loop_index=3,
            timestamp=datetime(2024, 1, 1, 12, 0, 0),
            relative_time_ms=5000,
            action_type="remove",
            stem_index=0,
            stem_details=None,
            action_description="Removed old synth",
        )

        result = action.to_dict()

        assert result["stem_details"] is None
        assert result["action_description"] == "Removed old synth"


class TestLLMInteractionModelEdgeCases:
    """Test LLMInteraction model edge cases."""

    def test_llm_interaction_with_error(self):
        from datetime import datetime

        from app.models.llm_interaction import LLMInteraction

        interaction = LLMInteraction(
            id=1,
            show_id=5,
            loop_index=2,
            timestamp=datetime(2024, 1, 1, 12, 0, 0),
            relative_time_ms=3000,
            prompt_messages=[{"role": "system", "content": "You are a DJ"}],
            parsed_response=None,
            reasoning="Test reasoning",
            error="GPU out of memory",
            was_fallback=True,
        )

        result = interaction.to_dict()

        assert result["error"] == "GPU out of memory"
        assert result["was_fallback"] is True

    def test_llm_interaction_to_llm_dump_dict_with_error(self):
        from app.models.llm_interaction import LLMInteraction

        interaction = LLMInteraction(
            id=1,
            show_id=5,
            loop_index=2,
            relative_time_ms=3000,
            prompt_messages=[{"role": "user", "content": "Hello"}],
            parsed_response={"answer": 42},
            reasoning="Test",
            error="Some error",
            was_fallback=False,
        )

        result = interaction.to_llm_dump_dict()

        # Error should not be in dump format
        assert "error" not in result
        assert "reasoning" not in result
