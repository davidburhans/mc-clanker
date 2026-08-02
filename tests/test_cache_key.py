"""Guard + identity tests for the foreground/background stem-audio cache key.

Closes the silent-drift hazard flagged in ``refactor/review/final/quality.md``
(cache_key CONCERN) and pins DoD gate-8's cache-key dimension:

- The SAME ``make_cache_key()`` (single source of truth in ``domain_audio``)
  must be used at every site that builds a stem-audio cache key. A source-level
  guard prevents the foreground (``_step_submit_jobs`` / ``tile_to_loop``) and
  background (``run_pregeneration``) paths from drifting apart — which would
  silently re-submit a job the other path already cached (duplicate stems /
  wasted GPU) while the divergence tests stay green.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from app.framework.domain_audio import make_cache_key

_FRAMEWORK_DIR = Path(__file__).resolve().parent.parent / "app" / "framework"

# Pattern that catches an INLINE f-string cache-key construction. After the
# dedup, every cache key must flow through ``make_cache_key``; an inline
# ``cache_key = f"{...}_{...}_..."`` is the drift smell we forbid.
_INLINE_CACHE_KEY_FSTRING = re.compile(r'cache_key\s*=\s*f"')


def test_make_cache_key_is_deterministic_and_matches_historical_format() -> None:
    """The key format must be frozen so pre-existing stem_cache entries still hit.

    Historical format (pre-dedup, identical at all 3 sites):
    ``{model_id}_{prompt}_{bpm}_{key}_{bars}``. Changing it here silently
    invalidates every cached stem, so this test makes a format change a
    visible, reviewed decision.
    """
    assert (
        make_cache_key("foundation-1", "Synth, A minor, 128", 128, "A minor", 4)
        == "foundation-1_Synth, A minor, 128_128_A minor_4"
    )
    # Deterministic across calls.
    assert make_cache_key(None, "p", 120, "C major", 8) == make_cache_key(None, "p", 120, "C major", 8)
    # Distinct inputs produce distinct keys.
    assert make_cache_key("m", "p", 120, "C major", 8) != make_cache_key("m", "p", 120, "C major", 4)


def test_no_inline_cache_key_fstring_remains_in_framework() -> None:
    """No ``cache_key = f"..."`` may remain in app/framework — all sites must
    call ``make_cache_key``. A new inline construction reopens the drift hazard.
    """
    offenders: list[str] = []
    for py in sorted(_FRAMEWORK_DIR.glob("*.py")):
        for lineno, line in enumerate(py.read_text().splitlines(), 1):
            if _INLINE_CACHE_KEY_FSTRING.search(line):
                offenders.append(f"{py.name}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Inline cache_key f-string found — route through make_cache_key() instead:\n"
        + "\n".join(offenders)
    )


def test_all_three_call_sites_invoke_make_cache_key() -> None:
    """The three consumers must reference ``make_cache_key`` by name.

    Sites: ``domain_audio.tile_to_loop`` (P9), ``loop_orchestrator._step_submit_jobs``
    (P7 foreground), ``pregeneration.run_pregeneration`` (background). If a site
    drops the call (e.g. re-inlines the key), this fails before the drift can ship.
    """
    expected = {
        "domain_audio.py": "make_cache_key",
        "loop_orchestrator.py": "make_cache_key",
        "pregeneration.py": "make_cache_key",
    }
    for fname, needle in expected.items():
        source = (_FRAMEWORK_DIR / fname).read_text()
        assert needle in source, f"{fname} must call make_cache_key (cache-key drift guard)"


def test_make_cache_key_returns_a_value_usable_as_a_dict_key() -> None:
    """Smoke: the key must be a hashable str usable in ``stem_cache`` / LRU."""
    key = make_cache_key("foundation-1", "p", 128, "A minor", 4)
    assert isinstance(key, str)
    cache: dict[str, dict] = {key: {"audio_data": np.zeros((2, 2)), "last_used": 0.0}}
    assert key in cache  # hashable + round-trips as a lookup
