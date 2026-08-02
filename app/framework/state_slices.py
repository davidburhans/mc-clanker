"""Additive typed read-view slices over GlobalState (Phase 8 / E3 pass-1).

Each slice is a READ VIEW over the SAME ``state.__dict__`` — storage does not
move, no attribute is renamed, and the legacy ``state.current_bpm``-style access
keeps working unchanged. The slice only forwards reads of its DOCUMENTED members
(``_attrs``); out-of-slice names raise ``AttributeError`` so the boundaries are
real, not decorative.

This is pass-1 (additive properties, zero repo-wide migration). Storage
migration to nested dataclasses + unified-lock restructuring is pass-2, deferred
until the E3 concurrency fixes (A1/A2/A4) land — those already restructure lock
ownership around the high-risk attrs these slices view (brief-03 ssC).

Why ``state.levels`` (not ``state.mixer``): avoids a name clash with
``framework_mixer.Mixer``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.framework.framework_state import GlobalState


class _Slice:
    """Read-view base: forwards documented ``_attrs`` reads to the host state."""

    _attrs: frozenset[str] = frozenset()

    def __init__(self, host: "GlobalState") -> None:
        self._host = host

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in type(self)._attrs:
            return getattr(self._host, name)
        raise AttributeError(f"{type(self).__name__!r} view has no attribute {name!r}")


class MusicalParams(_Slice):
    """Tempo, key, the stem triple, set name, history, LLM reasoning."""

    _attrs = frozenset(
        {
            "current_bpm",
            "current_key",
            "current_set_name",
            "previous_stems",
            "active_stems",
            "next_stems",
            "stem_history",
            "llm_reasoning",
        }
    )


class GenerationControl(_Slice):
    """Generation flags + user overrides + CFG."""

    _attrs = frozenset(
        {
            "is_generating",
            "is_show_started",
            "user_override",
            "target_bpm_override",
            "target_key_override",
            "should_reset",
            "generation_cfg_scale",
            "generation_steps",
        }
    )


class LLMConfig(_Slice):
    """LLM endpoint configuration."""

    _attrs = frozenset({"llm_base_url", "llm_api_key", "llm_model"})


class StemLevels(_Slice):
    """Per-stem mixer levels (the A2 cross-lock attrs — read view only here)."""

    _attrs = frozenset({"stem_volumes", "muted_stems", "soloed_stems"})


class LoopCoordination(_Slice):
    """Loop counter + the 'now audible' snapshot + history."""

    _attrs = frozenset(
        {
            "loop_count",
            "last_actions",
            "currently_playing_loop_index",
            "currently_playing_stems",
            "currently_playing_set_name",
            "currently_playing_reasoning",
            "loop_history",
        }
    )


class RecordingState(_Slice):
    """Export + show recording handles/buffers (sync_lock-protected on state)."""

    _attrs = frozenset(
        {
            "is_recording",
            "recording_format",
            "recording_file_path",
            "recording_start_time",
            "recording_file_handle",
            "current_show_id",
            "current_show_start_time",
            "is_show_recording",
            "llm_interaction_buffer",
            "action_buffer",
            "current_show_audio_file",
        }
    )


class PlaybackState(_Slice):
    """Pre-recorded show playback."""

    _attrs = frozenset({"currently_playing_show_id", "is_playback_active"})


class StemCacheView(_Slice):
    """The capped LRU of generated stems + its cache_stem() method."""

    _attrs = frozenset({"last_generated_stems", "cache_stem"})


class InstrumentCatalog(_Slice):
    """Available + custom instrument catalogs."""

    _attrs = frozenset({"available_instruments", "categorized_instruments", "custom_instruments"})


class SessionConfig(_Slice):
    """Auth + audience message + icecast toggle."""

    _attrs = frozenset(
        {
            "dj_password",
            "audience_password",
            "audience_message",
            "audience_message_ts",
            "icecast_enabled",
        }
    )
