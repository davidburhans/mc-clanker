"""Frozen-API smoke test for ``app.framework.framework_main_async``.

Phase 0 of the E1–E6 refactor (refactor/plan.md). This pins the public/private
surface that downstream callers and tests depend on so a module split cannot
silently drop or rename a name. It goes beyond an import-only check by:

- importing ALL 9 frozen names (non-None),
- *instantiating* ``AsyncFrameworkLoop(uuid4())`` (catches a constructor-signature
  break that a bare import misses — the one-arg call is used by ``app_ui.py``
  and ~12 test sites), and
- asserting the two string-patch targets (``decode_aac`` /
  ``create_garage_client_from_env``) resolve to *callables* on the module.

Note: this does NOT itself neutralize the silent string-patch no-op risk
(brief-02 §D) — that is the Phase 2 guard test's job. Here we only assert the
names remain bound and callable so a relocation that drops them fails loudly.
"""

from uuid import uuid4

import app.framework.framework_main_async as mfa


def test_frozen_api_importable():
    """All frozen names are importable, non-None, and callables resolve."""
    names = [
        "AsyncFrameworkLoop",
        "run_framework_loop_async",
        "flush_recording_buffers",
        "process_actions",
        "calc_duration",
        "_to_two_channel",
        "_flush_lock",
        "create_garage_client_from_env",
        "decode_aac",
    ]
    resolved = {n: getattr(mfa, n, None) for n in names}
    missing = [n for n, v in resolved.items() if v is None]
    assert not missing, f"frozen names missing/None on module: {missing}"

    # Instantiation catches a constructor-signature break (the one-arg call used
    # by app_ui.py:90 and ~12 test sites). Must succeed without extra args.
    loop = mfa.AsyncFrameworkLoop(uuid4())
    assert loop is not None
    assert loop.session_id is not None

    # The two string-patch targets must remain callable on the module namespace.
    assert callable(getattr(mfa, "decode_aac")), "decode_aac must be callable"
    assert callable(getattr(mfa, "create_garage_client_from_env")), (
        "create_garage_client_from_env must be callable"
    )
