"""Phase 9 guard: pydantic models accept the modernized ``X | None`` annotations.

The ruff UP045 pass converted ``Optional[X]`` -> ``X | None`` across the codebase.
pydantic v2 accepts PEP-604 unions on >=3.10, but this commits a regression test
so a future pydantic/annotation change can't silently break request parsing.
"""

from __future__ import annotations

from app.routes.schemas import LLMConfig, StateUpdate


def test_state_update_all_none_defaults() -> None:
    """StateUpdate (many Optional -> X|None) instantiates with all-None defaults."""
    su = StateUpdate()
    assert su.is_generating is None
    assert su.target_bpm_override is None
    assert su.target_key_override is None
    assert su.available_instruments is None


def test_state_update_with_values() -> None:
    su = StateUpdate(is_generating=True, target_bpm_override=128, available_instruments=["Drums", "Bass"])
    assert su.is_generating is True
    assert su.target_bpm_override == 128
    assert su.available_instruments == ["Drums", "Bass"]


def test_llm_config_optional_fields() -> None:
    c = LLMConfig()
    assert c.base_url is None
    c2 = LLMConfig(base_url="http://x:1234/v1", api_key="k", model="m")
    assert c2.base_url == "http://x:1234/v1"
